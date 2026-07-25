"""127.0.0.1에서만 실행하는 SERAPH GUI FastAPI 앱."""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Annotated, Any

from anyio import to_thread
from fastapi import FastAPI, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from seraph import config as config_module
from seraph import clusters, history, placement, sbatch, services, slack, tutorial

from .cache import SnapshotCache
from .dependencies import ConnectionManager
from .env_service import EnvService
from .errors import ApiError, install_error_handlers
from .job_service import JobService
from .local_picker import select_code_path, select_dataset_archive
from .occupancy_history import OccupancyHistory
from .remote import PREVIEW_MAX_BYTES
from .watcher import Watcher
from .schemas import (
    ConnectRequest,
    EnvSpec,
    JobSpec,
    PreviewRequest,
    RecommendationRequest,
    SubmitRequest,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCAL_JOB_ID = Annotated[str, Path(pattern=r"^[a-f0-9]{8,32}$")]
BUILD_ID = Annotated[str, Path(pattern=r"^[a-f0-9]{12}$")]
ENV_NAME = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("seraph_gui")


def create_app(config: Any | None = None, *, auto_connect: bool = True) -> FastAPI:
    cfg = config or config_module.load()
    manager = ConnectionManager(cfg)
    cache = SnapshotCache(lambda: manager, ttl_seconds=cfg.poll_interval)
    jobs = JobService(manager)
    envs = EnvService(manager)
    occupancy = OccupancyHistory()
    watcher = Watcher(manager, jobs, envs)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # SSH 비밀번호는 저장하지 않으므로 시작 시 무자격 자동 접속을 시도하지 않는다.
        # Mock 모드만 자동 연결하고 SSH는 연결 화면의 명시적 요청을 기다린다.
        if auto_connect and manager.mode != "ssh":
            try:
                await to_thread.run_sync(manager.connect)
            except ApiError:
                log.warning("SERAPH mock initial connection failed")
            else:
                await to_thread.run_sync(watcher.resync)
        watcher.start()
        yield
        watcher.stop()
        await to_thread.run_sync(manager.close)

    app = FastAPI(
        title="SERAPH GUI API",
        version="1.1.1",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.manager = manager
    app.state.cache = cache
    app.state.jobs = jobs
    app.state.envs = envs
    app.state.occupancy = occupancy
    app.state.watcher = watcher
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    async def cached_snapshot() -> tuple[Any, dict[str, Any]]:
        cached = await cache.get()
        # 새 Snapshot 이면 점유 추세에 한 점을 남긴다(중복은 내부에서 무시).
        occupancy.record(cached)
        meta = {
            "age_seconds": round(cache.age_seconds or 0.0, 3),
            "warning": cached.warning,
        }
        return cached.value, meta

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "mode": manager.mode,
            "seraph_reachable": manager.connected,
            "ssh_host": manager.host,
            "ssh_port": manager.port,
            "ssh_username": manager.username,
            "snapshot_age_seconds": (
                round(cache.age_seconds, 3) if cache.age_seconds is not None else None
            ),
        }

    @app.post("/api/v1/session/connect")
    async def connect_session(body: ConnectRequest) -> dict[str, Any]:
        password = body.password.get_secret_value() if body.password else None
        await to_thread.run_sync(lambda: manager.connect(
            password=password,
            username=body.username,
            host=body.host,
            port=body.port,
        ))
        cache.invalidate()
        snapshot, _ = await cached_snapshot()
        identity = services.whoami(snapshot)
        if manager.mode == "ssh" and identity.get("user") and identity["user"] != manager.username:
            await to_thread.run_sync(manager.close)
            cache.invalidate()
            raise ApiError(
                "SSH_USERNAME_MISMATCH",
                "입력한 사용자명과 SERAPH가 확인한 로그인 계정이 다릅니다.",
                status_code=422,
            )
        # 백엔드를 재시작하면 감시 목록이 비어 있다. 로그인 직후 서버에 다시 물어
        # 진행 중인 작업을 되찾지 않으면, 재시작 전에 낸 작업의 완료를 놓친다.
        await to_thread.run_sync(watcher.resync)
        return {"ok": True, "mode": manager.mode, "me": identity}

    @app.post("/api/v1/session/disconnect")
    async def disconnect_session() -> dict[str, Any]:
        await to_thread.run_sync(manager.close)
        cache.invalidate()
        return {"ok": True, "mode": manager.mode, "seraph_reachable": manager.connected}

    @app.get("/api/v1/me")
    async def me() -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        return {"ok": True, **services.whoami(snapshot), "cache": cache_meta}

    @app.get("/api/v1/tutorial")
    async def get_tutorial_route() -> dict[str, Any]:
        # 튜토리얼은 항상 mock 스냅샷 위에서 돈다(실서버 무관). SSH 연결 없이도 동작.
        result = await to_thread.run_sync(tutorial.get_tutorial)
        return {"ok": True, **result}

    @app.get("/api/v1/announcements")
    async def announcements_route() -> dict[str, Any]:
        # Slack 공지는 세라프 SSH 와 무관하다(토큰 없으면 mock). 실패해도 예외를 던지지 않는다.
        def _fetch() -> dict[str, Any]:
            client = slack.connect(cfg)
            return slack.get_announcements(client, cfg.slack_channel, cfg.slack_limit)
        return await to_thread.run_sync(_fetch)

    @app.post("/api/v1/local/select-code")
    async def select_local_code(
        kind: Annotated[str, Query(pattern=r"^(directory|file)$")] = "directory",
    ) -> dict[str, Any]:
        selected = await to_thread.run_sync(lambda: select_code_path(kind))
        return {"ok": True, "selected": bool(selected), "path": selected}

    # --- NAS 탐색·업로드 ---------------------------------------------------
    # 데이터 경로를 눈 감고 타이핑하게 만들면 "터미널 없이"가 거짓말이 된다.

    @app.get("/api/v1/remote/ls")
    async def remote_ls(
        path: str | None = None,
        show_hidden: bool = False,
        dirs_only: bool = False,
    ) -> dict[str, Any]:
        remote = jobs.remote
        target = path or remote.data_root
        return {"ok": True, **await to_thread.run_sync(
            lambda: remote.list_entries(target, show_hidden=show_hidden, dirs_only=dirs_only))}

    @app.get("/api/v1/remote/file")
    async def remote_file(
        path: str,
        max_bytes: Annotated[int, Query(ge=1024, le=PREVIEW_MAX_BYTES)] = PREVIEW_MAX_BYTES,
    ) -> dict[str, Any]:
        # 결과 파일 한 줄 보려고 터미널을 여는 일을 없앤다. 목록과 같은 규칙이다 —
        # 서버가 권한을 강제하고, 못 읽는 곳은 그냥 실패한다.
        remote = jobs.remote
        return {"ok": True, **await to_thread.run_sync(
            lambda: remote.preview_file(path, max_bytes=max_bytes))}

    @app.get("/api/v1/remote/conda")
    async def remote_conda() -> dict[str, Any]:
        remote = jobs.remote
        return {"ok": True, **await to_thread.run_sync(remote.find_conda)}

    @app.post("/api/v1/remote/datasets/upload")
    async def upload_dataset(local_path: str | None = None) -> dict[str, Any]:
        # local_path 를 안 주면 사용자 PC 의 파일 선택창을 연다(백엔드가 로컬에서 돈다).
        chosen = local_path or await to_thread.run_sync(select_dataset_archive)
        if not chosen:
            return {"ok": True, "selected": False, "dataset": None}
        remote = jobs.remote
        info = await to_thread.run_sync(lambda: remote.upload_dataset(chosen))
        return {"ok": True, "selected": True, "dataset": info}

    # --- 알림 ---------------------------------------------------------------
    # 몇 시간짜리 학습을 화면 보면서 기다리게 하면 `watch squeue` 와 다를 게 없다.
    # 이 호출은 새 이벤트를 가져가는 동시에 "브라우저가 살아 있다"는 신호가 된다 —
    # 브라우저가 있으면 화면이, 없으면 백엔드가 OS 알림을 띄운다(둘 다는 아니다).

    @app.get("/api/v1/events")
    async def events(
        since: Annotated[int | None, Query(ge=0)] = None,
        session: Annotated[str | None, Query(pattern=r"^[a-f0-9]{12}$")] = None,
        can_notify: bool = False,
    ) -> dict[str, Any]:
        # can_notify 가 거짓이면 화면은 알림 권한이 없다는 뜻이다. 그때도 백엔드가
        # 조용히 있으면 브라우저를 열어둔 채로 아무 소식도 못 받는다.
        return {"ok": True, **watcher.events(since, session, can_notify=can_notify)}

    # --- 개인 환경 만들기 ---------------------------------------------------
    # 공용 설치는 읽기 전용이라 원하는 torch 버전을 넣을 수 없었고, 그 순간
    # 학생은 이 도구를 닫고 터미널을 열었다. 마지막 남은 터미널 의존이다.

    @app.get("/api/v1/envs/tools")
    async def env_tools() -> dict[str, Any]:
        return {"ok": True, **await to_thread.run_sync(envs.tools)}

    @app.get("/api/v1/envs/builds/{build_id}")
    async def env_build(build_id: BUILD_ID) -> dict[str, Any]:
        return {"ok": True, **await to_thread.run_sync(lambda: envs.build_status(build_id))}

    @app.get("/api/v1/envs")
    async def list_envs() -> dict[str, Any]:
        listed = await to_thread.run_sync(envs.list_envs)
        # 진행 중인 빌드를 같이 준다. 화면이 자기가 시작하지 않은 빌드(다른 브라우저,
        # 저장소 삭제, 백엔드 재시작)도 이어서 볼 수 있어야 한다.
        return {"ok": True, **listed, "running_builds": watcher.running_builds()}

    @app.post("/api/v1/envs")
    async def create_env(body: EnvSpec) -> dict[str, Any]:
        result = await to_thread.run_sync(lambda: envs.create(body))
        watcher.watch_build(result["build"]["build_id"], result["build"]["name"])
        return {"ok": True, **result}

    @app.delete("/api/v1/envs/{name}")
    async def delete_env(name: ENV_NAME) -> dict[str, Any]:
        return {"ok": True, **await to_thread.run_sync(lambda: envs.delete(name))}

    @app.get("/api/v1/cluster/status")
    async def cluster_status(partition: str | None = None) -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        return {"ok": True, **services.get_gpu_status(snapshot, partition), "cache": cache_meta}

    @app.get("/api/v1/cluster/nodes")
    async def cluster_nodes(
        partition: str | None = None,
        gpus: Annotated[int, Query(ge=1, le=16)] = 1,
        high_perf: bool = False,
    ) -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        nodes = services.get_node_availability(
            snapshot, partition=partition, need_gpus=gpus, high_perf=high_perf
        )
        return {"ok": True, "nodes": nodes, "count": len(nodes), "cache": cache_meta}

    @app.get("/api/v1/cluster/partitions")
    async def cluster_partitions() -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        return {"ok": True, "partitions": services.get_partitions(snapshot), "cache": cache_meta}

    @app.get("/api/v1/clusters")
    async def clusters_route() -> dict[str, Any]:
        # 3개 클러스터 안내(정적). 실시간 아님, SSH 무관.
        return {"ok": True, **clusters.overview()}

    @app.get("/api/v1/cluster/usage")
    async def cluster_usage() -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        return {"ok": True, **services.get_my_usage(snapshot, snapshot.me), "cache": cache_meta}

    @app.post("/api/v1/cluster/refresh")
    async def cluster_refresh() -> dict[str, Any]:
        cached = await cache.force_refresh()
        occupancy.record(cached)
        return {
            "ok": True,
            "refreshed": cached.warning is None,
            "warning": cached.warning,
            "snapshot_age_seconds": round(cache.age_seconds or 0.0, 3),
        }

    @app.get("/api/v1/queue")
    async def cluster_queue(partition: str | None = None) -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        return {"ok": True, **services.get_queue(snapshot, partition), "cache": cache_meta}

    @app.get("/api/v1/cluster/history")
    async def cluster_history() -> dict[str, Any]:
        samples = occupancy.samples()
        return {"ok": True, "samples": samples, "count": len(samples)}

    @app.post("/api/v1/recommendations")
    async def recommendations(body: RecommendationRequest) -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        conn = manager.require_connection()
        result = await to_thread.run_sync(
            lambda: placement.find_fastest(
                conn,
                snapshot,
                gpus=body.gpus,
                hours=body.hours,
                high_perf=body.high_perf,
                node=body.node,
            )
        )
        return {"ok": True, **result, "cache": cache_meta}

    @app.post("/api/v1/jobs/preview")
    async def preview_job(body: PreviewRequest) -> dict[str, Any]:
        snapshot, _ = await cached_snapshot()
        built = sbatch.generate_sbatch(
            snapshot,
            name=body.name,
            command=body.command,
            partition=body.partition,
            gpus=body.gpus,
            high_perf=body.high_perf,
            cpus=body.cpus,
            mem=body.memory,
            time_limit=body.time_limit,
            node=body.node,
            paths=body.paths,
        )
        return built

    @app.get("/api/v1/jobs/diagnosis")
    async def diagnose_jobs(partition: str | None = None) -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        return {
            "ok": True,
            **services.diagnose_pending(snapshot, snapshot.me, partition),
            "cache": cache_meta,
        }

    @app.post("/api/v1/jobs/validate")
    async def validate_job(body: JobSpec) -> dict[str, Any]:
        snapshot, _ = await cached_snapshot()
        return await to_thread.run_sync(lambda: jobs.validate(body, snapshot))

    @app.post("/api/v1/jobs/prepare")
    async def prepare_job(body: JobSpec) -> dict[str, Any]:
        snapshot, _ = await cached_snapshot()
        return await to_thread.run_sync(lambda: jobs.prepare(body, snapshot))

    @app.get("/api/v1/jobs/history")
    async def job_history(
        days: Annotated[int, Query(ge=1, le=60)] = 7,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        snapshot, _ = await cached_snapshot()
        conn = manager.require_connection()
        raw = await to_thread.run_sync(lambda: conn.sacct(days, snapshot.me))
        return {"ok": True, **history.get_job_history(raw, limit=limit)}

    @app.get("/api/v1/jobs/slurm/{job_id}")
    async def slurm_job_result(
        job_id: Annotated[str, Path(pattern=r"^[0-9]+$")],
        days: Annotated[int, Query(ge=1, le=60)] = 7,
    ) -> dict[str, Any]:
        snapshot, _ = await cached_snapshot()
        conn = manager.require_connection()
        raw = await to_thread.run_sync(lambda: conn.sacct(days, snapshot.me))
        result = history.get_job_result(raw, job_id)
        if not result["found"]:
            raise ApiError("JOB_NOT_FOUND", "완료 작업을 찾을 수 없습니다.", 404)
        return {"ok": True, **result}

    @app.get("/api/v1/jobs/eta/{job_id}")
    async def job_eta(
        job_id: Annotated[str, Path(pattern=r"^[0-9]+$")],
    ) -> dict[str, Any]:
        snapshot, cache_meta = await cached_snapshot()
        result = services.estimate_wait_time(snapshot, job_id)
        if not result.get("found"):
            raise ApiError("JOB_NOT_FOUND", "대기열에서 작업을 찾을 수 없습니다.", 404)
        return {"ok": True, **result, "cache": cache_meta}

    @app.get("/api/v1/jobs")
    async def list_jobs(limit: Annotated[int, Query(ge=1, le=100)] = 50) -> dict[str, Any]:
        return await to_thread.run_sync(lambda: jobs.list(limit=limit))

    @app.get("/api/v1/jobs/{local_job_id}")
    async def get_job(local_job_id: LOCAL_JOB_ID) -> dict[str, Any]:
        return await to_thread.run_sync(lambda: jobs.get(local_job_id))

    @app.post("/api/v1/jobs/{local_job_id}/submit")
    async def submit_job(local_job_id: LOCAL_JOB_ID, body: SubmitRequest) -> dict[str, Any]:
        snapshot, _ = await cached_snapshot()
        result = await to_thread.run_sync(
            lambda: jobs.submit(
                local_job_id,
                request_id=body.request_id,
                confirmed=body.confirmed,
                snapshot=snapshot,
            )
        )
        job = result.get("job") or {}
        watcher.watch_job(local_job_id, job.get("job_name") or "작업")
        return result

    @app.delete("/api/v1/jobs/{local_job_id}")
    async def delete_job(local_job_id: LOCAL_JOB_ID) -> dict[str, Any]:
        return await to_thread.run_sync(lambda: jobs.delete(local_job_id))

    @app.post("/api/v1/jobs/{local_job_id}/preflight")
    async def preflight_job(local_job_id: LOCAL_JOB_ID) -> dict[str, Any]:
        return await to_thread.run_sync(lambda: jobs.preflight(local_job_id))

    @app.get("/api/v1/jobs/{local_job_id}/logs")
    async def job_logs(
        local_job_id: LOCAL_JOB_ID,
        max_bytes: Annotated[int, Query(ge=1024, le=1_000_000)] = 128_000,
    ) -> dict[str, Any]:
        return await to_thread.run_sync(lambda: jobs.logs(local_job_id, max_bytes=max_bytes))

    @app.post("/api/v1/jobs/{local_job_id}/cancel")
    async def cancel_job(local_job_id: LOCAL_JOB_ID) -> dict[str, Any]:
        return await to_thread.run_sync(lambda: jobs.cancel(local_job_id))

    static_dir = ROOT / "frontend" / "dist"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="gui")

    return app


app = create_app()
