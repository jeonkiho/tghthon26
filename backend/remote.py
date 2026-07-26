"""작업별 원격 파일, 제출, 로그를 다루는 Mock/SSH 어댑터."""

from __future__ import annotations

import io
import json
import os
import pathlib
import posixpath
import re
import shlex
import shutil
import stat
import tarfile
import threading
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

from seraph import connection as connection_module
from seraph.connection import MockConnection

from .errors import ApiError


def _is_supported_archive(path: str) -> bool:
    """세라프가 허용하는 데이터셋 압축 형식. NAS IOPS 보호를 위해 파일 하나로 올린다."""
    lower = path.lower()
    return lower.endswith((".tar", ".tar.gz", ".tgz", ".zip"))

_SUBMITTED = re.compile(r"Submitted batch job\s+(\d+)")
_JOB_ID = re.compile(r"^[0-9]+$")

# 미리보기 상한. 로그 한 줄 보려고 수 GB 짜리 체크포인트를 끌어오면 안 된다.
PREVIEW_MAX_BYTES = 256 * 1024

# 다운로드는 상한이 없다(체크포인트를 가져오는 게 목적이다). 대신 한 번에 이만큼씩
# 흘려보내 백엔드 메모리에 파일 전체가 올라오지 않게 한다.
DOWNLOAD_CHUNK_BYTES = 256 * 1024


def _MOCK_PUBLIC_ENTRIES(path: str) -> list[dict[str, Any]]:
    """서버 없는 시연에서 공용 데이터셋 폴더가 비어 보이지 않게 하는 흉내."""
    if posixpath.normpath(path) != "/data/datasets":
        return []
    names = [("tarfiles", True, None), ("imagenet-mini.tar.gz", False, 1_482_910_720),
             ("cifar10.tar.gz", False, 170_498_071)]
    return [{
        "name": name,
        "path": posixpath.join(path, name),
        "is_dir": is_dir,
        "is_link": False,
        "hidden": False,
        "size": size,
        "mtime": None,
        "is_archive": (not is_dir) and _is_supported_archive(name),
    } for name, is_dir, size in names]


def _preview_payload(path: str, raw: bytes, size: int, max_bytes: int) -> dict[str, Any]:
    """읽어 온 바이트를 화면이 쓸 모양으로 바꾼다.

    이진 파일을 텍스트로 우겨넣으면 화면이 깨진 문자로 뒤덮인다. 그럴 바에는
    "이건 텍스트가 아니다"라고 말하는 게 낫다. NUL 바이트가 있으면 이진으로 본다.
    """
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    if b"\x00" in raw:
        return {
            "path": path, "size": size, "binary": True,
            "truncated": truncated, "text": None,
        }
    return {
        "path": path,
        "size": size,
        "binary": False,
        "truncated": truncated,
        "text": raw.decode("utf-8", errors="replace"),
    }

# conda/pip 가 패키지를 받아야 하는 곳. 여기가 막혀 있으면 "처음부터 만들기"는
# 아무리 기다려도 실패한다 — 누르기 전에 알아야 한다.
PACKAGE_HOSTS = ("https://conda.anaconda.org/", "https://pypi.org/simple/")

