"""Slurm Snapshot 캐시와 중복 갱신 방지."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from anyio import to_thread

from seraph import services

from .errors import ApiError


@dataclass
class CachedSnapshot:
    value: Any
    created_at: float
    warning: str | None = None


class SnapshotCache:
    def __init__(self, get_connection: Callable[[], Any], ttl_seconds: float = 7):
        self._get_connection = get_connection
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self._cached: CachedSnapshot | None = None
        self._lock = asyncio.Lock()
        self._last_forced_refresh = 0.0

    @property
    def age_seconds(self) -> float | None:
        if self._cached is None:
            return None
        return max(0.0, time.monotonic() - self._cached.created_at)

    def invalidate(self) -> None:
        self._cached = None

    def _fresh(self) -> bool:
        age = self.age_seconds
        return age is not None and age < self.ttl_seconds

    async def get(self, *, force: bool = False) -> CachedSnapshot:
        if not force and self._fresh():
            return self._cached  # type: ignore[return-value]

        async with self._lock:
            if not force and self._fresh():
                return self._cached  # type: ignore[return-value]

            # 직전 Snapshot이 로그인 노드 과부하를 말하면 추가 명령을 보내지 않는다.
            if self._cached is not None and not services.should_poll(self._cached.value):
                self._cached.warning = (
                    "로그인 노드 부하가 높아 기존 상태를 유지하고 있습니다."
                )
                return self._cached

            try:
                conn = self._get_connection()
                snapshot = await to_thread.run_sync(conn.snapshot)
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError(
                    "SERAPH_UNREACHABLE",
                    "SERAPH 상태를 가져오지 못했습니다.",
                    status_code=503,
                    retryable=True,
                ) from exc

            self._cached = CachedSnapshot(snapshot, time.monotonic())
            return self._cached

    async def force_refresh(self) -> CachedSnapshot:
        now = time.monotonic()
        if now - self._last_forced_refresh < 3.0:
            raise ApiError(
                "REFRESH_IN_PROGRESS",
                "새로고침은 3초에 한 번만 할 수 있습니다.",
                status_code=409,
                retryable=True,
            )
        self._last_forced_refresh = now
        return await self.get(force=True)

