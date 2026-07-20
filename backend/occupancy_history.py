"""GPU 점유 추세를 메모리 안에 짧게 유지한다. 별도 DB 를 쓰지 않는다.

새 Snapshot 이 만들어질 때(폴링 주기마다)만 한 점을 찍는다. 같은 Snapshot 으로
여러 요청이 들어와도 중복으로 남기지 않는다. 서버를 끄면 사라진다 — 그래서
프론트는 표본이 적을 때(수집 중) 를 처리해야 한다.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from seraph import services


class OccupancyHistory:
    def __init__(self, maxlen: int = 240):
        self._samples: deque = deque(maxlen=maxlen)
        self._last_created = None

    def record(self, cached) -> None:
        """CachedSnapshot 을 받아, 처음 보는 것이면 점유 요약 한 점을 남긴다."""
        created = getattr(cached, "created_at", None)
        value = getattr(cached, "value", None)
        if created is None or value is None or created == self._last_created:
            return
        self._last_created = created
        try:
            status = services.get_gpu_status(value)
        except Exception:
            return  # 추세 기록이 실패해도 요청은 계속돼야 한다.
        self._samples.append({
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "partition": status["partition"],
            "utilization": status["utilization"],
            "total_gpus": status["total_gpus"],
            "used_gpus": status["used_gpus"],
            "free_gpus": status["free_gpus"],
            "free_high_perf_gpus": status["free_high_perf_gpus"],
            "free_standard_gpus": status["free_standard_gpus"],
            "pending_jobs": status["pending_jobs"],
            "running_jobs": status["running_jobs"],
        })

    def samples(self) -> list:
        return list(self._samples)
