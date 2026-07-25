"""검증 → 업로드 → 제출 → 모니터링 전체 작업 흐름."""

from __future__ import annotations

import json
import os
import pathlib
import posixpath
import secrets
import shlex
import stat
import tarfile
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from pydantic import ValidationError

from seraph import sbatch, services
from seraph.parsers.testonly import parse_test_only

from .dependencies import ConnectionManager
from .errors import ApiError
from .remote import MockRemote, SSHRemote, _is_supported_archive, read_job_json
from .schemas import JobSpec

_ARCHIVE_EXCLUDES = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
_MAX_CODE_BYTES = 2 * 1024 * 1024 * 1024
_FINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"}
_ACTIVE_STATES = {"SUBMITTED", "PENDING", "RUNNING", "COMPLETING", "CANCEL_REQUESTED"}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normal_member(name: str) -> str | None:
    clean = posixpath.normpath(name.replace("\\", "/"))
    if clean in ("", "."):
        return None
    if clean.startswith("/") or clean == ".." or clean.startswith("../"):
        raise ValueError("압축파일에 상위 폴더로 이동하는 경로가 있습니다.")
    return clean.lstrip("./")


def _is_under(path: str, root: str) -> bool:
    clean = posixpath.normpath(path)
    root = posixpath.normpath(root)
    return clean == root or clean.startswith(root + "/")


# 판별 규칙은 remote 쪽과 하나로 유지한다(업로드와 검증이 어긋나면 안 된다).
_is_supported_dataset_archive = _is_supported_archive


def _public_prediction(raw: str) -> dict[str, Any]:
    prediction = parse_test_only(raw)
    return {
        "ok": prediction.ok,
        "start": prediction.start,
        "node": prediction.node,
        "partition": prediction.partition,
        "reason": prediction.reason,
    }


