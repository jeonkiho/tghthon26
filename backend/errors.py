"""API 오류를 한 가지 JSON 형식으로 변환한다."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("seraph_gui.api")


@dataclass
class ApiError(Exception):
    code: str
    message: str
    status_code: int = 400
    retryable: bool = False


def error_body(code: str, message: str, retryable: bool = False) -> dict:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.retryable),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())[1:])
        detail = first.get("msg", "입력값을 확인하세요.")
        message = f"{location}: {detail}" if location else detail
        return JSONResponse(
            status_code=422,
            content=error_body("INVALID_REQUEST", message),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 명령문·경로·인증정보가 응답으로 새지 않게 원문을 반환하지 않는다.
        log.exception("unhandled API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=error_body(
                "INTERNAL_ERROR",
                "로컬 프로그램에서 예상하지 못한 오류가 발생했습니다.",
                retryable=True,
            ),
        )

