"""FastAPI 요청 모델. 셸 명령 대신 구조화된 값만 받는다."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MEMORY = re.compile(r"^[1-9][0-9]*(?:M|G|T)$", re.IGNORECASE)
_TIME = re.compile(r"^[0-9]{1,3}:[0-5][0-9]:[0-5][0-9]$")
_CONDA = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,255}$")
_LOCAL_ID = re.compile(r"^[a-f0-9]{8,32}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def _safe_relative(value: str, label: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"{label}은 코드 폴더 안의 안전한 상대경로여야 합니다.")
    return str(path)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConnectRequest(StrictModel):
    # 비밀번호는 한 글자도 바꾸지 않는다. 사용자명과 호스트만 validator에서 정리한다.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    username: str | None = Field(default=None, max_length=64)
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    password: SecretStr | None = None

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str | None) -> str | None:
        value = value.strip() if value is not None else None
        if value is not None and not _USERNAME.fullmatch(value):
            raise ValueError("사용자명에는 영문, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다.")
        return value

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str | None) -> str | None:
        value = value.strip() if value is not None else None
        if value is not None and not _HOST.fullmatch(value):
            raise ValueError("SSH 호스트 이름 형식이 올바르지 않습니다.")
        return value


class RecommendationRequest(StrictModel):
    gpus: int = Field(default=1, ge=1, le=16)
    hours: float = Field(default=2.0, gt=0, le=144)
    high_perf: bool = False
    node: str | None = Field(default=None, max_length=128)


class PreviewRequest(StrictModel):
    name: str
    command: str = Field(min_length=1, max_length=2000)
    partition: str | None = Field(default=None, max_length=128)
    gpus: int = Field(default=1, ge=1, le=16)
    high_perf: bool = False
    cpus: int = Field(default=8, ge=1, le=256)
    memory: str = "32G"
    time_limit: str = "02:00:00"
    node: str | None = Field(default=None, max_length=128)
    paths: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("영문, 숫자, 밑줄, 하이픈만 64자까지 사용할 수 있습니다.")
        return value

    @field_validator("memory")
    @classmethod
    def valid_memory(cls, value: str) -> str:
        value = value.upper()
        if not _MEMORY.fullmatch(value):
            raise ValueError("32G 또는 64000M 형식으로 입력하세요.")
        return value

    @field_validator("time_limit")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not _TIME.fullmatch(value):
            raise ValueError("HH:MM:SS 형식으로 입력하세요.")
        return value


class JobSpec(StrictModel):
    name: str
    local_code_path: str = Field(min_length=1, max_length=4096)
    entrypoint: str = Field(default="train.py", min_length=1, max_length=512)
    arguments: list[str] = Field(default_factory=list, max_length=100)
    dataset_path: str = Field(min_length=1, max_length=4096)
    output_path: str = Field(min_length=1, max_length=4096)
    copy_dataset_to_local: bool = True
    partition: str | None = Field(default=None, max_length=128)
    gpus: int = Field(default=1, ge=1, le=16)
    high_perf: bool = False
    cpus: int = Field(default=8, ge=1, le=256)
    memory: str = "32G"
    time_limit: str = "02:00:00"
    node: str | None = Field(default=None, max_length=128)
    conda_env: str | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("영문, 숫자, 밑줄, 하이픈만 64자까지 사용할 수 있습니다.")
        return value

    @field_validator("entrypoint")
    @classmethod
    def valid_entrypoint(cls, value: str) -> str:
        return _safe_relative(value, "진입 파일")

    @field_validator("dataset_path", "output_path")
    @classmethod
    def absolute_remote_path(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("서버 경로에는 제어 문자를 넣을 수 없습니다.")
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("서버의 절대경로를 입력하세요.")
        return str(path)

    @field_validator("arguments")
    @classmethod
    def safe_arguments(cls, values: list[str]) -> list[str]:
        for value in values:
            if "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError("실행 인자에는 줄바꿈이나 NUL 문자를 넣을 수 없습니다.")
            if len(value) > 1000:
                raise ValueError("실행 인자 하나는 1,000자를 넘을 수 없습니다.")
        return values

    @field_validator("memory")
    @classmethod
    def valid_memory(cls, value: str) -> str:
        value = value.upper()
        if not _MEMORY.fullmatch(value):
            raise ValueError("32G 또는 64000M 형식으로 입력하세요.")
        return value

    @field_validator("time_limit")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not _TIME.fullmatch(value):
            raise ValueError("HH:MM:SS 형식으로 입력하세요.")
        return value

    @field_validator("conda_env")
    @classmethod
    def valid_conda(cls, value: str | None) -> str | None:
        if value is not None and not _CONDA.fullmatch(value):
            raise ValueError("Conda 환경 이름에는 영문, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다.")
        return value


class SubmitRequest(StrictModel):
    request_id: str
    confirmed: bool

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value: str) -> str:
        if not _REQUEST_ID.fullmatch(value):
            raise ValueError("요청 ID 형식이 올바르지 않습니다.")
        return value


class LocalJobId(StrictModel):
    value: str

    @field_validator("value")
    @classmethod
    def valid_local_id(cls, value: str) -> str:
        if not _LOCAL_ID.fullmatch(value):
            raise ValueError("작업 ID 형식이 올바르지 않습니다.")
        return value
