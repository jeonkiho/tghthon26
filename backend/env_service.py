"""웹에서 개인 conda 환경을 만든다.

이 도구의 명분은 "터미널 없이"인데, 정작 환경은 터미널에서만 만들 수 있었다.
공용 설치(/data/opt/anaconda3)는 읽기 전용이라 pip 하나 넣을 수 없고, 개인
anaconda3 를 설치하려면 결국 ssh 로 들어가 수 GB 를 받아야 했다. 그래서 torch
버전을 바꾸고 싶은 순간, 학생은 이 도구를 닫고 터미널을 열었다.

여기서 하는 일은 하나다: **공용 conda 바이너리는 읽기만 하고, 환경은 prefix 로
내 폴더(/data/<사용자>/envs)에 만든다.** 개인 설치 없이 원하는 파이썬 버전을 갖는다.

빌드는 오래 걸리므로(solve 만 5~20분) HTTP 요청 안에서 기다리지 않는다.
nohup 으로 떼어 놓고 로그 파일을 tail 해서 진행 상황을 보여준다.
"""

from __future__ import annotations

import json
import shlex
import uuid
from typing import Any

from .errors import ApiError
from .remote import PACKAGE_HOSTS
from .schemas import EnvSpec

# 골라 쓸 수 있는 파이썬. 목록을 서버가 주는 이유는, 화면이 임의 문자열을 받으면
# 존재하지 않는 버전으로 20분을 기다린 끝에 실패하기 때문이다.
PYTHON_VERSIONS = ["3.8", "3.9", "3.10", "3.11", "3.12"]

# 자주 쓰는 조합. 채널 이름(pytorch, nvidia)을 학생이 외우게 하면 안 된다.
PRESETS = [
    {
        "id": "torch-cu121",
        "label": "PyTorch 2.5 · CUDA 12.1",
        "python": "3.11",
        "conda_packages": ["pytorch", "torchvision", "pytorch-cuda=12.1"],
        "channels": ["pytorch", "nvidia", "conda-forge"],
        "note": "최신 GPU 학습용. 내려받을 게 많아 15분 이상 걸릴 수 있습니다.",
    },
    {
        "id": "torch-cu118",
        "label": "PyTorch 2.4 · CUDA 11.8",
        "python": "3.10",
        "conda_packages": ["pytorch", "torchvision", "pytorch-cuda=11.8"],
        "channels": ["pytorch", "nvidia", "conda-forge"],
        "note": "드라이버가 오래된 노드까지 고려한 조합입니다.",
    },
    {
        "id": "sklearn",
        "label": "데이터 분석 (numpy · pandas · scikit-learn)",
        "python": "3.11",
        "conda_packages": ["numpy", "pandas", "scikit-learn", "matplotlib"],
        "channels": ["conda-forge"],
        "note": "GPU 가 필요 없는 작업용. 몇 분이면 끝납니다.",
    },
]

_BUILD_ID = "^[a-f0-9]{12}$"

