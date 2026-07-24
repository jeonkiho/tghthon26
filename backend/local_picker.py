"""브라우저 GUI에서 로컬 OS의 파일 선택창을 여는 보조 기능."""

from __future__ import annotations

from .errors import ApiError


def select_code_path(kind: str) -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if kind == "directory":
                selected = filedialog.askdirectory(title="SERAPH에 올릴 코드 폴더 선택")
            else:
                selected = filedialog.askopenfilename(
                    title="SERAPH에 올릴 코드 파일 선택",
                    filetypes=[
                        ("지원 파일", "*.py *.zip *.tar.gz *.tgz"),
                        ("모든 파일", "*.*"),
                    ],
                )
        finally:
            root.destroy()
        return selected or None
    except Exception as exc:
        raise ApiError(
            "FILE_PICKER_UNAVAILABLE",
            "파일 선택창을 열 수 없습니다. 코드 경로를 직접 입력해 주세요.",
            status_code=503,
            retryable=False,
        ) from exc
