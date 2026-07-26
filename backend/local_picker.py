"""브라우저 GUI에서 로컬 OS의 파일 선택창을 여는 보조 기능."""

from __future__ import annotations

from .errors import ApiError


def _ask(open_dialog, message: str):
    """tkinter 선택창을 띄운다. 백엔드가 사용자 PC 에서 돌기 때문에 가능하다."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = open_dialog(filedialog)
        finally:
            root.destroy()
        return selected or None
    except Exception as exc:
        raise ApiError(
            "FILE_PICKER_UNAVAILABLE",
            message,
            status_code=503,
            retryable=False,
        ) from exc


def select_code_path(kind: str) -> str | None:
    def dialog(filedialog):
        if kind == "directory":
            return filedialog.askdirectory(title="SERAPH에 올릴 코드 폴더 선택")
        return filedialog.askopenfilename(
            title="SERAPH에 올릴 코드 파일 선택",
            filetypes=[
                ("지원 파일", "*.py *.zip *.tar.gz *.tgz"),
                ("모든 파일", "*.*"),
            ],
        )

    return _ask(dialog, "파일 선택창을 열 수 없습니다. 코드 경로를 직접 입력해 주세요.")


def select_any_file() -> str | None:
    """탐색기에서 지금 보고 있는 폴더로 올릴 파일 하나를 고른다.

    데이터셋과 달리 확장자를 제한하지 않는다. 압축 파일만 받는 규칙은 NAS IOPS 를
    지키려고 **데이터셋**에 건 것이지, 스크립트 한 장에 걸 이유가 없다.
    """
    def dialog(filedialog):
        return filedialog.askopenfilename(title="SERAPH 폴더에 올릴 파일 선택")

    return _ask(dialog, "파일 선택창을 열 수 없습니다.")


def select_dataset_archive() -> str | None:
    """NAS 로 올릴 데이터셋 압축 파일을 고른다.

    세라프 정책상 데이터셋은 압축 파일 하나로 올려야 한다(NAS IOPS 보호).
    그래서 폴더는 고를 수 없게 하고 압축 확장자만 보여준다.
    """
    def dialog(filedialog):
        return filedialog.askopenfilename(
            title="NAS 에 올릴 데이터셋 압축 파일 선택",
            filetypes=[
                ("데이터셋 압축 파일", "*.tar *.tar.gz *.tgz *.zip"),
                ("모든 파일", "*.*"),
            ],
        )

    return _ask(dialog, "파일 선택창을 열 수 없습니다. 데이터셋 경로를 직접 입력해 주세요.")