# 빌드 스크립트. 파일로 올려서 실행한다 — 긴 명령을 문자열로 이어 붙이면
# 따옴표가 세 겹으로 중첩돼 나중에 고치는 사람이 읽을 수 없다.
_BUILD_SCRIPT = """#!/usr/bin/bash
# SERAPH GUI 가 만든 환경 빌드 스크립트. 지우거나 직접 고칠 필요는 없습니다.
cd "$(dirname "$0")" || exit 1

# 어떤 이유로 죽든 rc 파일은 남긴다. 이게 없으면 화면의 진행 표시줄이 영원히
# 돌아간다 — 끝나지 않는 진행 표시줄은 오류 메시지보다 나쁘다.
trap 'code=$?; [ -f rc ] || printf "%s" "$code" > rc' EXIT

# conda.sh 를 source 하지 않고 바이너리를 직접 부른다. conda.sh 는 `conda` 를
# **셸 함수**로 만드는데, 그러면 `nice -n 19 conda` 가 nice 에게 없는 실행파일을
# 찾게 해서 "No such file or directory" 로 죽는다. 환경을 만들 때는 activate 가
# 필요 없으니 함수도 필요 없다.
CONDA_BIN={conda_bin}
PREFIX={prefix}
DATA_ROOT={data_root}

# 공용 conda 설치는 읽기 전용이다. 패키지 캐시를 거기에 쓰려다 실패하지 않게
# 내 폴더로 돌린다. /home 은 작게 유지한다(세라프 저장소 예절).
export CONDA_PKGS_DIRS="$DATA_ROOT/.conda/pkgs"
export PIP_CACHE_DIR="$DATA_ROOT/.cache/pip"
mkdir -p "$CONDA_PKGS_DIRS" "$PIP_CACHE_DIR" || exit 1

say() {{ printf '[seraph] %s\\n' "$*"; }}
run() {{
  printf '[seraph] $ %s\\n' "$*"
  "$@" || {{ code=$?; say "실패 (rc=$code)"; printf '%s' "$code" > rc; exit "$code"; }}
}}

say "시작 $(date '+%F %T')"
if [ ! -x "$CONDA_BIN" ]; then
  say "conda 를 실행할 수 없습니다: $CONDA_BIN"
  printf '1' > rc
  exit 1
fi

{steps}

# 마지막에 반드시 실행해 본다. 이게 없으면 conda 가 조용히 반쪽짜리 환경을 남겨도
# "준비 완료"라고 말하게 되고, 학생은 그 사실을 30분 뒤 job 이 죽고 나서 안다.
say '환경을 확인합니다.'
run "$PREFIX/bin/python" --version
say "위치: $PREFIX"
say "완료 $(date '+%F %T')"
printf '0' > rc
"""


