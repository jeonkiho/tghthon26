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
import pathlib
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


# 코드 폴더에서 찾아볼 환경 스펙 파일. 위에 있는 것을 먼저 쓴다 —
# environment.yml 은 채널과 conda 패키지까지 담아서 더 완전하다.
SPEC_FILES = (
    ("conda", "environment.yml"),
    ("conda", "environment.yaml"),
    ("conda", "conda.yml"),
    ("pip", "requirements.txt"),
)

# 감지는 하지만 아직 이 파일로 환경을 만들지는 못한다. 도구(poetry·uv·pdm)마다
# 설치 방법이 달라서, 하나로 뭉뚱그리면 틀린 명령을 돌리게 된다.
SPEC_FILES_UNSUPPORTED = ("pyproject.toml", "Pipfile", "poetry.lock", "uv.lock")

# 스펙 파일 미리보기 상한. 화면에서 확인용으로 보여줄 만큼만 읽는다.
SPEC_PREVIEW_BYTES = 8 * 1024


def detect_spec_files(local_dir: str) -> dict[str, Any]:
    """코드 폴더에 환경 스펙 파일이 있는지 본다.

    깃에 커밋된 environment.yml / requirements.txt 로 환경을 만드는 건 파이썬
    쪽에서 가장 표준적인 방식이다. 그런데 이 화면은 지금까지 패키지를 손으로
    타이핑하게 했다 — 이미 저장소에 정답이 적혀 있는데도.
    """
    root = pathlib.Path(local_dir).expanduser()
    if not root.is_dir():
        raise ApiError("LOCAL_DIR_NOT_FOUND", "선택한 폴더를 찾을 수 없습니다.", 400)

    found = []
    for kind, name in SPEC_FILES:
        path = root / name
        if not path.is_file():
            continue
        raw = path.read_bytes()[:SPEC_PREVIEW_BYTES + 1]
        found.append({
            "kind": kind,
            "name": name,
            "path": str(path),
            "size": path.stat().st_size,
            "truncated": len(raw) > SPEC_PREVIEW_BYTES,
            "text": raw[:SPEC_PREVIEW_BYTES].decode("utf-8", errors="replace"),
        })

    unsupported = [name for name in SPEC_FILES_UNSUPPORTED if (root / name).is_file()]
    return {"directory": str(root), "specs": found, "unsupported": unsupported}


# conda 가 내는 오류 중, 원문만 봐서는 무엇을 해야 하는지 알기 어려운 것들.
# 로그는 그대로 두고 맨 위에 한 줄로 무엇을 해야 하는지 얹는다.
_FAILURE_HINTS = (
    (
        "CondaToSNonInteractiveError",
        "Anaconda 기본 채널(repo.anaconda.com)의 이용약관에 동의하지 않아 막혔습니다. "
        "'처음부터 만들기'는 적어 놓은 채널만 쓰므로 이 오류가 나지 않습니다. "
        "복제나 환경 파일로 만드는 중이라면, 그 환경·파일이 기본 채널을 요구하는 "
        "경우입니다 — 환경 파일의 channels 에서 defaults 를 빼거나, 서버에서 "
        "`conda tos accept` 로 직접 동의해야 합니다.",
    ),
    (
        "PackagesNotFoundError",
        "요청한 패키지를 채널에서 찾지 못했습니다. 이름·버전을 확인하거나 "
        "필요한 채널(pytorch·nvidia 등)을 채널 칸에 넣어 주세요.",
    ),
    (
        "No space left on device",
        "디스크가 가득 찼습니다. '파일' 화면에서 쓰지 않는 환경이나 데이터를 정리한 뒤 "
        "다시 시도하세요.",
    ),
    (
        "CondaHTTPError",
        "패키지 저장소에 연결하지 못했습니다. 환경 화면 맨 위의 점검 결과에서 "
        "네트워크 상태를 확인하세요.",
    ),
)


