"""앱 수명 동안 SSH/Mock 연결 하나를 재사용한다."""

from __future__ import annotations

import threading
from typing import Any

from seraph import connection as connection_module

from .errors import ApiError
from .remote import MockRemote, SSHRemote, create_remote


class ConnectionManager:
    def __init__(self, config: Any):
        self.config = config
        self.connection: Any | None = None
        self.remote: MockRemote | SSHRemote | None = None
        self.last_error: str | None = None
        self.username: str | None = None
        self.host: str = config.host
        self.port: int = config.port
        self._lock = threading.RLock()

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def connected(self) -> bool:
        if self.connection is None or self.remote is None:
            return False
        # 전송이 죽었는데 연결된 척하면, 화면은 멀쩡해 보이는데 누르는 것마다
        # 무한 대기한다. is_active() 는 네트워크를 타지 않아 health 를 느리게 하지 않는다.
        client = getattr(self.connection, "client", None)
        transport = client.get_transport() if client is not None else None
        if transport is not None and not transport.is_active():
            return False
        return True

    def connect(
        self,
        password: str | None = None,
        *,
        username: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        with self._lock:
            if username is not None:
                self.username = username
            if host is not None:
                self.host = host
            if port is not None:
                self.port = port
            self.close()
            try:
                if self.config.mode == "ssh":
                    if not self.username:
                        raise ApiError(
                            "SSH_USERNAME_REQUIRED",
                            "SERAPH에서 사용하는 실제 사용자명을 입력하세요.",
                            status_code=422,
                        )
                    conn = connection_module.SSHConnection(
                        self.host,
                        username=self.username,
                        port=self.port,
                        password=password,
                        config=self.config,
                        ask_password=lambda *_: None,
                    )
                else:
                    conn = connection_module.MockConnection(config=self.config)
                remote = create_remote(conn)
            except Exception as exc:
                if isinstance(exc, ApiError):
                    raise
                if isinstance(exc, connection_module.AuthError):
                    self.last_error = "AuthError"
                    raise ApiError(
                        "SSH_AUTH_FAILED",
                        "SSH 인증에 실패했습니다. 사용자명과 비밀번호를 다시 확인하세요.",
                        status_code=401,
                        retryable=True,
                    ) from exc
                self.last_error = type(exc).__name__
                raise ApiError(
                    "SERAPH_UNREACHABLE",
                    "SERAPH에 연결할 수 없습니다. SSH 설정과 인증정보를 확인하세요.",
                    status_code=503,
                    retryable=True,
                ) from exc
            self.connection = conn
            self.remote = remote
            self.last_error = None

    def require_connection(self) -> Any:
        if self.connection is None:
            raise ApiError(
                "SERAPH_UNREACHABLE",
                "SERAPH에 연결되어 있지 않습니다.",
                status_code=503,
                retryable=True,
            )
        return self.connection

    def require_remote(self) -> MockRemote | SSHRemote:
        if self.remote is None:
            raise ApiError(
                "SERAPH_UNREACHABLE",
                "SERAPH에 연결되어 있지 않습니다.",
                status_code=503,
                retryable=True,
            )
        return self.remote

    def snapshot(self) -> Any:
        """연결이 끊겼을 때 한 번만 다시 연결하고 Snapshot을 재시도한다."""
        conn = self.require_connection()
        try:
            return conn.snapshot()
        except Exception as first:
            if self.config.mode != "ssh":
                raise
            try:
                self.connect(username=self.username, host=self.host, port=self.port)
                return self.require_connection().snapshot()
            except Exception:
                raise first

    def close(self) -> None:
        remote, conn = self.remote, self.connection
        self.remote = None
        self.connection = None
        if remote is not None:
            try:
                remote.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