_PROBE_SCRIPT = """#!/usr/bin/bash
# SERAPH GUI 환경 점검. 읽기만 한다 — 설치하거나 지우지 않는다.
CONDA_ROOT={conda_root}
DATA_ROOT={data_root}

if [ -n "$CONDA_ROOT" ] && [ -x "$CONDA_ROOT/bin/conda" ]; then
  printf 'conda_version=%s\\n' "$("$CONDA_ROOT/bin/conda" --version 2>/dev/null | tr -d '\\r')"
fi
printf 'mamba=%s\\n' "$(command -v mamba 2>/dev/null)"
printf 'writable=%s\\n' "$([ -w "$DATA_ROOT" ] && echo 1 || echo 0)"

# df 는 파일시스템 전체 여유를 말한다. 개인 쿼터와 다를 수 있어서 화면에서도
# '파일시스템 여유'라고 부른다 — 없는 정보를 있는 척하지 않는다.
line=$(df -Pk "$DATA_ROOT" 2>/dev/null | awk 'NR==2 {{print $1 " " $4}}')
printf 'filesystem=%s\\n' "${{line%% *}}"
printf 'avail_kb=%s\\n' "${{line##* }}"
printf 'loadavg=%s\\n' "$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
printf 'nproc=%s\\n' "$(nproc 2>/dev/null)"

for u in {hosts}; do
  c=000
  if command -v curl >/dev/null 2>&1; then
    c=$(curl -s -o /dev/null -m 8 -w '%{{http_code}}' "$u" 2>/dev/null || echo 000)
  elif command -v wget >/dev/null 2>&1; then
    if wget -q -T 8 -O /dev/null "$u" >/dev/null 2>&1; then c=200; else c=000; fi
  fi
  printf 'net=%s %s\\n' "$u" "$c"
done
exit 0
""".replace("{hosts}", " ".join(PACKAGE_HOSTS))


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
        self._builds: dict[str, dict[str, Any]] = {}
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
        # 임시 이름에 난수를 넣는다. 고정 이름이면 같은 파일을 동시에 쓰는 두 흐름이
        # 같은 임시 파일을 열어 윈도우에서 PermissionError 로 죽는다. SSHRemote 와
        # 같은 결함이라 같은 방식으로 막는다.
        tmp = target.with_suffix(f"{target.suffix}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)

    def read_text(self, remote_path: str, max_bytes: int = 1_000_000) -> str:
        target = self._local(remote_path)
        if not target.is_file():
            raise FileNotFoundError(remote_path)
        with target.open("rb") as handle:
            return handle.read(max_bytes + 1)[:max_bytes].decode("utf-8", errors="replace")

    @property
    def datasets_root(self) -> str:
        return f"{self.data_root}/datasets"

    def list_entries(self, path: str, *, show_hidden: bool = False,
                     dirs_only: bool = False) -> dict[str, Any]:
        clean = posixpath.normpath(path or self.data_root)
        # 샌드박스 밖(공용 데이터셋 등)은 흉내만 낸다. path_info 가 이미 같은 방식으로
        # 공용 경로가 있는 척하므로, 탐색기만 403 을 내면 서버 없는 시연이 반쪽이 된다.
        if not self._inside_sandbox(clean):
            return {
                "path": clean,
                "parent": posixpath.dirname(clean) if clean != "/" else None,
                "entries": [] if dirs_only else _MOCK_PUBLIC_ENTRIES(clean),
                "data_root": self.data_root,
                "datasets_root": self.datasets_root,
                "home": self._home,
            }
        base = self._local(clean)
        entries = []
        if base.is_dir():
            for child in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                hidden = child.name.startswith(".")
                if hidden and not show_hidden:
                    continue
                is_dir = child.is_dir()
                if dirs_only and not is_dir:
                    continue
                info = child.stat()
                entries.append({
                    "name": child.name,
                    "path": posixpath.join(clean, child.name),
                    "is_dir": is_dir,
                    "is_link": child.is_symlink(),
                    "hidden": hidden,
                    "size": None if is_dir else info.st_size,
                    "mtime": int(info.st_mtime),
                    "is_archive": child.is_file() and _is_supported_archive(child.name),
                })
        return {
            "path": clean,
            "parent": posixpath.dirname(clean) if clean != "/" else None,
            "entries": entries,
            "data_root": self.data_root,
            "datasets_root": self.datasets_root,
            "home": self._home,
        }

    # --- 탐색기 쓰기 (mock) -----------------------------------------------

    def _guard_removable(self, path: str) -> str:
        clean = posixpath.normpath(path)
        if not self._inside_sandbox(clean):
            raise ApiError("REMOTE_PATH_NOT_ALLOWED", "내 폴더 아래에서만 지울 수 있습니다.", 403)
        protected = {
            posixpath.normpath(self.data_root): "내 폴더 자체는 지울 수 없습니다.",
            posixpath.normpath(f"{self.data_root}/.seraph-gui"):
                "작업 기록 폴더(.seraph-gui)는 지울 수 없습니다.",
        }
        if clean in protected:
            raise ApiError("REMOTE_PATH_PROTECTED", protected[clean], status_code=403)
        return clean

    def describe_target(self, path: str) -> dict[str, Any]:
        clean = self._guard_removable(path)
        target = self._local(clean)
        if target.is_dir():
            folders = files = total = 0
            for child in target.rglob("*"):
                if child.is_dir():
                    folders += 1
                else:
                    files += 1
                    total += child.stat().st_size
            return {"path": clean, "name": posixpath.basename(clean), "is_dir": True,
                    "folders": folders, "files": files, "bytes": total}
        size = target.stat().st_size if target.is_file() else None
        return {"path": clean, "name": posixpath.basename(clean), "is_dir": False,
                "folders": 0, "files": 1, "bytes": size}

    def make_folder(self, parent: str, name: str) -> dict[str, Any]:
        target = posixpath.join(posixpath.normpath(parent), name)
        local = self._local(target)
        if local.exists():
            raise ApiError("REMOTE_MKDIR_FAILED", f"'{name}' 이 이미 있습니다.", 409)
        local.mkdir(parents=True)
        return {"path": target, "name": name}

    def rename_entry(self, path: str, new_name: str) -> dict[str, Any]:
        clean = self._guard_removable(path)
        target = posixpath.join(posixpath.dirname(clean), new_name)
        local_target = self._local(target)
        if local_target.exists():
            raise ApiError("REMOTE_RENAME_FAILED", f"'{new_name}' 이 이미 있습니다.", 409)
        self._local(clean).rename(local_target)
        return {"path": target, "name": new_name}

    def delete_entry(self, path: str) -> dict[str, Any]:
        clean = self._guard_removable(path)
        target = self._local(clean)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
        return {"deleted": clean}

    def upload_into(self, local_path: str, remote_dir: str) -> dict[str, Any]:
        source = pathlib.Path(local_path).expanduser()
        if not source.is_file():
            raise ApiError("LOCAL_FILE_NOT_FOUND", "선택한 파일을 찾을 수 없습니다.", 400)
        target = posixpath.join(posixpath.normpath(remote_dir), source.name)
        local = self._local(target)
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, local)
        return {"path": target, "name": source.name, "size": source.stat().st_size}

    def open_download(self, path: str) -> tuple[Any, dict[str, Any]]:
        clean = posixpath.normpath(path)
        if not self._inside_sandbox(clean):
            raise ApiError(
                "REMOTE_FILE_NOT_READABLE",
                "실서버에 연결해야 내려받을 수 있습니다.",
                status_code=404,
            )
        target = self._local(clean)
        name = posixpath.basename(clean) or "download"
        if target.is_dir():
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                archive.add(target, arcname=name)
            data = buffer.getvalue()
            return iter([data]), {
                "filename": f"{name}.tar.gz", "size": None, "media_type": "application/gzip",
            }
        if not target.is_file():
            raise ApiError("REMOTE_FILE_NOT_READABLE", f"'{clean}' 를 읽을 수 없습니다.", 404)

        def chunks():
            with target.open("rb") as handle:
                while True:
                    block = handle.read(DOWNLOAD_CHUNK_BYTES)
                    if not block:
                        return
                    yield block
        return chunks(), {
            "filename": name,
            "size": target.stat().st_size,
            "media_type": "application/octet-stream",
        }

    def _inside_sandbox(self, path: str) -> bool:
        clean = posixpath.normpath(path)
        return clean == self.data_root or clean.startswith(self.data_root + "/")

    def preview_file(self, path: str, max_bytes: int = PREVIEW_MAX_BYTES) -> dict[str, Any]:
        clean = posixpath.normpath(path)
        if not self._inside_sandbox(clean):
            text = f"[mock] {clean} 의 내용은 실서버에 연결해야 볼 수 있습니다.\n"
            return _preview_payload(clean, text.encode("utf-8"), len(text), max_bytes)
        target = self._local(clean)
        if target.is_dir():
            raise ApiError("REMOTE_PATH_IS_DIR", "폴더는 미리 볼 수 없습니다.", 400)
        if not target.is_file():
            raise ApiError("REMOTE_FILE_NOT_READABLE", f"'{clean}' 를 읽을 수 없습니다.", 404)
        with target.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        return _preview_payload(clean, raw, target.stat().st_size, max_bytes)

    def upload_dataset(self, local_path: str) -> dict[str, Any]:
        source = pathlib.Path(local_path).expanduser()
        if not source.is_file():
            raise ApiError("LOCAL_FILE_NOT_FOUND", "선택한 파일을 찾을 수 없습니다.", 400)
        if not _is_supported_archive(source.name):
            raise ApiError("DATASET_ARCHIVE_REQUIRED", "TAR, TAR.GZ, TGZ, ZIP 만 올릴 수 있습니다.", 400)
        target = f"{self.datasets_root}/{source.name}"
        self.upload_file(str(source), target)
        return {"path": target, "name": source.name, "size": source.stat().st_size}

    def delete_job_dir(self, path: str) -> None:
        root = posixpath.normpath(self.jobs_root)
        clean = posixpath.normpath(path)
        if clean == root or not clean.startswith(root + "/"):
            raise ApiError("REMOTE_PATH_NOT_ALLOWED", "작업 폴더만 삭제할 수 있습니다.", 403)
        shutil.rmtree(self._local(clean), ignore_errors=True)

    def find_conda(self) -> dict[str, Any]:
        # mock 은 개인 설치 하나가 있는 것으로 흉내 낸다.
        return {"installs": [{
            "root": f"{self.data_root}/anaconda3",
            "conda_sh": f"{self.data_root}/anaconda3/etc/profile.d/conda.sh",
            "is_personal": True,
            "envs": ["pytorch1.12.1_p38"],
        }]}

    # --- 개인 환경 만들기 (mock) ------------------------------------------
    # 실서버 없이도 화면 전체를 시연할 수 있어야 한다. job mock 과 같은 방식으로
    # 경과 시간에 따라 빌드가 진행되는 척한다.

    @property
    def envs_root(self) -> str:
        return f"{self.data_root}/envs"

    @property
    def env_builds_root(self) -> str:
        return f"{self.data_root}/.seraph-gui/envbuilds"

    def probe_env_tools(self, conda_root: str | None) -> dict[str, Any]:
        return {
            "conda_version": "conda 24.1.2",
            "mamba": "",
            "writable": True,
            "filesystem": "mock-nas:/data",
            "avail_kb": "419430400",
            "avail_bytes": 419430400 * 1024,
            "loadavg": "0.30 0.25 0.20",
            "nproc": "32",
            "network": {host: 200 for host in PACKAGE_HOSTS},
        }

    def build_dir(self, build_id: str) -> str:
        return f"{self.env_builds_root}/{build_id}"

    def start_env_build(self, build_id: str, script: str, spec: dict[str, Any]) -> None:
        directory = self.build_dir(build_id)
        self.write_text(f"{directory}/spec.json", json.dumps(spec, ensure_ascii=False))
        self.write_text(f"{directory}/build.sh", script)
        self._builds[build_id] = {"started_at": time.monotonic(), "spec": spec}

    def background_state(self, build_id: str) -> dict[str, Any]:
        build = self._builds.get(build_id)
        if build is None:
            return {"rc": None, "alive": False, "log": ""}
        elapsed = time.monotonic() - build["started_at"]
        name = build["spec"]["name"]
        # 실서버는 5~20분 걸리지만 mock 은 시연·테스트용이라 몇 초로 압축한다.
        steps = [
            (0.0, "[seraph] 환경 빌드를 시작합니다"),
            (0.4, "[seraph] 패키지 목록을 확인하는 중 (solve)"),
            (0.9, "[seraph] 패키지를 내려받는 중"),
            (1.4, f"[seraph] {name} 준비 완료"),
        ]
        log = "\n".join(text for at, text in steps if elapsed >= at)
        done = elapsed >= 1.4
        if done:
            prefix = f"{self.envs_root}/{name}"
            self.make_dir(f"{prefix}/conda-meta")
            self.write_text(f"{prefix}/seraph-env.json", json.dumps(build["spec"]))
        return {"rc": 0 if done else None, "alive": not done, "log": log}

    def list_prefix_envs(self) -> list[dict[str, Any]]:
        base = self._local(self.envs_root)
        if not base.is_dir():
            return []
        envs = []
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            envs.append({
                "name": child.name,
                "prefix": f"{self.envs_root}/{child.name}",
                "kind": "conda" if (child / "conda-meta").is_dir() else "venv",
                "python": "Python 3.11.9",
            })
        return envs

    def remove_prefix_env(self, name: str) -> None:
        shutil.rmtree(self._local(f"{self.envs_root}/{name}"), ignore_errors=True)

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
        # paramiko SFTP 는 기본 타임아웃이 없다. 전송이 죽으면 모든 호출이 영원히
        # 블록되고, 화면에서는 "작업을 눌러도 아무 반응이 없다"로 나타난다.
        channel = self.sftp.get_channel()
        if channel is not None:
            channel.settimeout(connection_module.SFTP_TIMEOUT_SECONDS)
        # SFTPClient 는 채널 하나에 요청 ID 를 붙여 쓰는 구조라 스레드 안전하지 않다.
        # 백엔드는 요청마다 워커 스레드를 쓰므로(예: 화면이 상세와 로그를 동시에 부른다)
        # 잠그지 않으면 두 스레드가 서로의 응답을 가져가 채널이 엉키고 타임아웃까지 멈춘다.
        # make_dir 이 upload_file/write_text 안에서 다시 호출되므로 재진입 가능해야 한다.
        self._sftp_lock = threading.RLock()
        self._home = posixpath.normpath(self.sftp.normalize("."))
        self.username = connection.username
        self.data_root = f"{connection.config.data_root}/{self.username}"

    @property
    def home(self) -> str:
        return self._home

    @property
    def jobs_root(self) -> str:
        return f"{self.data_root}/.seraph-gui/jobs"

    @property
    def datasets_root(self) -> str:
        """웹에서 올린 데이터셋이 들어가는 곳. 사용자 자기 NAS 폴더 안이다."""
        return f"{self.data_root}/datasets"

    @property
    def envs_root(self) -> str:
        """웹에서 만든 개인 conda 환경이 들어가는 곳.

        공용 설치(/data/opt/anaconda3)는 읽기 전용이라 거기에는 환경을 못 만든다.
        공용 conda 바이너리로 **prefix** 환경을 내 폴더에 만들면, 수 GB 짜리 개인
        anaconda3 를 따로 설치하지 않고도 원하는 파이썬 버전을 가질 수 있다.
        """
        return f"{self.data_root}/envs"

    @property
    def env_builds_root(self) -> str:
        """환경 빌드 스크립트와 로그. 환경 자체와 섞이면 목록에 잡힌다."""
        return f"{self.data_root}/.seraph-gui/envbuilds"

    def _guard_write(self, path: str, roots: list[str], message: str) -> str:
        clean = posixpath.normpath(path)
        for root in roots:
            root = posixpath.normpath(root)
            if clean == root or clean.startswith(root + "/"):
                return clean
        raise ApiError("REMOTE_PATH_NOT_ALLOWED", message, status_code=403)

    def _guard_user_path(self, path: str) -> str:
        """탐색기가 쓰는 경계 — 내 NAS 폴더 전체.

        작업·환경용 _guard_job_path 보다 넓다. 그쪽을 넓히지 않고 따로 둔 이유는,
        작업 코드에 경로 버그가 나더라도 여전히 좁은 네 루트를 벗어나지 못하게
        하려는 것이다. 탐색기는 사용자가 눈으로 보고 누르는 곳이라 다르다.

        더 넓히지는 않는다. /data/datasets 같은 공용 자리는 남의 것이고, 읽기는
        되지만 쓰기는 이 도구가 할 일이 아니다.
        """
        return self._guard_write(
            path,
            [self.data_root],
            f"{self.data_root} 아래에서만 파일을 만들거나 지울 수 있습니다.",
        )

    def _guard_removable(self, path: str) -> str:
        """지우거나 이름을 바꿔도 되는 곳인가.

        .seraph-gui 는 이 도구가 만들고 관리하는 폴더다. 사용자가 만든 적이 없으니
        지웠을 때 무엇을 잃는지도 알 수 없다 — 작업 기록 전체가 사라진다.
        """
        clean = self._guard_user_path(path)
        protected = {
            posixpath.normpath(self.data_root): "내 폴더 자체는 지울 수 없습니다.",
            posixpath.normpath(f"{self.data_root}/.seraph-gui"):
                "작업 기록 폴더(.seraph-gui)는 지울 수 없습니다. 작업 기록은 '내 작업'에서 하나씩 지우세요.",
        }
        if clean in protected:
            raise ApiError("REMOTE_PATH_PROTECTED", protected[clean], status_code=403)
        return clean

    def _guard_job_path(self, path: str) -> str:
        # 쓰기는 여전히 사용자 소유 폴더로만 제한한다. 데이터셋 업로드와 환경 빌드
        # 때문에 허용 루트가 늘었을 뿐, 넷 다 /data/<사용자> 아래고 /data 전체가
        # 열린 게 아니다.
        return self._guard_write(
            path,
            [self.jobs_root, self.datasets_root, self.envs_root, self.env_builds_root],
            "작업 파일은 사용자 작업·데이터·환경 폴더 아래에만 만들 수 있습니다.",
        )

    def list_entries(self, path: str, *, show_hidden: bool = False,
                     dirs_only: bool = False) -> dict[str, Any]:
        """디렉토리 내용을 나열한다(읽기 전용).

        경로를 눈 감고 타이핑하게 만들지 않으려고 만들었다. 쓰기와 달리 읽기는
        /data 아래 공용 데이터셋도 봐야 해서 사용자 폴더로 제한하지 않는다.
        서버가 권한을 강제하므로 못 읽는 곳은 그냥 실패한다.

        dirs_only 는 왼쪽 폴더 트리용이다. 트리에 파일까지 실어 보내면 데이터셋
        폴더 하나가 수만 개 항목을 끌고 온다.
        """
        clean = posixpath.normpath(path or self.data_root)
        with self._sftp_lock:
            try:
                attrs = self.sftp.listdir_attr(clean)
            except OSError as exc:
                raise ApiError(
                    "REMOTE_PATH_NOT_LISTABLE",
                    f"'{clean}' 를 열 수 없습니다. 경로나 권한을 확인하세요.",
                    status_code=404,
                ) from exc

        entries = []
        for a in attrs:
            hidden = a.filename.startswith(".")
            if hidden and not show_hidden:
                continue                      # 숨김 항목은 기본으로 감춘다(.seraph-gui 포함)
            mode = a.st_mode or 0
            is_link = stat.S_ISLNK(mode)
            is_dir = stat.S_ISDIR(mode)
            if is_link:
                # 심볼릭 링크는 st_mode 가 링크 자신을 가리킨다. 가리키는 대상이
                # 폴더면 폴더처럼 열 수 있어야 하므로 한 번 더 물어본다.
                is_dir = self._link_is_dir(posixpath.join(clean, a.filename))
            if dirs_only and not is_dir:
                continue
            entries.append({
                "name": a.filename,
                "path": posixpath.join(clean, a.filename),
                "is_dir": is_dir,
                "is_link": is_link,
                "hidden": hidden,
                "size": None if is_dir else int(a.st_size or 0),
                "mtime": int(a.st_mtime) if a.st_mtime else None,
                "is_archive": (not is_dir) and _is_supported_archive(a.filename),
            })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return {
            "path": clean,
            "parent": posixpath.dirname(clean) if clean != "/" else None,
            "entries": entries,
            "data_root": self.data_root,
            "datasets_root": self.datasets_root,
            "home": self._home,
        }

    # --- 탐색기 쓰기 ------------------------------------------------------
    # 이 도구의 첫 파괴적 기능이다. NAS 에는 휴지통이 없어서 한 번 지우면 끝이라,
    # 무엇이 사라지는지 먼저 세어서 보여주고 나서 지운다.

    def describe_target(self, path: str) -> dict[str, Any]:
        """지우기 전에 무엇이 사라지는지 센다.

        "정말 지울까요?" 는 아무 정보도 주지 않는다. 폴더 3개·파일 12개·1.4GB 라고
        말해줘야 사용자가 판단할 수 있다. 이 프로젝트의 원칙 그대로 — 추측하지 않는다.
        """
        clean = self._guard_removable(path)
        quoted = shlex.quote(clean)
        command = (
            f"p={quoted}; "
            'if [ -d "$p" ]; then '
            '  printf "dir=1\\n"; '
            '  printf "folders=%s\\n" "$(find "$p" -mindepth 1 -type d 2>/dev/null | wc -l)"; '
            '  printf "files=%s\\n" "$(find "$p" -mindepth 1 ! -type d 2>/dev/null | wc -l)"; '
            '  printf "bytes=%s\\n" "$(du -sb "$p" 2>/dev/null | cut -f1)"; '
            'else '
            '  printf "dir=0\\nfolders=0\\nfiles=1\\n"; '
            '  printf "bytes=%s\\n" "$(stat -c %s "$p" 2>/dev/null)"; '
            "fi"
        )
        raw = self.connection.run_command(
            command, label="삭제 대상 확인", timeout=60, check=False)
        parsed: dict[str, str] = {}
        for line in raw.splitlines():
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()
        as_int = lambda key: int(parsed[key]) if parsed.get(key, "").isdigit() else None  # noqa: E731
        return {
            "path": clean,
            "name": posixpath.basename(clean),
            "is_dir": parsed.get("dir") == "1",
            "folders": as_int("folders"),
            "files": as_int("files"),
            # du 가 없거나 느려서 실패할 수 있다. 모르면 모른다고 한다.
            "bytes": as_int("bytes"),
        }

    def make_folder(self, parent: str, name: str) -> dict[str, Any]:
        target = self._guard_user_path(posixpath.join(posixpath.normpath(parent), name))
        with self._sftp_lock:
            try:
                self.sftp.mkdir(target, mode=0o755)
            except OSError as exc:
                raise ApiError(
                    "REMOTE_MKDIR_FAILED",
                    f"'{name}' 폴더를 만들지 못했습니다. 같은 이름이 이미 있거나 권한이 없습니다.",
                    status_code=409,
                ) from exc
        return {"path": target, "name": name}

    def rename_entry(self, path: str, new_name: str) -> dict[str, Any]:
        clean = self._guard_removable(path)
        target = self._guard_user_path(posixpath.join(posixpath.dirname(clean), new_name))
        with self._sftp_lock:
            try:
                # posix_rename 을 쓰지 않는다. 그건 있는 파일을 조용히 덮어쓴다 —
                # 이름 바꾸기에서 남의 파일이 사라지면 안 된다.
                self.sftp.rename(clean, target)
            except OSError as exc:
                raise ApiError(
                    "REMOTE_RENAME_FAILED",
                    f"'{new_name}' 로 바꾸지 못했습니다. 같은 이름이 이미 있거나 권한이 없습니다.",
                    status_code=409,
                ) from exc
        return {"path": target, "name": new_name}

    def delete_entry(self, path: str) -> dict[str, Any]:
        clean = self._guard_removable(path)
        self.connection.run_command(
            f"rm -rf -- {shlex.quote(clean)}", label="삭제", timeout=300)
        return {"deleted": clean}

    def upload_into(self, local_path: str, remote_dir: str) -> dict[str, Any]:
        """내 PC 파일 하나를 지금 보고 있는 폴더에 올린다.

        데이터셋 업로드와 달리 압축 파일만 받지 않는다. 그 제한은 NAS IOPS 를
        지키려고 **데이터셋**에 건 것이지, 스크립트 한 장에 걸 이유가 없다.
        """
        source = pathlib.Path(local_path).expanduser()
        if not source.is_file():
            raise ApiError("LOCAL_FILE_NOT_FOUND", "선택한 파일을 찾을 수 없습니다.", 400)
        target = self._guard_user_path(
            posixpath.join(posixpath.normpath(remote_dir), source.name))
        self.make_dir_user(posixpath.dirname(target))
        with self._sftp_lock:
            self.sftp.put(str(source), target)
            self.sftp.chmod(target, 0o644)
        return {"path": target, "name": source.name, "size": source.stat().st_size}

    def make_dir_user(self, path: str) -> None:
        clean = self._guard_user_path(path)
        with self._sftp_lock:
            current = "/" if clean.startswith("/") else ""
            for part in clean.split("/"):
                if not part:
                    continue
                current = posixpath.join(current, part)
                try:
                    self.sftp.stat(current)
                except OSError:
                    self.sftp.mkdir(current, mode=0o755)

    def open_download(self, path: str) -> tuple[Any, dict[str, Any]]:
        """내 PC 로 내려받을 바이트 흐름을 연다. 파일은 그대로, 폴더는 tar.gz 로.

        **공유 SFTP 채널을 쓰지 않는다.** 그 채널은 잠금으로 직렬화돼 있어서
        (7043dd8), 몇 GB 짜리 체크포인트를 그걸로 내려받으면 전송이 끝날 때까지
        작업 상태도 목록도 전부 멈춘다. 다운로드마다 자기 채널을 연다.
        """
        clean = posixpath.normpath(path)
        with self._sftp_lock:
            try:
                info = self.sftp.stat(clean)
            except OSError as exc:
                raise ApiError(
                    "REMOTE_FILE_NOT_READABLE",
                    f"'{clean}' 를 읽을 수 없습니다. 경로나 권한을 확인하세요.",
                    status_code=404,
                ) from exc
        name = posixpath.basename(clean) or "download"
        if stat.S_ISDIR(info.st_mode or 0):
            return self._stream_archive(clean, name), {
                "filename": f"{name}.tar.gz",
                # tar 를 만들면서 흘려보내므로 최종 크기를 미리 알 수 없다.
                "size": None,
                "media_type": "application/gzip",
            }
        return self._stream_file(clean, int(info.st_size or 0)), {
            "filename": name,
            "size": int(info.st_size or 0),
            "media_type": "application/octet-stream",
        }

    def _stream_file(self, path: str, size: int) -> Any:
        sftp = self.connection.client.open_sftp()
        handle = sftp.file(path, "rb")
        # prefetch 가 없으면 왕복 지연 때문에 큰 파일이 몇 배로 느려진다.
        handle.prefetch(size)

        def chunks():
            try:
                while True:
                    block = handle.read(DOWNLOAD_CHUNK_BYTES)
                    if not block:
                        return
                    yield block
            finally:
                try:
                    handle.close()
                finally:
                    sftp.close()
        return chunks()

    def _stream_archive(self, path: str, name: str) -> Any:
        # 서버에 tar 파일을 만들지 않는다. 만들면 남의 NAS 에 쓰레기를 쌓고,
        # 도중에 그만두면 그게 그대로 남는다. 만들면서 바로 흘려보낸다.
        parent = posixpath.dirname(path) or "/"
        command = f"tar -czf - -C {shlex.quote(parent)} -- {shlex.quote(name)}"
        _, stdout, _ = self.connection.client.exec_command(command, timeout=None)

        def chunks():
            try:
                while True:
                    block = stdout.read(DOWNLOAD_CHUNK_BYTES)
                    if not block:
                        return
                    yield block
            finally:
                try:
                    stdout.channel.close()
                except Exception:                # noqa: BLE001 - 이미 닫혔으면 그만이다
                    pass
        return chunks()

    def _link_is_dir(self, path: str) -> bool:
        with self._sftp_lock:
            try:
                return stat.S_ISDIR(self.sftp.stat(path).st_mode or 0)
            except OSError:
                return False              # 끊어진 링크. 파일처럼 두면 열려다 실패한다

    def preview_file(self, path: str, max_bytes: int = PREVIEW_MAX_BYTES) -> dict[str, Any]:
        """파일 앞부분을 텍스트로 보여준다.

        결과 파일 한 줄 보려고 터미널을 여는 일을 없애려는 것이다. 목록과 마찬가지로
        사용자 폴더로 제한하지 않는다 — 어차피 SSH 로 접속한 본인이 읽을 수 있는
        것만 서버가 내주고, 못 읽는 곳은 그냥 실패한다.
        """
        clean = posixpath.normpath(path)
        with self._sftp_lock:
            try:
                info = self.sftp.stat(clean)
                if stat.S_ISDIR(info.st_mode or 0):
                    raise ApiError("REMOTE_PATH_IS_DIR", "폴더는 미리 볼 수 없습니다.", 400)
                with self.sftp.file(clean, "rb") as handle:
                    raw = handle.read(max_bytes + 1)
            except ApiError:
                raise
            except OSError as exc:
                raise ApiError(
                    "REMOTE_FILE_NOT_READABLE",
                    f"'{clean}' 를 읽을 수 없습니다. 경로나 권한을 확인하세요.",
                    status_code=404,
                ) from exc
        return _preview_payload(clean, raw, int(info.st_size or 0), max_bytes)

    def upload_dataset(self, local_path: str) -> dict[str, Any]:
        """로컬 압축 파일을 사용자 NAS 데이터 폴더로 올린다."""
        source = pathlib.Path(local_path).expanduser()
        if not source.is_file():
            raise ApiError("LOCAL_FILE_NOT_FOUND", "선택한 파일을 찾을 수 없습니다.", 400)
        if not _is_supported_archive(source.name):
            raise ApiError(
                "DATASET_ARCHIVE_REQUIRED",
                "NAS IOPS 보호를 위해 TAR, TAR.GZ, TGZ 또는 ZIP 파일만 올릴 수 있습니다.",
                400,
            )
        target = f"{self.datasets_root}/{source.name}"
        self.upload_file(str(source), target)
        return {"path": target, "name": source.name, "size": source.stat().st_size}

    def find_conda(self) -> dict[str, Any]:
        """쓸 수 있는 conda 설치와 환경을 찾는다.

        예전에는 /data/<user>/anaconda3 하나만 가정해서, 그게 없으면 공용
        설치(/data/opt/anaconda3)가 있어도 환경을 고를 방법이 없었다.
        """
        candidates = [
            f"{self.data_root}/anaconda3",
            f"{self.data_root}/miniconda3",
            f"{self.connection.config.data_root}/opt/anaconda3",
            f"{self._home}/anaconda3",
            f"{self._home}/miniconda3",
        ]
        seen, installs = set(), []
        for root in candidates:
            root = posixpath.normpath(root)
            if root in seen:
                continue
            seen.add(root)
            quoted = shlex.quote(root)
            command = (
                f"r={quoted}; "
                'if [ -r "$r/etc/profile.d/conda.sh" ]; then '
                '  echo OK; ls -1 "$r/envs" 2>/dev/null; '
                "fi"
            )
            try:
                raw = self.connection.run_command(command, label="conda 확인", timeout=15)
            except Exception:
                continue
            lines = [x.strip() for x in raw.splitlines() if x.strip()]
            if not lines or lines[0] != "OK":
                continue
            installs.append({
                "root": root,
                "conda_sh": f"{root}/etc/profile.d/conda.sh",
                "is_personal": root.startswith(self.data_root + "/") or root.startswith(self._home + "/"),
                "envs": lines[1:],
            })
        return {"installs": installs}

    # --- 개인 환경 만들기 -------------------------------------------------
    # 공용 설치는 읽기 전용이라 pip 하나 넣을 수 없다. 여기 있는 것들은 공용 conda
    # 바이너리를 읽기만 하고, 쓰기는 전부 /data/<사용자>/envs 아래로 간다.

    def run_script(self, script: str, *, label: str, timeout: int = 60) -> str:
        """스크립트를 파일로 올린 뒤 실행한다.

        긴 셸 명령을 문자열로 이어 붙이면 따옴표가 세 겹으로 중첩되면서, 고치는
        사람이 무엇이 실행되는지 읽을 수 없게 된다. 파일로 올리면 그냥 셸 스크립트다.
        """
        # 요청마다 다른 이름을 쓴다. 고정 이름이면 화면 두 곳이 동시에 점검할 때
        # 서로의 스크립트를 덮어써서 엉뚱한 걸 실행한다.
        path = f"{self.env_builds_root}/_run-{uuid.uuid4().hex[:8]}.sh"
        self.write_text(path, script)
        quoted = shlex.quote(path)
        # 실행 후 지운다. 점검은 화면을 열 때마다 도는데, 남겨두면 사용자 NAS 에
        # 쓰레기 파일이 쌓인다.
        return self.connection.run_command(
            f"bash {quoted}; rm -f -- {quoted}", label=label, timeout=timeout, check=False)

    def probe_env_tools(self, conda_root: str | None) -> dict[str, Any]:
        """이 서버에서 환경을 만들 수 있는지 사실만 모아 온다. 아무것도 바꾸지 않는다.

        추측하면 "만들기" 버튼을 눌렀을 때 20분 뒤에 실패한다. 네트워크가 막혀
        있는지, 디스크가 남는지, conda 가 실행은 되는지 먼저 물어본다.
        """
        script = _PROBE_SCRIPT.format(
            conda_root=shlex.quote(conda_root or ""),
            data_root=shlex.quote(self.data_root),
        )
        raw = self.run_script(script, label="환경 점검", timeout=60)
        info: dict[str, Any] = {"network": {}}
        for line in raw.splitlines():
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key == "net":
                url, _, code = value.rpartition(" ")
                info["network"][url] = int(code) if code.isdigit() else 0
            elif key:
                info[key] = value
        avail = info.get("avail_kb", "")
        info["avail_bytes"] = int(avail) * 1024 if avail.isdigit() else None
        info["writable"] = info.get("writable") == "1"
        return info

    def build_dir(self, build_id: str) -> str:
        return f"{self.env_builds_root}/{build_id}"

    def start_env_build(self, build_id: str, script: str, spec: dict[str, Any]) -> None:
        """빌드를 백그라운드로 띄운다. conda solve 는 20분까지 걸린다.

        HTTP 요청 안에서 기다릴 수 없고, SSH 채널이 닫혀도 죽으면 안 된다.
        그래서 nohup 으로 떼어놓고, 진행 상황은 로그 파일을 tail 해서 본다.
        """
        directory = self.build_dir(build_id)
        self.write_text(f"{directory}/spec.json", json.dumps(spec, ensure_ascii=False))
        self.write_text(f"{directory}/build.sh", script)
        quoted = shlex.quote(directory)
        # stdin 을 /dev/null 로 돌리고 stdout/stderr 를 파일로 보내지 않으면
        # paramiko 가 채널 EOF 를 기다리며 이 호출 자체가 블록된다.
        command = (
            f"cd {quoted} && "
            "{ nohup bash build.sh > build.log 2>&1 < /dev/null & } && "
            'echo "$!" > pid'
        )
        self.connection.run_command(command, label="환경 빌드 시작", timeout=30)

    def background_state(self, build_id: str) -> dict[str, Any]:
        """빌드 상태. rc 파일이 있으면 끝난 것이고, 없으면 PID 가 살아있는지 본다.

        rc 파일만 보면 프로세스가 죽었을 때(노드 재시작 등) 영원히 '진행 중'으로
        남는다. 끝나지 않는 진행 표시줄은 오류 메시지보다 나쁘다.
        """
        directory = shlex.quote(self.build_dir(build_id))
        command = (
            f"d={directory}; "
            'printf "rc=%s\\n" "$(cat "$d/rc" 2>/dev/null)"; '
            'p=$(cat "$d/pid" 2>/dev/null); '
            'if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then a=1; else a=0; fi; '
            'printf "alive=%s\\n" "$a"'
        )
        raw = self.connection.run_command(
            command, label="환경 빌드 상태", timeout=20, check=False)
        parsed = {}
        for line in raw.splitlines():
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()
        return {
            "rc": int(parsed["rc"]) if parsed.get("rc", "").lstrip("-").isdigit() else None,
            "alive": parsed.get("alive") == "1",
            "log": self.tail(f"{self.build_dir(build_id)}/build.log"),
        }

    def list_prefix_envs(self) -> list[dict[str, Any]]:
        """/data/<사용자>/envs 아래의 환경들. conda-meta 가 있어야 진짜 환경이다."""
        root = shlex.quote(self.envs_root)
        command = (
            f"root={root}; [ -d \"$root\" ] || exit 0; "
            'for d in "$root"/*; do '
            '  [ -d "$d/conda-meta" ] || [ -x "$d/bin/python" ] || continue; '
            '  v=$("$d/bin/python" --version 2>&1 | tr -d "\\r"); '
            '  if [ -d "$d/conda-meta" ]; then k=conda; else k=venv; fi; '
            '  printf "%s\\t%s\\t%s\\n" "$(basename "$d")" "$k" "$v"; '
            "done"
        )
        raw = self.connection.run_command(
            command, label="개인 환경 목록", timeout=30, check=False)
        envs = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or not parts[0]:
                continue
            envs.append({
                "name": parts[0],
                "prefix": f"{self.envs_root}/{parts[0]}",
                "kind": parts[1],
                "python": parts[2] or None,
            })
        return sorted(envs, key=lambda e: e["name"].lower())

    def remove_prefix_env(self, name: str) -> None:
        """내가 만든 환경만 지운다. 공용 설치는 이 경로에 있을 수 없다."""
        target = self._guard_write(
            f"{self.envs_root}/{name}",
            [self.envs_root],
            "직접 만든 환경만 삭제할 수 있습니다.",
        )
        if target == posixpath.normpath(self.envs_root):
            raise ApiError("REMOTE_PATH_NOT_ALLOWED", "환경 폴더 전체는 지울 수 없습니다.", 403)
        self.connection.run_command(
            f"rm -rf -- {shlex.quote(target)}", label="환경 삭제", timeout=120)

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
        with self._sftp_lock:
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
        with self._sftp_lock:
            self.sftp.put(local_path, target)
            self.sftp.chmod(target, 0o600)

    def write_text(self, remote_path: str, text: str) -> None:
        target = self._guard_job_path(remote_path)
        self.make_dir(posixpath.dirname(target))
        # 임시 이름에 난수를 넣는다. 고정 이름이면 같은 파일을 동시에 쓰는 두 흐름이
        # 서로의 임시 파일을 가져가, 한쪽이 "파일이 없다"로 죽는다.
        tmp = f"{target}.{uuid.uuid4().hex[:8]}.tmp"
        with self._sftp_lock:
            with self.sftp.file(tmp, "w") as handle:
                handle.write(text)
                handle.flush()
            self.sftp.chmod(tmp, 0o600)
            try:
                # posix_rename 은 기존 파일 위에 원자적으로 덮어쓴다. 예전처럼
                # remove 후 rename 하면 그 사이 아주 짧게 파일이 **없는** 순간이
                # 생기고, 그때 job.json 을 읽은 쪽에는 작업이 사라진 것으로 보인다.
                self.sftp.posix_rename(tmp, target)
            except (OSError, AttributeError):
                # 서버가 posix-rename 확장을 지원하지 않으면 예전 방식으로 되돌린다.
                try:
                    self.sftp.remove(target)
                except OSError:
                    pass
                self.sftp.rename(tmp, target)

    def read_text(self, remote_path: str, max_bytes: int = 1_000_000) -> str:
        target = self._guard_job_path(remote_path)
        with self._sftp_lock:
            with self.sftp.file(target, "rb") as handle:
                return handle.read(max_bytes).decode("utf-8", errors="replace")

    def list_directories(self, root: str) -> list[str]:
        clean = self._guard_job_path(root)
        with self._sftp_lock:
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
        with self._sftp_lock:
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

    def delete_job_dir(self, path: str) -> None:
        """작업 폴더 하나를 지운다(메타데이터·업로드한 코드·로그).

        삭제는 데이터셋 폴더까지 허용하는 _guard_job_path 를 쓰지 않는다. 실수로
        데이터를 날리지 않게 작업 폴더 아래로만 막고, 작업 루트 자체도 거부한다.
        """
        root = posixpath.normpath(self.jobs_root)
        clean = self._guard_write(path, [root], "작업 폴더만 삭제할 수 있습니다.")
        if clean == root:
            raise ApiError("REMOTE_PATH_NOT_ALLOWED", "작업 폴더 전체는 지울 수 없습니다.", 403)
        self.connection.run_command(
            f"rm -rf -- {shlex.quote(clean)}", label="작업 삭제", timeout=30)

    def job_state(self, job_id: str) -> dict[str, Any] | None:
        if not _JOB_ID.fullmatch(job_id):
            raise ApiError("INVALID_REQUEST", "Slurm 작업 ID 형식이 올바르지 않습니다.", 422)
        # 끝난 job 은 Slurm 이 큐에서 치우고, 그때부터 squeue 는 rc=1
        # "Invalid job id specified" 를 낸다. 오류가 아니라 "이미 끝났다"는 뜻이므로
        # 빈 결과로 받아 아래 sacct 조회로 넘어간다.
        queue = self.connection.run_command(
            f'squeue -h -j {job_id} -o "%T|%R|%N|%M"',
            label="작업 상태",
            timeout=15,
            check=False,
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

        # 회계 DB(slurmdbd)는 우리 통제 밖이고 실제로 가끔 죽는다. 실측 사례:
        #   sacct: error: failed to open persistent connection to ariel-master:6819
        #   sacct: error: Problem talking to the database: Connection timed out
        # 그때 예외를 던지면 작업 상세가 통째로 500 이 된다 — 정작 메타데이터와 로그는
        # 멀쩡한데도. 최신 상태만 포기하고(None) 저장된 상태를 그대로 보여주는 게 낫다.
        accounting = self.connection.run_command(
            f'sacct -n -P -j {job_id} -X -o "JobIDRaw,State,ExitCode,Elapsed,NodeList"',
            label="완료 작업 상태",
            timeout=30,
            check=False,
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
