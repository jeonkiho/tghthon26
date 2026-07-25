"""끝난 일을 알려준다 (작업 완료 · 환경 준비 완료).

이 도구를 쓰는 이유는 터미널 앞에 앉아 있지 않기 위해서다. 그런데 학습은 몇
시간, 환경 빌드는 20분씩 걸리고, 그동안 화면을 들여다보게 만들면 터미널에서
`watch squeue` 를 치던 것과 다를 게 없다.

**왜 프론트가 아니라 백엔드가 감시하나**: 화면 쪽 폴링은 탭이 가려지면 멈추고
(그때가 바로 알림이 필요한 순간이다) 탭을 닫으면 사라진다. 백엔드는 사용자 PC
에서 계속 돌고 있으니 여기서 보는 게 맞다.

**중복은 이렇게 막는다**: 브라우저가 최근에 이벤트를 받아 갔으면 화면이 알림을
띄운다고 보고 OS 알림은 건너뛴다. 브라우저를 닫아 소식이 끊기면 그때부터 OS
알림이 나간다. 같은 일로 두 번 울리지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from . import os_notify

log = logging.getLogger("seraph_gui.watcher")

# 서버에 묻는 주기. 로그인 노드를 아끼려고 느리게 잡는다 — 몇 시간짜리 학습에
# 30초 늦게 알려주는 건 아무 문제가 아니다.
INTERVAL_SECONDS = 30

# 이 시간 안에 브라우저가 이벤트를 받아 갔으면 화면이 알림을 맡는다.
# 폴링 주기(15초)의 세 배로 잡아, 한두 번 걸러도 중복으로 울리지 않게 한다.
CLIENT_ALIVE_SECONDS = 50

# 보관할 이벤트 수. 알림은 지나가면 그만이라 이력을 길게 들고 있을 이유가 없다.
MAX_EVENTS = 100

_JOB_HEADLINE = {
    "COMPLETED": ("작업 완료", "정상적으로 끝났습니다."),
    "FAILED": ("작업 실패", "종료 코드가 0이 아닙니다. 로그를 확인하세요."),
    "TIMEOUT": ("작업 시간 초과", "시간 제한에 걸려 중단됐습니다."),
    "CANCELLED": ("작업 취소됨", "취소되어 중단됐습니다."),
    "OUT_OF_MEMORY": ("작업 실패 · 메모리 부족", "메모리가 모자라 죽었습니다."),
    "NODE_FAIL": ("작업 실패 · 노드 장애", "실행 중이던 노드에 문제가 생겼습니다."),
}


class Watcher:
    """진행 중인 작업과 환경 빌드를 지켜보다가 끝나면 알린다."""

    def __init__(self, manager: Any, jobs: Any, envs: Any, *,
                 interval: float = INTERVAL_SECONDS):
        self.manager = manager
        self.jobs = jobs
        self.envs = envs
        self.interval = interval
        # 백엔드가 다시 뜨면 이벤트 번호가 1부터 시작한다. 화면이 "42번까지 봤다"를
        # 들고 있으면 새 이벤트를 영영 못 알아보므로, 세션이 바뀐 걸 알려준다.
        self.session_id = uuid.uuid4().hex[:12]
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._sequence = 0
        self._jobs_watched: dict[str, str] = {}      # local_job_id -> job_name
        self._builds_watched: dict[str, str] = {}    # build_id -> env_name
        self._last_client_seen = 0.0
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    # --- 수명 -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="seraph-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5)

    # --- 등록 -------------------------------------------------------------

    def watch_job(self, local_job_id: str, job_name: str) -> None:
        with self._lock:
            self._jobs_watched[local_job_id] = job_name
        self._wake.set()

    def watch_build(self, build_id: str, env_name: str) -> None:
        with self._lock:
            self._builds_watched[build_id] = env_name
        self._wake.set()

    def running_builds(self) -> list[dict[str, str]]:
        """지금 돌고 있는 환경 빌드. 화면이 자기가 시작하지 않은 빌드도 이어서 볼 수 있게.

        화면은 자기가 시작한 빌드 번호를 브라우저에 저장해 두는데, 다른 브라우저로
        열거나 저장소를 지우면 그 번호가 없다. 그러면 "다시 열면 진행 상황이
        이어집니다"라고 써 놓고 정작 아무것도 안 보여주게 된다.
        """
        with self._lock:
            return [{"build_id": build_id, "name": name}
                    for build_id, name in self._builds_watched.items()]

    def resync(self) -> None:
        """서버에 지금 진행 중인 작업이 뭐가 있는지 다시 물어본다.

        백엔드 재시작은 흔하다(코드를 고칠 때마다 한다). 재시작했다고 이미 제출한
        작업의 완료를 놓치면, 알림 기능이 있으나 마나 해진다.
        """
        try:
            listed = self.jobs.list(limit=50)
        except Exception as exc:                    # noqa: BLE001 - 연결 전이면 그냥 넘어간다
            log.debug("작업 재동기화 건너뜀: %s", exc)
            return
        with self._lock:
            for job in listed.get("jobs", []):
                if job.get("status") in _ACTIVE and job.get("local_job_id"):
                    self._jobs_watched[job["local_job_id"]] = job.get("job_name") or "작업"
        self._wake.set()

    # --- 화면이 가져가는 것 ------------------------------------------------

    def events(self, since: int | None, session: str | None, *,
               can_notify: bool = False) -> dict[str, Any]:
        """새 이벤트를 준다. 이 호출 자체가 '브라우저가 살아 있다'는 신호다.

        단, **알림을 띄울 수 있는** 브라우저만 신호로 친다. 사용자가 알림 권한을
        거부했는데 백엔드까지 조용히 있으면, 창을 열어둔 채로 아무 소식도 못 받는
        가장 나쁜 상태가 된다.
        """
        with self._lock:
            if can_notify:
                self._last_client_seen = time.monotonic()
            fresh = self._events if since is None else [
                event for event in self._events if event["id"] > since
            ]
            # 세션이 다르면 화면이 들고 있던 번호는 남의 번호다. 알림을 쏟아내지
            # 않도록 지금 시점을 기준으로 다시 맞추게 한다.
            stale_session = session is not None and session != self.session_id
            return {
                "session": self.session_id,
                "latest_id": self._sequence,
                "events": [] if stale_session else fresh,
                "reset": stale_session,
                "watching": {
                    "jobs": len(self._jobs_watched),
                    "builds": len(self._builds_watched),
                },
                "os_notify": os_notify.available(),
            }

    # --- 감시 루프 --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stopped.is_set():
            # 볼 게 없으면 서버를 귀찮게 하지 않는다. 등록될 때 깨운다.
            with self._lock:
                idle = not self._jobs_watched and not self._builds_watched
            self._wake.wait(timeout=self.interval if not idle else self.interval * 4)
            self._wake.clear()
            if self._stopped.is_set():
                return
            if not self.manager.connected:
                continue
            try:
                self._tick()
            except Exception as exc:                # noqa: BLE001 - 루프는 절대 죽으면 안 된다
                log.warning("감시 한 바퀴 실패: %s", exc)

    def _tick(self) -> None:
        with self._lock:
            jobs = dict(self._jobs_watched)
            builds = dict(self._builds_watched)

        for local_job_id, name in jobs.items():
            try:
                detail = self.jobs.get(local_job_id)
            except Exception as exc:                # noqa: BLE001
                log.debug("작업 %s 상태 확인 실패: %s", local_job_id, exc)
                continue
            job = detail.get("job") or {}
            status = job.get("status")
            if status in _ACTIVE:
                continue
            with self._lock:
                self._jobs_watched.pop(local_job_id, None)
            if status:
                self._emit(_job_event(job, status, name))

        for build_id, name in builds.items():
            try:
                state = self.envs.build_status(build_id).get("build") or {}
            except Exception as exc:                # noqa: BLE001
                log.debug("빌드 %s 상태 확인 실패: %s", build_id, exc)
                continue
            if state.get("state") == "running":
                continue
            with self._lock:
                self._builds_watched.pop(build_id, None)
            self._emit(_env_event(state, name))

    def _emit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            event = {**event, "id": self._sequence, "created_at": _now_iso()}
            self._events.append(event)
            del self._events[:-MAX_EVENTS]
            browser_alive = (time.monotonic() - self._last_client_seen) < CLIENT_ALIVE_SECONDS
        log.info("알림: %s — %s", event["title"], event["body"])
        # 브라우저가 받아 가고 있으면 화면이 띄운다. 둘 다 띄우면 두 번 울린다.
        if not browser_alive:
            os_notify.send(event["title"], event["body"])


_ACTIVE = {"SUBMITTING", "SUBMITTED", "PENDING", "RUNNING", "COMPLETING", "CANCEL_REQUESTED"}


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _job_event(job: dict[str, Any], status: str, name: str) -> dict[str, Any]:
    headline, detail = _JOB_HEADLINE.get(status, ("작업 종료", f"상태: {status}"))
    job_name = job.get("job_name") or name
    slurm = job.get("slurm_job_id")
    where = f"Slurm #{slurm}" if slurm else "제출 전"
    return {
        "kind": "job",
        "ok": status == "COMPLETED",
        "title": f"{headline} · {job_name}",
        "body": f"{detail} ({where})",
        "ref": {"local_job_id": job.get("local_job_id"), "status": status},
    }


def _env_event(build: dict[str, Any], name: str) -> dict[str, Any]:
    env_name = build.get("name") or name
    ok = build.get("state") == "succeeded"
    return {
        "kind": "env",
        "ok": ok,
        "title": f"{'환경 준비 완료' if ok else '환경 만들기 실패'} · {env_name}",
        "body": ("이제 새 작업에서 이 환경을 고를 수 있습니다."
                 if ok else build.get("message") or "빌드 로그를 확인하세요."),
        "ref": {"env": env_name, "build_id": build.get("build_id")},
    }
