"""SERAPH GUI 로컬 실행 진입점."""

import webbrowser
from threading import Timer

import uvicorn


def main() -> None:
    Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