class EnvService:
    """개인 환경의 점검·목록·생성·삭제. 화면 하나가 쓰는 만큼만 있다."""

    def __init__(self, manager: Any):
        self.manager = manager

    @property
    def remote(self) -> Any:
        return self.manager.remote

    # --- 점검 -------------------------------------------------------------

    def tools(self) -> dict[str, Any]:
        """"이 서버에서 환경을 만들 수 있나"에 사실로 답한다.

        추측하고 시작하면 20분 뒤에 실패한다. 네트워크·디스크·conda 를 먼저 본다.
        """
        remote = self.remote
        installs = remote.find_conda().get("installs", [])
        base = self._base_install(installs)
        probe = remote.probe_env_tools(base["root"] if base else None)

        network = probe.get("network") or {}
        online = [url for url, code in network.items() if 200 <= code < 400]
        blockers: list[str] = []
        if not installs:
            blockers.append("서버에서 conda 설치를 찾지 못했습니다.")
        elif not probe.get("conda_version"):
            blockers.append(f"conda 를 실행할 수 없습니다({base['root'] if base else '?'}).")
        if not probe.get("writable"):
            blockers.append(f"{remote.data_root} 에 쓸 수 없습니다.")

        warnings: list[str] = []
        if not online:
            warnings.append(
                "로그인 노드에서 패키지 저장소에 닿지 못했습니다. 새로 내려받아야 하는 "
                "'처음부터 만들기'는 실패할 수 있고, '공용 환경 복제'만 안전합니다.")
        avail = probe.get("avail_bytes")
        if isinstance(avail, int) and avail < 10 * 1024**3:
            warnings.append(
                f"{probe.get('filesystem') or '/data'} 여유 공간이 적습니다. "
                "PyTorch 환경 하나가 5~8GB 를 씁니다.")

        return {
            "can_create": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "conda_version": probe.get("conda_version") or None,
            "base_install": base,
            "installs": installs,
            "filesystem": probe.get("filesystem") or None,
            "avail_bytes": avail,
            "loadavg": probe.get("loadavg") or None,
            "cpus": probe.get("nproc") or None,
            "network": [
                {"url": url, "status": network.get(url, 0), "ok": 200 <= network.get(url, 0) < 400}
                for url in PACKAGE_HOSTS
            ],
            "envs_root": remote.envs_root,
            "python_versions": PYTHON_VERSIONS,
            "presets": PRESETS,
        }

    @staticmethod
    def _base_install(installs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """환경을 만들 때 쓸 conda. 개인 설치가 있으면 그쪽을 먼저 본다."""
        if not installs:
            return None
        return sorted(installs, key=lambda i: not i.get("is_personal"))[0]

    # --- 목록 -------------------------------------------------------------

    def list_envs(self) -> dict[str, Any]:
        remote = self.remote
        installs = remote.find_conda().get("installs", [])
        mine = [{**env, "source": "personal", "removable": True}
                for env in remote.list_prefix_envs()]
        known = {env["name"] for env in mine}

        shared = []
        for install in installs:
            for name in install.get("envs") or []:
                if name in known:
                    continue
                known.add(name)
                shared.append({
                    "name": name,
                    "prefix": f"{install['root']}/envs/{name}",
                    "kind": "conda",
                    "python": None,
                    "source": "personal-install" if install.get("is_personal") else "shared",
                    "conda_root": install["root"],
                    # 공용 설치는 읽기 전용이다. 지우기 버튼을 주면 거짓말이 된다.
                    "removable": False,
                })
        return {"envs": mine + shared, "installs": installs, "envs_root": remote.envs_root}

    # --- 생성 -------------------------------------------------------------

    def create(self, spec: EnvSpec) -> dict[str, Any]:
        remote = self.remote
        installs = remote.find_conda().get("installs", [])
        base = self._base_install(installs)
        if base is None:
            raise ApiError(
                "CONDA_NOT_FOUND",
                "서버에서 conda 설치를 찾지 못해 환경을 만들 수 없습니다.",
                status_code=409,
            )

        prefix = f"{remote.envs_root}/{spec.name}"
        if any(env["name"] == spec.name for env in remote.list_prefix_envs()):
            raise ApiError(
                "ENV_ALREADY_EXISTS",
                f"'{spec.name}' 환경이 이미 있습니다. 다른 이름을 쓰거나 기존 환경을 지우세요.",
                status_code=409,
            )

        source_prefix = None
        if spec.mode in ("clone", "venv"):
            source_prefix = self._resolve_source(spec.source, remote, installs)

        build_id = uuid.uuid4().hex[:12]
        script = _BUILD_SCRIPT.format(
            conda_bin=shlex.quote(f"{base['root']}/bin/conda"),
            prefix=shlex.quote(prefix),
            data_root=shlex.quote(remote.data_root),
            steps=self._steps(spec, prefix, source_prefix),
        )
        record = {
            "build_id": build_id,
            "name": spec.name,
            "prefix": prefix,
            "mode": spec.mode,
            "python": spec.python,
            "conda_packages": spec.conda_packages,
            "pip_packages": spec.pip_packages,
            "channels": spec.channels,
            "source": source_prefix,
            "conda_root": base["root"],
        }
        remote.start_env_build(build_id, script, record)
        return {"build": {**record, "state": "running", "log": ""}}

    @staticmethod
    def _resolve_source(source: str | None, remote: Any, installs: list[dict[str, Any]]) -> str:
        """복제·venv 의 원본 환경을 절대 경로로 바꾼다.

        이름만 넘기면 conda 가 자기 envs_dirs 에서 찾는데, 공용 설치의 환경은
        거기에 없다. 경로로 주면 어느 설치의 환경이든 그대로 쓸 수 있다.
        """
        if not source:
            raise ApiError(
                "ENV_SOURCE_REQUIRED",
                "복제하거나 기반으로 쓸 환경을 골라주세요.",
                status_code=422,
            )
        for env in remote.list_prefix_envs():
            if env["name"] == source:
                return env["prefix"]
        for install in sorted(installs, key=lambda i: not i.get("is_personal")):
            if source in (install.get("envs") or []):
                return f"{install['root']}/envs/{source}"
        raise ApiError(
            "ENV_SOURCE_NOT_FOUND",
            f"'{source}' 환경을 서버에서 찾지 못했습니다.",
            status_code=404,
        )

    @staticmethod
    def _steps(spec: EnvSpec, prefix: str, source_prefix: str | None) -> str:
        """모드별 실제 명령. 만든 뒤에는 항상 절대 경로 python 으로 pip 를 쓴다 —
        activate 는 셸 상태에 의존해서 비대화형 스크립트에서 조용히 어긋난다."""
        quoted_prefix = shlex.quote(prefix)
        lines: list[str] = []

        if spec.mode in ("scratch", "clone"):
            # conda 26+ 는 기본 채널(main/r) 이용약관 동의를 요구한다. -y 비대화형
            # 실행이라 미동의면 CondaToSNonInteractiveError 로 바로 실패한다. 빌드 전에
            # best-effort 로 동의해 둔다(구버전 conda 는 tos 서브커맨드가 없어 실패할 수
            # 있으므로 || true 로 무시한다 — run 이 아니라 실패해도 빌드는 계속한다).
            lines.append("say 'conda 채널 약관을 확인합니다.'")
            for _tos_channel in ("https://repo.anaconda.com/pkgs/main",
                                 "https://repo.anaconda.com/pkgs/r"):
                lines.append(
                    f'"$CONDA_BIN" tos accept --override-channels '
                    f"--channel {_tos_channel} >/dev/null 2>&1 || true")

        if spec.mode == "scratch":
            args = ['"$CONDA_BIN"', "create", "-y", "-p", quoted_prefix, f"python={spec.python}"]
            args += [shlex.quote(pkg) for pkg in spec.conda_packages]
            for channel in spec.channels:
                args += ["-c", shlex.quote(channel)]
            # solve 는 CPU 를 많이 쓴다. 로그인 노드는 모두가 같이 쓰는 곳이라
            # 우선순위를 낮춰 다른 사람의 셸이 느려지지 않게 한다.
            lines.append("say '환경을 처음부터 만듭니다. 15분 이상 걸릴 수 있습니다.'")
            lines.append("run nice -n 19 " + " ".join(args))
        elif spec.mode == "clone":
            lines.append("say '기존 환경을 복제합니다.'")
            lines.append(
                f'run nice -n 19 "$CONDA_BIN" create -y -p {quoted_prefix} '
                f"--clone {shlex.quote(source_prefix or '')}")
        else:  # venv
            lines.append("say '기존 파이썬 위에 venv 를 만듭니다.'")
            lines.append(
                f"run {shlex.quote(f'{source_prefix}/bin/python')} -m venv {quoted_prefix}")

        if spec.pip_packages:
            python = shlex.quote(f"{prefix}/bin/python")
            packages = " ".join(shlex.quote(pkg) for pkg in spec.pip_packages)
            lines.append("say 'pip 패키지를 설치합니다.'")
            lines.append(f"run {python} -m pip install --no-input --disable-pip-version-check {packages}")
        return "\n".join(lines)

    # --- 진행 상황 --------------------------------------------------------

    def build_status(self, build_id: str) -> dict[str, Any]:
        remote = self.remote
        try:
            raw = remote.read_text(f"{remote.build_dir(build_id)}/spec.json")
        except Exception as exc:
            raise ApiError("ENV_BUILD_NOT_FOUND", "환경 빌드 기록을 찾을 수 없습니다.", 404) from exc

        record = json.loads(raw)
        state = remote.background_state(build_id)
        rc, alive = state.get("rc"), state.get("alive")
        if rc == 0:
            status, message = "succeeded", f"'{record['name']}' 환경이 준비됐습니다."
        elif rc is not None:
            status, message = "failed", f"환경 만들기가 실패했습니다 (rc={rc}). 아래 로그를 보세요."
        elif alive:
            status, message = "running", "만드는 중입니다. 창을 닫아도 서버에서 계속됩니다."
        else:
            # rc 도 없고 프로세스도 없다 = 도중에 죽었다. 조용히 기다리면 안 된다.
            status, message = "failed", "빌드 프로세스가 사라졌습니다. 로그의 마지막 줄을 확인하세요."
        return {"build": {**record, "state": status, "message": message, "log": state.get("log", "")}}

    # --- 삭제 -------------------------------------------------------------

    def delete(self, name: str) -> dict[str, Any]:
        remote = self.remote
        if not any(env["name"] == name for env in remote.list_prefix_envs()):
            raise ApiError(
                "ENV_NOT_FOUND",
                f"'{name}' 은 직접 만든 환경이 아니어서 지울 수 없습니다.",
                status_code=404,
            )
        remote.remove_prefix_env(name)
        return {"deleted": name}