class JobService:
    def __init__(self, manager: ConnectionManager):
        self.manager = manager

    @property
    def remote(self) -> MockRemote | SSHRemote:
        return self.manager.require_remote()

    @property
    def connection(self) -> Any:
        return self.manager.require_connection()

    def _inspect_local_code(self, spec: JobSpec) -> dict[str, Any]:
        source = pathlib.Path(spec.local_code_path).expanduser()
        try:
            source = source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ApiError(
                "LOCAL_CODE_NOT_FOUND",
                "선택한 로컬 코드 경로를 찾을 수 없습니다.",
                status_code=422,
            ) from exc

        if not os.access(source, os.R_OK):
            raise ApiError("LOCAL_CODE_NOT_READABLE", "선택한 코드를 읽을 수 없습니다.", 422)

        if source.is_dir():
            entry = source / pathlib.PurePosixPath(spec.entrypoint)
            if not entry.is_file():
                raise ApiError(
                    "ENTRYPOINT_NOT_FOUND",
                    f"코드 폴더에서 {spec.entrypoint} 파일을 찾을 수 없습니다.",
                    status_code=422,
                )
            total = 0
            files = 0
            for path in source.rglob("*"):
                rel = path.relative_to(source)
                if any(part in _ARCHIVE_EXCLUDES for part in rel.parts):
                    continue
                if path.is_symlink():
                    raise ApiError(
                        "SYMLINK_NOT_ALLOWED",
                        "코드 폴더의 심볼릭 링크는 업로드할 수 없습니다.",
                        status_code=422,
                    )
                if path.is_file():
                    total += path.stat().st_size
                    files += 1
                    if total > _MAX_CODE_BYTES:
                        raise ApiError("CODE_TOO_LARGE", "코드 업로드 크기가 2GB를 넘습니다.", 422)
            return {
                "source": source,
                "kind": "tar.gz",
                "source_type": "directory",
                "files": files,
                "bytes": total,
                "display_name": source.name,
            }

        suffixes = "".join(source.suffixes).lower()
        if suffixes == ".zip":
            names = self._validate_zip(source)
            kind = "zip"
        elif suffixes in (".tar.gz", ".tgz"):
            names = self._validate_tar(source)
            kind = "tar.gz"
        elif source.suffix.lower() == ".py":
            if spec.entrypoint != source.name:
                raise ApiError(
                    "ENTRYPOINT_NOT_FOUND",
                    f"단일 파일을 선택했으므로 진입 파일은 {source.name}이어야 합니다.",
                    422,
                )
            return {
                "source": source,
                "kind": "tar.gz",
                "source_type": "single_file",
                "files": 1,
                "bytes": source.stat().st_size,
                "display_name": source.name,
            }
        else:
            raise ApiError(
                "UNSUPPORTED_CODE_FORMAT",
                "코드 폴더, Python 파일, ZIP 또는 TAR.GZ만 선택할 수 있습니다.",
                status_code=422,
            )

        if spec.entrypoint not in names:
            raise ApiError(
                "ENTRYPOINT_NOT_FOUND",
                f"압축파일에서 {spec.entrypoint} 파일을 찾을 수 없습니다.",
                status_code=422,
            )
        size = source.stat().st_size
        if size > _MAX_CODE_BYTES:
            raise ApiError("CODE_TOO_LARGE", "코드 업로드 크기가 2GB를 넘습니다.", 422)
        return {
            "source": source,
            "kind": kind,
            "source_type": "archive",
            "files": len(names),
            "bytes": size,
            "display_name": source.name,
        }

    @staticmethod
    def _validate_zip(source: pathlib.Path) -> set[str]:
        try:
            with zipfile.ZipFile(source) as archive:
                names: set[str] = set()
                for member in archive.infolist():
                    name = _normal_member(member.filename)
                    if name is None:
                        continue
                    mode = (member.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        raise ValueError("압축파일의 심볼릭 링크는 허용하지 않습니다.")
                    if not member.is_dir():
                        names.add(name)
                return names
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            raise ApiError("UNSAFE_ARCHIVE", str(exc) or "ZIP 파일을 읽을 수 없습니다.", 422) from exc

    @staticmethod
    def _validate_tar(source: pathlib.Path) -> set[str]:
        try:
            with tarfile.open(source, "r:*") as archive:
                names: set[str] = set()
                for member in archive.getmembers():
                    name = _normal_member(member.name)
                    if name is None:
                        continue
                    if member.issym() or member.islnk() or member.isdev():
                        raise ValueError("압축파일의 링크와 장치 파일은 허용하지 않습니다.")
                    if member.isfile():
                        names.add(name)
                return names
        except (OSError, tarfile.TarError, ValueError) as exc:
            raise ApiError("UNSAFE_ARCHIVE", str(exc) or "TAR.GZ 파일을 읽을 수 없습니다.", 422) from exc

    @contextmanager
    def _package_code(self, info: dict[str, Any]) -> Iterator[tuple[str, str]]:
        source: pathlib.Path = info["source"]
        if info["source_type"] == "archive":
            upload_name = "code.zip" if info["kind"] == "zip" else "code.tar.gz"
            yield str(source), upload_name
            return

        with tempfile.TemporaryDirectory(prefix="seraph-gui-code-") as tmp:
            target = pathlib.Path(tmp) / "code.tar.gz"
            with tarfile.open(target, "w:gz") as archive:
                if source.is_file():
                    archive.add(source, arcname=source.name, recursive=False)
                else:
                    for path in sorted(source.rglob("*")):
                        rel = path.relative_to(source)
                        if any(part in _ARCHIVE_EXCLUDES for part in rel.parts):
                            continue
                        if path.is_file() and not path.is_symlink():
                            archive.add(path, arcname=rel.as_posix(), recursive=False)
            yield str(target), "code.tar.gz"

    def validate(self, spec: JobSpec, snapshot: Any) -> dict[str, Any]:
        code = self._inspect_local_code(spec)
        dataset = self.remote.path_info(spec.dataset_path)
        output = self.remote.path_info(spec.output_path)
        problems: list[dict[str, str]] = []

        if not dataset.exists:
            problems.append({"level": "block", "code": "DATASET_NOT_FOUND", "message": "NAS 데이터 경로가 존재하지 않습니다."})
        elif not dataset.readable:
            problems.append({"level": "block", "code": "DATASET_NOT_READABLE", "message": "NAS 데이터 경로를 읽을 권한이 없습니다."})

        if posixpath.normpath(spec.dataset_path) in {"/", self.remote.data_root, self.remote.connection.config.data_root}:
            problems.append({"level": "block", "code": "DATASET_PATH_TOO_BROAD", "message": "데이터의 실제 파일 또는 폴더 경로까지 지정하세요."})

        if not _is_under(spec.dataset_path, self.remote.connection.config.data_root):
            problems.append({
                "level": "block",
                "code": "DATASET_NOT_ON_NAS",
                "message": "튜토리얼 기준 NAS 데이터 경로인 /data/...를 입력하세요.",
            })
        if not spec.copy_dataset_to_local:
            problems.append({
                "level": "block",
                "code": "NAS_LOCAL_COPY_REQUIRED",
                "message": "/data 데이터는 GPU 노드의 /local_datasets로 복사한 뒤 사용해야 합니다.",
            })
        if dataset.exists and (not dataset.is_file or not _is_supported_dataset_archive(spec.dataset_path)):
            problems.append({
                "level": "block",
                "code": "DATASET_ARCHIVE_REQUIRED",
                "message": "NAS IOPS 보호를 위해 TAR, TAR.GZ, TGZ 또는 ZIP 데이터 파일을 지정하세요.",
            })

        if output.exists and not output.is_dir:
            problems.append({"level": "block", "code": "OUTPUT_NOT_DIRECTORY", "message": "결과 경로가 기존 파일과 겹칩니다."})
        elif output.exists and not output.writable:
            problems.append({"level": "block", "code": "OUTPUT_NOT_WRITABLE", "message": "결과 경로에 쓸 권한이 없습니다."})
        elif not output.exists and not output.parent_writable:
            problems.append({"level": "block", "code": "OUTPUT_PARENT_NOT_WRITABLE", "message": "결과 폴더를 만들 권한이 없습니다."})

        if not _is_under(spec.output_path, self.remote.data_root):
            problems.append({
                "level": "block",
                "code": "OUTPUT_OUTSIDE_USER_DATA",
                "message": f"결과 경로는 {self.remote.data_root}/... 아래여야 합니다.",
            })
        if posixpath.normpath(spec.output_path) in {"/", self.remote.connection.config.data_root, self.remote.data_root, "/home", self.remote.home}:
            problems.append({"level": "block", "code": "OUTPUT_PATH_TOO_BROAD", "message": "결과를 저장할 하위 폴더까지 지정하세요."})

        partition = spec.partition or snapshot.default_partition
        direct_paths = [] if spec.copy_dataset_to_local else [spec.dataset_path]
        built = sbatch.generate_sbatch(
            snapshot,
            name=spec.name,
            command="true",
            partition=partition,
            gpus=spec.gpus,
            high_perf=spec.high_perf,
            cpus=spec.cpus,
            mem=spec.memory,
            time_limit=spec.time_limit,
            node=spec.node,
            paths=direct_paths,
        )
        seen = {(p["level"], p["code"]) for p in problems}
        for problem in built["lint"]["problems"]:
            key = (problem["level"], problem["code"])
            if key not in seen:
                problems.append(problem)
                seen.add(key)

        public_code = {key: value for key, value in code.items() if key != "source"}
        return {
            "ok": not any(problem["level"] == "block" for problem in problems),
            "problems": problems,
            "code": public_code,
            "dataset": dataset.to_dict(),
            "output": output.to_dict(),
            "resolved": {
                "partition": partition,
                "node": built.get("node"),
                "auto_selected_node": built.get("auto_selected_node", False),
                "copy_dataset_to_local": spec.copy_dataset_to_local,
            },
        }

    @staticmethod
    def _argument_token(value: str) -> str:
        if value == "{dataset}":
            return '"$SERAPH_DATASET_PATH"'
        if value == "{output}":
            return '"$SERAPH_OUTPUT_PATH"'
        return shlex.quote(value)

    def _payload_command(self, meta: dict[str, Any]) -> str:
        job_dir = shlex.quote(meta["remote_dir"])
        archive = shlex.quote(meta["code_archive_name"])
        dataset = shlex.quote(meta["dataset_path"])
        output = shlex.quote(meta["output_path"])
        entrypoint = shlex.quote(meta["entrypoint"])
        args = " ".join(self._argument_token(arg) for arg in meta.get("arguments", []))

        lines = [
            "set -Eeuo pipefail",
            "umask 077",
            f"JOB_DIR={job_dir}",
            f"DATASET_SOURCE={dataset}",
            f"OUTPUT_DEST={output}",
            f'LOCAL_ROOT="{self.remote.connection.config.local_datasets_root}/{meta["username"]}/seraph-gui-$SLURM_JOB_ID"',
            'CODE_DIR="$LOCAL_ROOT/code"',
            'DATA_DIR="$LOCAL_ROOT/data"',
            'SERAPH_OUTPUT_PATH="$LOCAL_ROOT/output"',
            "finish_job() {",
            "  rc=$?",
            "  set +e",
            '  mkdir -p "$OUTPUT_DEST"',
            '  cp -a "$SERAPH_OUTPUT_PATH"/. "$OUTPUT_DEST"/ 2>/dev/null',
            '  rm -rf "$LOCAL_ROOT"',
            "  exit \"$rc\"",
            "}",
            "trap finish_job EXIT",
            'mkdir -p "$CODE_DIR" "$DATA_DIR" "$SERAPH_OUTPUT_PATH"',
            'chmod 700 "$LOCAL_ROOT"',
            f'cp "$JOB_DIR"/{archive} "$LOCAL_ROOT"/{archive}',
        ]

        if meta["code_archive_kind"] == "zip":
            lines.append(f'unzip -q "$LOCAL_ROOT"/{archive} -d "$CODE_DIR"')
        else:
            lines.append(f'tar -xzf "$LOCAL_ROOT"/{archive} -C "$CODE_DIR"')

        if meta["copy_dataset_to_local"]:
            lines.extend([
                'DATA_ARCHIVE="$LOCAL_ROOT/$(basename -- "$DATASET_SOURCE")"',
                'cp "$DATASET_SOURCE" "$DATA_ARCHIVE"',
                'case "$DATA_ARCHIVE" in',
                '  *.tar.gz|*.tgz) tar -xzf "$DATA_ARCHIVE" -C "$DATA_DIR" ;;',
                '  *.tar) tar -xf "$DATA_ARCHIVE" -C "$DATA_DIR" ;;',
                '  *.zip) unzip -q "$DATA_ARCHIVE" -d "$DATA_DIR" ;;',
                '  *) echo "지원하지 않는 데이터 압축 형식입니다." >&2; exit 2 ;;',
                'esac',
                'SERAPH_DATASET_PATH="$DATA_DIR"',
            ])
        else:
            lines.append('SERAPH_DATASET_PATH="$DATASET_SOURCE"')

        if meta.get("conda_env"):
            env = shlex.quote(meta["conda_env"])
            conda_sh = shlex.quote(self._conda_sh(meta))
            lines.extend([
                f'[ -r {conda_sh} ] || {{ echo "개인 Conda 초기화 파일을 찾을 수 없습니다: {conda_sh}" >&2; exit 127; }}',
                f'. {conda_sh}',
                f"conda activate {env}",
            ])

        run = f"python -u {entrypoint}"
        if args:
            run += " " + args
        lines.extend([
            "export SERAPH_DATASET_PATH SERAPH_OUTPUT_PATH",
            'cd "$CODE_DIR"',
            run,
        ])
        return "\n".join(lines)

    def _resolve_conda_root(self, conda_env: str | None) -> str | None:
        """이 환경 이름을 가진 conda 설치를 찾는다. 개인 설치를 공용보다 우선한다.

        환경을 안 쓰면 None. 못 찾으면 None 을 돌려주고 _conda_sh 가 예전 기본값으로
        떨어진다 — 그 경우 job 이 "conda 초기화 파일을 찾을 수 없습니다" 로 명확히 죽는다.
        """
        if not conda_env:
            return None
        try:
            found = self.remote.find_conda()
        except Exception:
            return None
        installs = found.get("installs", [])
        for install in sorted(installs, key=lambda i: not i.get("is_personal")):
            if conda_env in (install.get("envs") or []):
                return install["root"]
        return installs[0]["root"] if installs else None

    def _conda_sh(self, meta: dict[str, Any]) -> str:
        """이 job 이 source 할 conda.sh 경로.

        예전에는 개인 설치(/data/<user>/anaconda3)만 가정해서, 그게 없으면 공용
        설치(/data/opt/anaconda3)에 환경이 있어도 쓸 수 없었다. 이제 준비 단계에서
        실제로 찾은 설치 경로를 meta 에 적어두고 그걸 쓴다.
        """
        root = meta.get("conda_root")
        if root:
            return f"{root}/etc/profile.d/conda.sh"
        return f"{self.remote.data_root}/anaconda3/etc/profile.d/conda.sh"

    def _preflight_script(self, meta: dict[str, Any]) -> str:
        job_dir = shlex.quote(meta["remote_dir"])
        archive = shlex.quote(meta["code_archive_name"])
        dataset = shlex.quote(meta["dataset_path"])
        entrypoint = shlex.quote(meta["entrypoint"])
        local_root = self.remote.connection.config.local_datasets_root
        lines = [
            "#!/usr/bin/bash",
            "set -Eeuo pipefail",
            f"JOB_DIR={job_dir}",
            f"DATASET_SOURCE={dataset}",
            f'LOCAL_ROOT="{local_root}/{meta["username"]}/seraph-gui-preflight-$SLURM_JOB_ID"',
            'CODE_DIR="$LOCAL_ROOT/code"',
            'cleanup() { rm -rf "$LOCAL_ROOT"; }',
            "trap cleanup EXIT",
            'mkdir -p "$CODE_DIR"',
            'chmod 700 "$LOCAL_ROOT"',
            'test -r "$DATASET_SOURCE"',
            'test -f "$DATASET_SOURCE"',
            f'cp "$JOB_DIR"/{archive} "$LOCAL_ROOT"/{archive}',
        ]
        if meta["code_archive_kind"] == "zip":
            lines.append(f'unzip -q "$LOCAL_ROOT"/{archive} -d "$CODE_DIR"')
        else:
            lines.append(f'tar -xzf "$LOCAL_ROOT"/{archive} -C "$CODE_DIR"')
        lines.append(f'test -f "$CODE_DIR"/{entrypoint}')
        if meta.get("conda_env"):
            env = shlex.quote(meta["conda_env"])
            conda_sh = shlex.quote(self._conda_sh(meta))
            lines.extend([
                f"test -r {conda_sh}",
                f". {conda_sh}",
                f"conda activate {env}",
            ])
        lines.extend([
            "nvidia-smi -L",
            "python --version",
            'echo "SERAPH srun preflight OK"',
        ])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _debug_partition(partition: str) -> str:
        return "debug_" + partition[len("batch_"):] if partition.startswith("batch_") else partition

    def _build_script(self, snapshot: Any, meta: dict[str, Any]) -> dict[str, Any]:
        try:
            staged = JobSpec(
                name=meta["job_name"],
                local_code_path="staged-code",
                entrypoint=meta["entrypoint"],
                arguments=meta.get("arguments", []),
                dataset_path=meta["dataset_path"],
                output_path=meta["output_path"],
                copy_dataset_to_local=meta["copy_dataset_to_local"],
                partition=meta["partition"],
                gpus=meta["gpus"],
                high_perf=meta["high_perf"],
                cpus=meta["cpus"],
                memory=meta["memory"],
                time_limit=meta["time_limit"],
                node=meta.get("node"),
                conda_env=meta.get("conda_env"),
            )
        except (KeyError, ValidationError, TypeError) as exc:
            raise ApiError(
                "JOB_METADATA_INVALID",
                "저장된 작업 설정이 올바르지 않아 다시 준비해야 합니다.",
                status_code=422,
            ) from exc

        expected_archive = {"zip": "code.zip", "tar.gz": "code.tar.gz"}.get(
            meta.get("code_archive_kind")
        )
        if expected_archive is None or meta.get("code_archive_name") != expected_archive:
            raise ApiError(
                "JOB_METADATA_INVALID",
                "저장된 코드 패키지 정보가 올바르지 않습니다.",
                status_code=422,
            )

        # 아래 생성기는 이 재검증된 값만 사용한다.
        meta.update({
            # job.json은 사용자가 서버에서 수정할 수 있으므로 계정명은 현재 SSH
            # 세션에서 다시 가져온 값만 신뢰한다.
            "username": self.remote.username,
            "job_name": staged.name,
            "entrypoint": staged.entrypoint,
            "arguments": staged.arguments,
            "dataset_path": staged.dataset_path,
            "output_path": staged.output_path,
            "copy_dataset_to_local": staged.copy_dataset_to_local,
            "partition": staged.partition,
            "gpus": staged.gpus,
            "high_perf": staged.high_perf,
            "cpus": staged.cpus,
            "memory": staged.memory,
            "time_limit": staged.time_limit,
            "node": staged.node,
            "conda_env": staged.conda_env,
        })
        direct_paths = [] if meta["copy_dataset_to_local"] else [meta["dataset_path"]]
        built = sbatch.generate_sbatch(
            snapshot,
            name=meta["job_name"],
            command=self._payload_command(meta),
            partition=meta["partition"],
            gpus=meta["gpus"],
            high_perf=meta["high_perf"],
            cpus=meta["cpus"],
            mem=meta["memory"],
            time_limit=meta["time_limit"],
            node=meta.get("node"),
            paths=direct_paths,
            output=f"{meta['remote_dir']}/stdout.log",
            error=f"{meta['remote_dir']}/stderr.log",
        )
        if not built["ok"]:
            raise ApiError(
                "JOB_CONFIGURATION_BLOCKED",
                built["lint"]["problems"][0]["message"] if built["lint"]["problems"] else "작업 설정이 차단되었습니다.",
                status_code=422,
            )
        meta["node"] = built["node"]
        meta["auto_selected_node"] = built["auto_selected_node"]
        return built

    def _validate_staged_remote(self, meta: dict[str, Any]) -> None:
        dataset = self.remote.path_info(meta["dataset_path"])
        if not dataset.exists or not dataset.readable:
            raise ApiError(
                "DATASET_UNAVAILABLE",
                "제출 직전 데이터 경로를 다시 확인했지만 읽을 수 없습니다.",
                status_code=422,
            )
        if not dataset.is_file or not _is_supported_dataset_archive(meta["dataset_path"]):
            raise ApiError(
                "DATASET_ARCHIVE_REQUIRED",
                "TAR, TAR.GZ, TGZ 또는 ZIP 데이터 파일을 지정하세요.",
                status_code=422,
            )
        if not _is_under(meta["dataset_path"], self.remote.connection.config.data_root):
            raise ApiError(
                "DATASET_NOT_ON_NAS",
                "튜토리얼 기준 NAS 데이터 경로인 /data/...를 입력하세요.",
                status_code=422,
            )
        if not meta["copy_dataset_to_local"]:
            raise ApiError(
                "NAS_LOCAL_COPY_REQUIRED",
                "/data 데이터는 GPU 노드의 /local_datasets로 복사해야 합니다.",
                status_code=422,
            )
        output = self.remote.path_info(meta["output_path"])
        if not _is_under(meta["output_path"], self.remote.data_root):
            raise ApiError(
                "OUTPUT_OUTSIDE_USER_DATA",
                f"결과 경로는 {self.remote.data_root}/... 아래여야 합니다.",
                status_code=422,
            )
        writable = output.writable if output.exists else output.parent_writable
        if not writable:
            raise ApiError(
                "OUTPUT_NOT_WRITABLE",
                "제출 직전 결과 경로를 다시 확인했지만 쓸 수 없습니다.",
                status_code=422,
            )

    def prepare(self, spec: JobSpec, snapshot: Any) -> dict[str, Any]:
        validation = self.validate(spec, snapshot)
        if not validation["ok"]:
            first = next(problem for problem in validation["problems"] if problem["level"] == "block")
            raise ApiError("JOB_CONFIGURATION_BLOCKED", first["message"], status_code=422)

        code = self._inspect_local_code(spec)
        local_id = secrets.token_hex(6)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        remote_dir = f"{self.remote.jobs_root}/{stamp}-{spec.name}-{local_id}"
        archive_name = "code.zip" if code["kind"] == "zip" else "code.tar.gz"
        meta: dict[str, Any] = {
            "schema_version": 1,
            "local_job_id": local_id,
            "slurm_job_id": None,
            "job_name": spec.name,
            "username": self.remote.username,
            "entrypoint": spec.entrypoint,
            "arguments": spec.arguments,
            "dataset_path": spec.dataset_path,
            "output_path": spec.output_path,
            "copy_dataset_to_local": spec.copy_dataset_to_local,
            "partition": spec.partition or snapshot.default_partition,
            "gpus": spec.gpus,
            "high_perf": spec.high_perf,
            "cpus": spec.cpus,
            "memory": spec.memory,
            "time_limit": spec.time_limit,
            "node": validation["resolved"]["node"],
            "auto_selected_node": validation["resolved"]["auto_selected_node"],
            "conda_env": spec.conda_env,
            "conda_root": self._resolve_conda_root(spec.conda_env),
            "code_source_name": code["display_name"],
            "code_archive_name": archive_name,
            "code_archive_kind": code["kind"],
            "remote_dir": remote_dir,
            "stdout_path": f"{remote_dir}/stdout.log",
            "stderr_path": f"{remote_dir}/stderr.log",
            "status": "VALIDATED",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "submitted_at": None,
            "submit_request_id": None,
        }
        built = self._build_script(snapshot, meta)
        prediction = _public_prediction(self.connection.test_submit(built["script"]))
        if not prediction["ok"]:
            raise ApiError(
                "SLURM_TEST_REJECTED",
                prediction["reason"] or "Slurm 제출 전 검사에서 거절되었습니다.",
                status_code=422,
            )

        self.remote.make_dir(remote_dir)
        with self._package_code(code) as (local_archive, upload_name):
            self.remote.upload_file(local_archive, f"{remote_dir}/{upload_name}")
        self.remote.write_text(f"{remote_dir}/job.sbatch", built["script"])
        meta["status"] = "STAGED"
        meta["updated_at"] = _now_iso()
        meta["test_only"] = prediction
        self._write_meta(meta)
        return {
            "ok": True,
            "job": self._public_job(meta),
            "script": built["script"],
            "lint": built["lint"],
            "test_only": prediction,
        }

    def preflight(self, local_job_id: str) -> dict[str, Any]:
        meta = self._find(local_job_id)
        if meta.get("slurm_job_id") or meta.get("status") != "STAGED":
            raise ApiError("JOB_NOT_PREPARED", "준비 완료 상태에서만 srun 사전 점검을 할 수 있습니다.", 409)
        self._validate_staged_remote(meta)
        script = self._preflight_script(meta)
        self.remote.write_text(f"{meta['remote_dir']}/preflight.sh", script)
        debug_partition = self._debug_partition(meta["partition"])
        try:
            output = self.remote.run_preflight(
                meta["remote_dir"],
                partition=debug_partition,
                gpus=meta["gpus"],
                high_perf=meta["high_perf"],
                cpus=meta["cpus"],
                memory=meta["memory"],
                node=meta.get("node"),
            )
        except Exception as exc:
            meta["preflight"] = {
                "ok": False,
                "partition": debug_partition,
                "checked_at": _now_iso(),
                "output": str(exc)[-16_000:],
            }
            self._write_meta(meta)
            return {"ok": False, "preflight": meta["preflight"], "job": self._public_job(meta)}
        meta["preflight"] = {
            "ok": True,
            "partition": debug_partition,
            "checked_at": _now_iso(),
            "output": output[-16_000:],
        }
        self._write_meta(meta)
        return {"ok": True, "preflight": meta["preflight"], "job": self._public_job(meta)}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        meta["updated_at"] = _now_iso()
        self.remote.write_text(
            f"{meta['remote_dir']}/job.json",
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    @staticmethod
    def _public_job(meta: dict[str, Any]) -> dict[str, Any]:
        hidden = {"submit_request_id", "schema_version"}
        return {key: value for key, value in meta.items() if key not in hidden}

    def _find(self, local_job_id: str) -> dict[str, Any]:
        for remote_dir in self.remote.list_directories(self.remote.jobs_root):
            try:
                meta = read_job_json(self.remote, remote_dir)
            except ApiError:
                continue
            if meta.get("local_job_id") == local_job_id:
                # 신뢰 경계: job.json 안의 경로 대신 실제 검색 경로를 사용한다.
                meta["remote_dir"] = remote_dir
                return meta
        raise ApiError("JOB_NOT_FOUND", "작업을 찾을 수 없습니다.", 404)

    def submit(self, local_job_id: str, request_id: str, confirmed: bool, snapshot: Any) -> dict[str, Any]:
        if not confirmed:
            raise ApiError("CONFIRMATION_REQUIRED", "최종 확인 후 제출할 수 있습니다.", 409)
        meta = self._find(local_job_id)
        if meta.get("slurm_job_id"):
            if meta.get("submit_request_id") == request_id:
                return {"ok": True, "idempotent": True, "job": self._public_job(meta)}
            raise ApiError("JOB_ALREADY_SUBMITTED", "이미 제출된 작업입니다.", 409)
        if meta.get("status") == "SUBMITTING":
            if meta.get("submit_request_id") == request_id:
                raise ApiError(
                    "SUBMIT_IN_PROGRESS",
                    "앞선 제출 요청의 결과를 확인 중입니다. 잠시 후 작업 목록을 새로고침하세요.",
                    status_code=409,
                    retryable=True,
                )
            raise ApiError("JOB_ALREADY_SUBMITTED", "이미 제출이 시작된 작업입니다.", 409)
        if meta.get("status") != "STAGED":
            raise ApiError("JOB_NOT_PREPARED", "준비 완료 상태의 작업만 제출할 수 있습니다.", 409)
        if not meta.get("preflight", {}).get("ok"):
            raise ApiError(
                "SRUN_PREFLIGHT_REQUIRED",
                "튜토리얼 절차에 따라 srun 사전 점검을 통과한 뒤 제출하세요.",
                status_code=409,
            )

        # 서버 파일이 수정되었더라도 구조화된 job.json으로 스크립트를 다시 만든다.
        built = self._build_script(snapshot, meta)
        self._validate_staged_remote(meta)
        prediction = _public_prediction(self.connection.test_submit(built["script"]))
        if not prediction["ok"]:
            raise ApiError("SLURM_TEST_REJECTED", prediction["reason"], 422)
        self.remote.write_text(f"{meta['remote_dir']}/job.sbatch", built["script"])
        # sbatch 성공 직후 프로세스가 중단돼도 같은 요청을 다시 실행하지 않도록
        # SUBMITTING을 먼저 기록한다. 결과가 불명확하면 자동 재제출하지 않는다.
        meta["status"] = "SUBMITTING"
        meta["submit_request_id"] = request_id
        self._write_meta(meta)
        try:
            slurm_job_id = self.remote.submit(meta["remote_dir"])
        except Exception as exc:
            raise ApiError(
                "SUBMIT_STATUS_UNKNOWN",
                "제출 결과를 확정하지 못했습니다. 중복 제출을 막기 위해 자동 재시도하지 않습니다.",
                status_code=503,
                retryable=True,
            ) from exc
        meta.update({
            "slurm_job_id": slurm_job_id,
            "status": "SUBMITTED",
            "submitted_at": _now_iso(),
            "submit_request_id": request_id,
            "test_only": prediction,
        })
        self._write_meta(meta)
        return {"ok": True, "idempotent": False, "job": self._public_job(meta)}

    @staticmethod
    def _normalize_state(value: str) -> str:
        value = (value or "").upper().split()[0].rstrip("+")
        return {
            "PD": "PENDING",
            "R": "RUNNING",
            "CG": "COMPLETING",
            "CD": "COMPLETED",
            "F": "FAILED",
            "CA": "CANCELLED",
            "TO": "TIMEOUT",
            "OOM": "OUT_OF_MEMORY",
            "NF": "NODE_FAIL",
        }.get(value, value or "UNKNOWN")

    def get(self, local_job_id: str, *, refresh: bool = True) -> dict[str, Any]:
        meta = self._find(local_job_id)
        slurm_id = meta.get("slurm_job_id")
        state_info = None
        if refresh and slurm_id:
            state_info = self.remote.job_state(str(slurm_id))
            if state_info:
                status = self._normalize_state(state_info.get("state", ""))
                if status and status != meta.get("status"):
                    meta["status"] = status
                    self._write_meta(meta)

        result_info = None
        if meta.get("status") in _FINAL_STATES:
            result_info = self.remote.path_info(meta["output_path"]).to_dict()
        return {
            "ok": True,
            "job": self._public_job(meta),
            "slurm": state_info,
            "result": result_info,
        }

    def list(self, limit: int = 50) -> dict[str, Any]:
        jobs = []
        for remote_dir in self.remote.list_directories(self.remote.jobs_root):
            if len(jobs) >= limit:
                break
            try:
                meta = read_job_json(self.remote, remote_dir)
            except ApiError:
                continue
            meta["remote_dir"] = remote_dir
            jobs.append(self._public_job(meta))
        jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return {"ok": True, "jobs": jobs[:limit], "count": min(len(jobs), limit)}

    def logs(self, local_job_id: str, max_bytes: int = 128_000) -> dict[str, Any]:
        meta = self._find(local_job_id)
        return {
            "ok": True,
            "local_job_id": local_job_id,
            "stdout": self.remote.tail(meta["stdout_path"], max_bytes=max_bytes),
            "stderr": self.remote.tail(meta["stderr_path"], max_bytes=max_bytes),
            "truncated_to_bytes": max_bytes,
        }

    def cancel(self, local_job_id: str) -> dict[str, Any]:
        meta = self._find(local_job_id)
        slurm_id = meta.get("slurm_job_id")
        if not slurm_id:
            raise ApiError("JOB_NOT_SUBMITTED", "아직 제출되지 않은 작업입니다.", 409)
        if meta.get("status") in _FINAL_STATES:
            raise ApiError("JOB_ALREADY_FINISHED", "이미 종료된 작업입니다.", 409)
        self.remote.cancel(str(slurm_id))
        meta["status"] = "CANCEL_REQUESTED"
        self._write_meta(meta)
        return {"ok": True, "job": self._public_job(meta)}