def _explain_failure(log: str) -> str | None:
    """로그에서 알려진 실패를 찾아 '무엇을 해야 하는지' 한 줄로 바꾼다."""
    for needle, hint in _FAILURE_HINTS:
        if needle in log:
            return hint
    return None


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

        spec_text = None
        if spec.mode == "spec":
            spec_text = self._read_spec(spec)

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
            "spec_kind": spec.spec_kind,
            "spec_name": pathlib.PurePath(spec.spec_path).name if spec.spec_path else None,
            "conda_root": base["root"],
        }
        if spec_text is not None:
            # 스펙 파일을 서버로 함께 올린다. 로컬 경로를 서버가 읽을 수는 없다.
            remote.write_text(f"{remote.build_dir(build_id)}/spec", spec_text)
        remote.start_env_build(build_id, script, record)
        return {"build": {**record, "state": "running", "log": ""}}

    @staticmethod
    def _read_spec(spec: EnvSpec) -> str:
        """내 PC 의 스펙 파일을 읽는다. 서버는 이 경로를 볼 수 없으므로 내용을 올린다."""
        if not spec.spec_path:
            raise ApiError("ENV_SPEC_REQUIRED", "사용할 환경 파일을 골라주세요.", 422)
        path = pathlib.Path(spec.spec_path).expanduser()
        if not path.is_file():
            raise ApiError("ENV_SPEC_NOT_FOUND", f"'{path.name}' 파일을 찾을 수 없습니다.", 400)
        if path.stat().st_size > 1024 * 1024:
            raise ApiError(
                "ENV_SPEC_TOO_LARGE",
                "환경 파일이 1MB 를 넘습니다. 올바른 파일인지 확인해 주세요.",
                status_code=422,
            )
        return path.read_text(encoding="utf-8", errors="replace")

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

        # conda 26+ 는 기본 채널(repo.anaconda.com)의 이용약관 동의를 요구하고,
        # -y 비대화형이라 미동의면 CondaToSNonInteractiveError 로 바로 실패한다.
        #
        # **scratch 에는 붙이지 않는다.** 아래에서 --override-channels 로 화면에 적힌
        # 채널만 쓰므로 애초에 그 채널에 갈 일이 없다 — 쓰지도 않을 약관에 동의부터
        # 해 둘 이유가 없다. 우리가 정할 수 없는 경우에만 남긴다:
        #   clone — 원본 환경이 어느 채널에서 왔는지는 그 환경이 안다
        #   spec  — 채널은 사용자가 커밋한 environment.yml 이 정한다
        #
        # 로그를 감추지 않는다. 남의 이용약관에 대신 동의하는 일이므로, 최소한
        # 무슨 일이 있었는지는 빌드 로그에 남아야 한다.
        if spec.mode in ("clone", "spec"):
            lines.append("say 'conda 기본 채널 약관 동의를 확인합니다(이 방식은 채널을 우리가 정하지 않습니다).'")
            for _tos_channel in ("https://repo.anaconda.com/pkgs/main",
                                 "https://repo.anaconda.com/pkgs/r"):
                # 구버전 conda 에는 tos 서브커맨드가 없다. 없다고 빌드를 멈추면 안 되므로
                # run 이 아니라 || true 로 넘긴다.
                lines.append(
                    f'"$CONDA_BIN" tos accept --override-channels '
                    f"--channel {_tos_channel} 2>&1 | tail -1 || true")

        if spec.mode == "scratch":
            # --override-channels 를 붙인다. 이게 없으면 conda 는 화면에 적힌 채널
            # **말고도** condarc 의 defaults(repo.anaconda.com)를 같이 뒤진다.
            #
            # 두 가지가 어긋난다. 하나는 화면이 "채널: pytorch nvidia conda-forge"
            # 라고 말해 놓고 실제로는 다른 곳에서도 받는다는 것. 다른 하나는 요즘
            # conda 가 그 채널에 대해 약관 동의를 요구해서, 최신 conda 를 쓰는
            # 사람만 CondaToSNonInteractiveError 로 죽는다는 것 —
            # 같은 화면에서 같은 값을 넣었는데 누구는 되고 누구는 안 된다.
            #
            # 약관 동의를 우리가 대신 눌러 주지는 않는다(라이선스 판단이다).
            # 화면에 적힌 채널만 쓰면 애초에 그 채널에 갈 일이 없다.
            channels = spec.channels or ["conda-forge"]
            args = ['"$CONDA_BIN"', "create", "-y", "--override-channels",
                    "-p", quoted_prefix, f"python={spec.python}"]
            args += [shlex.quote(pkg) for pkg in spec.conda_packages]
            for channel in channels:
                args += ["-c", shlex.quote(channel)]
            # solve 는 CPU 를 많이 쓴다. 로그인 노드는 모두가 같이 쓰는 곳이라
            # 우선순위를 낮춰 다른 사람의 셸이 느려지지 않게 한다.
            lines.append("say '환경을 처음부터 만듭니다. 15분 이상 걸릴 수 있습니다.'")
            lines.append(f"say '채널: {' '.join(channels)} (이 목록만 사용합니다)'")
            lines.append("run nice -n 19 " + " ".join(args))
        elif spec.mode == "clone":
            lines.append("say '기존 환경을 복제합니다.'")
            lines.append(
                f'run nice -n 19 "$CONDA_BIN" create -y -p {quoted_prefix} '
                f"--clone {shlex.quote(source_prefix or '')}")
        elif spec.mode == "spec":
            # 스펙 파일은 빌드 폴더에 spec 이라는 이름으로 같이 올라간다. 원본 이름을
            # 그대로 쓰지 않는 이유는, 이름이 무엇이든 스크립트가 한 곳만 보게 하려는 것.
            if spec.spec_kind == "conda":
                lines.append("say '환경 파일(environment.yml)로 만듭니다.'")
                # conda env create 에는 --override-channels 가 없다. 채널은 파일이
                # 정한다 — 사용자가 커밋해 둔 파일을 우리가 고쳐 쓰지는 않는다.
                lines.append(
                    f'run nice -n 19 "$CONDA_BIN" env create -f "$(dirname "$0")/spec" '
                    f"-p {quoted_prefix}")
            else:
                lines.append("say 'requirements.txt 로 만듭니다.'")
                lines.append(
                    f'run nice -n 19 "$CONDA_BIN" create -y --override-channels '
                    f"-p {quoted_prefix} python={spec.python} -c conda-forge")
                lines.append(
                    f'run {shlex.quote(f"{prefix}/bin/python")} -m pip install --no-input '
                    f'--disable-pip-version-check -r "$(dirname "$0")/spec"')
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
            status = "failed"
            message = (_explain_failure(state.get("log", ""))
                       or f"환경 만들기가 실패했습니다 (rc={rc}). 아래 로그를 보세요.")
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
