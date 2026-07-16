"""작업별 원격 파일, 제출, 로그를 다루는 Mock/SSH 어댑터."""

from __future__ import annotations

import json
import os
import pathlib
import posixpath
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from seraph.connection import MockConnection

from .errors import ApiError

_SUBMITTED = re.compile(r"Submitted batch job\s+(\d+)")
_JOB_ID = re.compile(r"^[0-9]+$")


@dataclass
class PathInfo:
    path: str
    exists: bool
    readable: bool
    writable: bool
    is_file: bool
    is_dir: bool
    parent_writable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "readable": self.readable,
            "writable": self.writable,
            "is_file": self.is_file,
            "is_dir": self.is_dir,
            "parent_writable": self.parent_writable,
        }


class MockRemote:
    """실제 서버 없이 업로드부터 완료까지 시연하는 로컬 샌드박스."""

    mode = "mock"

    def __init__(self, connection: Any):
        self.connection = connection
        self._tmp = tempfile.TemporaryDirectory(prefix="seraph-gui-mock-")
        self._root = pathlib.Path(self._tmp.name)
        self._home = "/home/mockuser"
        self.username = "mockuser"
        self.data_root = f"{connection.config.data_root}/{self.username}"
        self._jobs: dict[str, dict[str, Any]] = {}
        self._next_job_id = 990001

    @property
    def home(self) -> str:
        return self._home

    @property
    def jobs_root(self) -> str:
        return f"{self.data_root}/.seraph-gui/jobs"

    def _local(self, remote_path: str) -> pathlib.Path:
        clean = posixpath.normpath(remote_path)
        prefix = self.data_root + "/"
        if clean == self.data_root:
            rel = ""
        elif clean.startswith(prefix):
            rel = clean[len(prefix):]
        else:
            raise ApiError(
                "REMOTE_PATH_NOT_ALLOWED",
                "작업 파일은 /data의 사용자 작업 폴더 아래에만 만들 수 있습니다.",
                status_code=403,
            )
        local = (self._root / rel).resolve()
        if self._root.resolve() not in (local, *local.parents):
            raise ApiError("REMOTE_PATH_NOT_ALLOWED", "안전하지 않은 원격 경로입니다.", 403)
        return local

    def path_info(self, path: str) -> PathInfo:
        # Mock에서는 공용 NAS 데이터셋을 읽을 수 있는 것으로 흉내 낸다.
        if path.startswith("/data/") and not path.startswith(self.data_root + "/"):
            suffix = pathlib.PurePosixPath(path).suffix.lower()
            is_file = bool(suffix)
            return PathInfo(path, True, True, True, is_file, not is_file, True)
        if path == self.data_root or path.startswith(self.data_root + "/"):
            local = self._local(path)
            exists = local.exists()
            return PathInfo(
                path,
                exists,
                os.access(local, os.R_OK) if exists else False,
                os.access(local, os.W_OK) if exists else False,
                local.is_file(),
                local.is_dir(),
                True,
            )
        return PathInfo(path, True, True, True, False, True, True)

    def make_dir(self, path: str) -> None:
        self._local(path).mkdir(parents=True, exist_ok=True)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        target = self._local(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, target)

    def write_text(self, remote_path: str, text: str) -> None:
        target = self._local(remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)

    def read_text(self, remote_path: str, max_bytes: int = 1_000_000) -> str:
        target = self._local(remote_path)
        if not target.is_file():
            raise FileNotFoundError(remote_path)
        with target.open("rb") as handle:
            return handle.read(max_bytes + 1)[:max_bytes].decode("utf-8", errors="replace")

    def list_directories(self, root: str) -> list[str]:
        base = self._local(root)
        if not base.exists():
            return []
        return sorted(
            (f"{root.rstrip('/')}/{item.name}" for item in base.iterdir() if item.is_dir()),
            reverse=True,
        )

    def tail(self, remote_path: str, max_bytes: int = 128_000) -> str:
        target = self._local(remote_path)
        if not target.exists():
            return ""
        with target.open("rb") as handle:
            size = target.stat().st_size
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")

    def submit(self, remote_dir: str) -> str:
        script = self._local(f"{remote_dir}/job.sbatch")
        if not script.is_file():
            raise ApiError("JOB_NOT_PREPARED", "제출 스크립트를 찾을 수 없습니다.", 409)
        job_id = str(self._next_job_id)
        self._next_job_id += 1
        self._jobs[job_id] = {
            "remote_dir": remote_dir,
            "submitted_at": time.monotonic(),
            "state": "PENDING",
            "cancelled": False,
        }
        self.write_text(
            f"{remote_dir}/stdout.log",
            f"[mock] job {job_id} submitted\n",
        )
        self.write_text(f"{remote_dir}/stderr.log", "")
        return job_id

    def run_preflight(
        self,
        remote_dir: str,
        *,
        partition: str,
        gpus: int,
        high_perf: bool,
        cpus: int,
        memory: str,
        node: str | None,
    ) -> str:
        script = self._local(f"{remote_dir}/preflight.sh")
        if not script.is_file():
            raise ApiError("JOB_NOT_PREPARED", "srun 사전 점검 스크립트를 찾을 수 없습니다.", 409)
        return (
            f"[mock] srun preflight on {node or 'automatic-node'}\n"
            f"[mock] partition={partition} gpu={gpus} cpu-per-gpu={cpus} mem-per-gpu={memory}\n"
            "SERAPH srun preflight OK\n"
        )

    def job_state(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        elapsed = time.monotonic() - job["submitted_at"]
        if job["cancelled"]:
            state, reason, finished = "CANCELLED", "Cancelled by user", True
        elif elapsed < 0.75:
            state, reason, finished = "PENDING", "Priority", False
        elif elapsed < 3.0:
            state, reason, finished = "RUNNING", "None", False
        else:
            state, reason, finished = "COMPLETED", "None", True

        if job["state"] != state:
            job["state"] = state
            line = {
                "RUNNING": f"[mock] job {job_id} running on ariel-v7\n",
                "COMPLETED": f"[mock] training finished successfully\n",
                "CANCELLED": f"[mock] job {job_id} cancelled\n",
            }.get(state)
            if line:
                target = self._local(f"{job['remote_dir']}/stdout.log")
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(line)

        return {
            "state": state,
            "reason": reason,
            "nodes": "ariel-v7" if state in ("RUNNING", "COMPLETED") else "",
            "elapsed_seconds": int(max(elapsed, 0)),
            "finished": finished,
            "exit_code": "0:0" if state == "COMPLETED" else None,
        }

    def cancel(self, job_id: str) -> None:
        if job_id not in self._jobs:
            raise ApiError("JOB_NOT_FOUND", "Slurm 작업을 찾을 수 없습니다.", 404)
        self._jobs[job_id]["cancelled"] = True

    def close(self) -> None:
        self._tmp.cleanup()


class SSHRemote:
    """Paramiko SFTP와 제한된 Slurm 명령만 제공하는 실서버 어댑터."""

    mode = "ssh"

    def __init__(self, connection: Any):
        self.connection = connection
        self.sftp = connection.client.open_sftp()
        self._home = posixpath.normpath(self.sftp.normalize("."))
        self.username = connection.username
        self.data_root = f"{connection.config.data_root}/{self.username}"

    @property
    def home(self) -> str:
        return self._home

    @property
    def jobs_root(self) -> str:
        return f"{self.data_root}/.seraph-gui/jobs"

    def _guard_job_path(self, path: str) -> str:
        clean = posixpath.normpath(path)
        root = posixpath.normpath(self.jobs_root)
        if clean != root and not clean.startswith(root + "/"):
            raise ApiError(
                "REMOTE_PATH_NOT_ALLOWED",
                "작업 파일은 사용자 작업 폴더 아래에만 만들 수 있습니다.",
                status_code=403,
            )
        return clean

    def path_info(self, path: str) -> PathInfo:
        quoted = shlex.quote(path)
        command = (
            f"p={quoted}; "
            "if [ -e \"$p\" ]; then e=1; else e=0; fi; "
            "if [ -r \"$p\" ]; then r=1; else r=0; fi; "
            "if [ -w \"$p\" ]; then w=1; else w=0; fi; "
            "if [ -f \"$p\" ]; then f=1; else f=0; fi; "
            "if [ -d \"$p\" ]; then d=1; else d=0; fi; "
            "parent=$(dirname -- \"$p\"); "
            "while [ ! -e \"$parent\" ] && [ \"$parent\" != / ]; do parent=$(dirname -- \"$parent\"); done; "
            "if [ -w \"$parent\" ]; then pw=1; else pw=0; fi; "
            "printf '%s|%s|%s|%s|%s|%s' \"$e\" \"$r\" \"$w\" \"$f\" \"$d\" \"$pw\""
        )
        raw = self.connection.run_command(command, label="경로 확인", timeout=15)
        parts = raw.strip().split("|")
        if len(parts) != 6:
            raise ApiError("SERAPH_COMMAND_FAILED", "서버 경로를 확인하지 못했습니다.", 502)
        flags = [part == "1" for part in parts]
        return PathInfo(path, flags[0], flags[1], flags[2], flags[3], flags[4], flags[5])

    def make_dir(self, path: str) -> None:
        clean = self._guard_job_path(path)
        current = "/" if clean.startswith("/") else ""
        for part in clean.split("/"):
            if not part:
                continue
            current = posixpath.join(current, part)
            try:
                self.sftp.stat(current)
            except OSError:
                self.sftp.mkdir(current, mode=0o700)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        target = self._guard_job_path(remote_path)
        self.make_dir(posixpath.dirname(target))
        self.sftp.put(local_path, target)
        self.sftp.chmod(target, 0o600)

    def write_text(self, remote_path: str, text: str) -> None:
        target = self._guard_job_path(remote_path)
        self.make_dir(posixpath.dirname(target))
        tmp = target + ".tmp"
        with self.sftp.file(tmp, "w") as handle:
            handle.write(text)
            handle.flush()
        self.sftp.chmod(tmp, 0o600)
        try:
            self.sftp.remove(target)
        except OSError:
            pass
        self.sftp.rename(tmp, target)

    def read_text(self, remote_path: str, max_bytes: int = 1_000_000) -> str:
        target = self._guard_job_path(remote_path)
        with self.sftp.file(target, "rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")

    def list_directories(self, root: str) -> list[str]:
        clean = self._guard_job_path(root)
        try:
            attrs = self.sftp.listdir_attr(clean)
        except OSError:
            return []
        out = []
        for attr in attrs:
            # S_IFDIR bit mask without importing platform-specific stat helpers.
            if attr.st_mode & 0o170000 == 0o040000:
                out.append(posixpath.join(clean, attr.filename))
        return sorted(out, reverse=True)

    def tail(self, remote_path: str, max_bytes: int = 128_000) -> str:
        target = self._guard_job_path(remote_path)
        try:
            size = self.sftp.stat(target).st_size
            with self.sftp.file(target, "rb") as handle:
                handle.seek(max(0, size - max_bytes))
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def submit(self, remote_dir: str) -> str:
        clean = self._guard_job_path(remote_dir)
        command = f"cd {shlex.quote(clean)} && sbatch job.sbatch"
        raw = self.connection.run_command(command, label="sbatch 제출", timeout=30)
        match = _SUBMITTED.search(raw)
        if not match:
            raise ApiError(
                "SERAPH_COMMAND_FAILED",
                "Slurm이 작업 ID를 반환하지 않았습니다.",
                status_code=502,
            )
        return match.group(1)

    def run_preflight(
        self,
        remote_dir: str,
        *,
        partition: str,
        gpus: int,
        high_perf: bool,
        cpus: int,
        memory: str,
        node: str | None,
    ) -> str:
        clean = self._guard_job_path(remote_dir)
        gres = f"gpu:high_perf:{gpus}" if high_perf else f"gpu:{gpus}"
        args = [
            "srun",
            "--ntasks=1",
            f"--gres={gres}",
            f"--cpus-per-gpu={cpus}",
            f"--mem-per-gpu={memory}",
            f"--partition={partition}",
            "--time=00:05:00",
            "--kill-on-bad-exit=1",
        ]
        if node:
            args.append(f"--nodelist={node}")
        args.extend(["/usr/bin/bash", "preflight.sh"])
        command = f"cd {shlex.quote(clean)} && " + " ".join(shlex.quote(item) for item in args)
        return self.connection.run_command(command, label="srun 사전 점검", timeout=360)

    def job_state(self, job_id: str) -> dict[str, Any] | None:
        if not _JOB_ID.fullmatch(job_id):
            raise ApiError("INVALID_REQUEST", "Slurm 작업 ID 형식이 올바르지 않습니다.", 422)
        queue = self.connection.run_command(
            f'squeue -h -j {job_id} -o "%T|%R|%N|%M"',
            label="작업 상태",
            timeout=15,
        ).strip()
        if queue:
            state, reason, nodes, elapsed = (queue.split("|") + ["", "", "", ""])[:4]
            return {
                "state": state,
                "reason": reason.strip("()"),
                "nodes": "" if nodes in ("(null)", "n/a") else nodes,
                "elapsed": elapsed,
                "finished": False,
                "exit_code": None,
            }

        accounting = self.connection.run_command(
            f'sacct -n -P -j {job_id} -X -o "JobIDRaw,State,ExitCode,Elapsed,NodeList"',
            label="완료 작업 상태",
            timeout=30,
        )
        for line in accounting.splitlines():
            parts = line.split("|")
            if len(parts) < 5 or parts[0].strip() != job_id:
                continue
            state = parts[1].strip().split()[0]
            return {
                "state": state,
                "reason": "",
                "nodes": parts[4].strip(),
                "elapsed": parts[3].strip(),
                "finished": state not in {"PENDING", "RUNNING", "REQUEUED", "SUSPENDED"},
                "exit_code": parts[2].strip(),
            }
        return None

    def cancel(self, job_id: str) -> None:
        if not _JOB_ID.fullmatch(job_id):
            raise ApiError("INVALID_REQUEST", "Slurm 작업 ID 형식이 올바르지 않습니다.", 422)
        self.connection.run_command(f"scancel {job_id}", label="작업 취소", timeout=15)

    def close(self) -> None:
        self.sftp.close()


def create_remote(connection: Any) -> MockRemote | SSHRemote:
    if isinstance(connection, MockConnection):
        return MockRemote(connection)
    return SSHRemote(connection)


def read_job_json(remote: MockRemote | SSHRemote, remote_dir: str) -> dict[str, Any]:
    try:
        raw = remote.read_text(f"{remote_dir}/job.json", max_bytes=256_000)
        value = json.loads(raw)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("JOB_NOT_FOUND", "작업 정보를 찾을 수 없습니다.", 404) from exc
    if not isinstance(value, dict):
        raise ApiError("JOB_NOT_FOUND", "작업 정보가 손상되었습니다.", 404)
    return value
