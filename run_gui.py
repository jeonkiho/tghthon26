"""SERAPH GUI 로컬 실행 진입점."""

import webbrowser
from threading import Timer

import uvicorn

import frontend_build


def main() -> None:
    # 화면(frontend/dist)은 저장소에 두지 않고 여기서 만든다. 소스가 그대로면
    # 건너뛰므로 두 번째 실행부터는 바로 뜬다.
    frontend_build.ensure()
    Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
