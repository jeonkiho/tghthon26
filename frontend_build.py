"""프론트엔드 빌드 산출물(frontend/dist)을 필요할 때만 만든다.

예전에는 dist 를 저장소에 커밋했다. 그러면 프론트를 건드리는 PR 두 개가 동시에
열릴 때마다 index.html 이 충돌하고, 웹 에디터에서 한쪽 텍스트를 고르면 소스는
합쳐졌는데 번들은 한쪽만 남는다 — 실제로 그렇게 CSS 가 통째로 빠진 채 머지된
적이 있다. 산출물은 소스에서 다시 만들 수 있으므로 저장소에 둘 이유가 없다.

**시작 스크립트가 아니라 여기(파이썬)에 두는 이유**: .env 로딩을 start_unix.sh
에만 넣었다가, 윈도우 배치와 uvicorn 직접 실행에서 조용히 빠졌던 적이 있다.
같은 실수를 반복하지 않는다. run_gui.py 를 거치면 어느 OS 든 똑같이 동작한다.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
# 이 파일들이 dist 보다 새로우면 다시 빌드한다.
WATCHED = ("src", "index.html", "package.json", "package-lock.json", "vite.config.js")

INSTALL_HINT = (
    "프론트엔드를 빌드하려면 Node.js 가 필요합니다.\n"
    "  https://nodejs.org 에서 LTS 를 설치한 뒤 이 창을 다시 실행하세요.\n"
    "  (설치 없이 API 만 쓰려면 그대로 두셔도 됩니다 — 화면만 나오지 않습니다.)"
)


def _newest_mtime(path: pathlib.Path) -> float:
    if path.is_file():
        return path.stat().st_mtime
    newest = 0.0
    for item in path.rglob("*"):
        if item.is_file():
            newest = max(newest, item.stat().st_mtime)
    return newest


def needs_build() -> bool:
    """소스가 산출물보다 새로우면 다시 만들어야 한다."""
    index = DIST / "index.html"
    if not index.is_file():
        return True
    built = _newest_mtime(DIST)
    for name in WATCHED:
        source = FRONTEND / name
        if source.exists() and _newest_mtime(source) > built:
            return True
    return False


def _npm() -> str | None:
    # 윈도우에서 npm 은 npm.cmd 다. shutil.which 가 PATHEXT 를 봐서 찾아준다.
    return shutil.which("npm")


def ensure(*, force: bool = False) -> bool:
    """필요하면 프론트엔드를 빌드한다. 실패해도 예외를 올리지 않는다.

    빌드가 안 됐다고 서버까지 못 뜨게 하지는 않는다. API 는 그대로 쓸 수 있고,
    화면 대신 무엇을 해야 하는지 알려주는 안내가 뜬다(backend/main.py).
    """
    if not FRONTEND.is_dir():
        return False
    if not force and not needs_build():
        return True

    npm = _npm()
    if not npm:
        print("[seraph] " + INSTALL_HINT, file=sys.stderr)
        return False

    if not (FRONTEND / "node_modules").is_dir():
        print("[seraph] 프론트엔드 의존성을 처음 한 번 설치합니다. 1~2분 걸립니다…")
        if subprocess.run([npm, "install"], cwd=FRONTEND).returncode != 0:
            print("[seraph] npm install 이 실패했습니다.", file=sys.stderr)
            return False

    print("[seraph] 프론트엔드를 빌드합니다…")
    if subprocess.run([npm, "run", "build"], cwd=FRONTEND).returncode != 0:
        print("[seraph] 프론트엔드 빌드가 실패했습니다. 위 메시지를 확인하세요.", file=sys.stderr)
        return False
    print("[seraph] 빌드 완료.")
    return True


if __name__ == "__main__":
    sys.exit(0 if ensure(force="--force" in sys.argv) else 1)
