"""127.0.0.1에서만 실행하는 SERAPH GUI FastAPI 앱."""

from __future__ import annotations

import logging
import pathlib
import urllib.parse
from contextlib import asynccontextmanager
from typing import Annotated, Any

from anyio import to_thread
from fastapi import FastAPI, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from seraph import config as config_module
from seraph import clusters, history, placement, sbatch, services, slack, tutorial

from .cache import SnapshotCache
from .dependencies import ConnectionManager
from . import env_service as env_service_module
from .env_service import EnvService
from .errors import ApiError, install_error_handlers
from .job_service import JobService
from .local_picker import select_any_file, select_code_path, select_dataset_archive
from .occupancy_history import OccupancyHistory
from .remote import PREVIEW_MAX_BYTES
from .watcher import Watcher
from .schemas import (
    ConnectRequest,
    EnvSpec,
    JobSpec,
    MakeFolderRequest,
    PreviewRequest,
    RecommendationRequest,
    RemotePathRequest,
    RenameRequest,
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

# 화면이 아직 안 만들어졌을 때 보여줄 안내. 빈 404 는 "고장났다"도 "내가 뭘 해야
# 한다"도 알려주지 않는다.
_FRONTEND_MISSING_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>SERAPH — 화면 준비 필요</title>
<style>
 body{font-family:"Malgun Gothic",system-ui,sans-serif;background:#f4f6fa;color:#182136;
      margin:0;display:grid;place-items:center;min-height:100vh}
 main{max-width:560px;padding:32px}
 h1{font-size:22px;margin:0 0 12px}
 p{font-size:13px;line-height:1.8;color:#5c6880;margin:0 0 14px}
 code{background:#e9edf4;border-radius:5px;padding:2px 7px;font-size:12px}
 a{color:#21bd91}
</style></head><body><main>
 <h1>화면이 아직 만들어지지 않았습니다</h1>
 <p>백엔드는 정상입니다. 화면(<code>frontend/dist</code>)은 저장소에 두지 않고
    시작할 때 만드는데, 아직 만들어지지 않았습니다.</p>
 <p><code>start_windows.bat</code> 또는 <code>./start_unix.sh</code> 로 다시 실행하면
    자동으로 만듭니다. 그래도 안 되면 <a href="https://nodejs.org">Node.js</a> 가
    설치돼 있는지 확인하세요 — 화면을 만들 때만 필요합니다.</p>
 <p>API 는 지금도 쓸 수 있습니다: <a href="/api/docs">/api/docs</a></p>
</main></body></html>"""


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
        # 접속한 클러스터의 내 NAS 경로. 화면이 결과 경로 기본값을 만들 때 쓴다
        # (클러스터마다 다르다: ariel /nas2/data, moana /data).
        return {"ok": True, **services.whoami(snapshot),
                "data_root": jobs.remote.data_root, "cache": cache_meta}

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
            body = slack.get_announcements(client, cfg.slack_channel, cfg.slack_limit)
            # 토큰이 없으면 예시 공지가 나가는데, 예전에는 그게 진짜인지 예시인지
            # 화면에서 알 방법이 없었다. 토큰을 넣어두고도 예시가 계속 보이면
            # 어디를 봐야 하는지 알 수 없다 — 그래서 출처를 같이 준다.
            live = not isinstance(client, slack.MockSlackClient)
            return {**body, "source": "slack" if live else "mock"}
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

    @app.post("/api/v1/envs/detect")
    async def detect_env_spec(local_path: str | None = None) -> dict[str, Any]:
        """코드 폴더에서 environment.yml / requirements.txt 를 찾는다.

        깃에 커밋된 스펙 파일로 환경을 만드는 건 파이썬 쪽에서 가장 표준적인
        방식인데, 이 화면은 지금까지 패키지를 손으로 타이핑하게 했다 — 이미
        저장소에 정답이 적혀 있는데도.
        """
        chosen = local_path or await to_thread.run_sync(lambda: select_code_path("directory"))
        if not chosen:
            return {"ok": True, "selected": False, "specs": [], "unsupported": []}
        found = await to_thread.run_sync(lambda: env_service_module.detect_spec_files(chosen))
        return {"ok": True, "selected": True, **found}

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

    # --- 탐색기 쓰기 --------------------------------------------------------
    # 이 도구의 첫 파괴적 기능이다. NAS 에는 휴지통이 없어서 한 번 지우면 끝이라,
    # 무엇이 사라지는지 먼저 세어 보여주고(describe) 지운다(delete).

    @app.post("/api/v1/remote/mkdir")
    async def remote_mkdir(body: MakeFolderRequest) -> dict[str, Any]:
        remote = jobs.remote
        return {"ok": True, **await to_thread.run_sync(
            lambda: remote.make_folder(body.path, body.name))}

    @app.post("/api/v1/remote/rename")
    async def remote_rename(body: RenameRequest) -> dict[str, Any]:
        remote = jobs.remote
        return {"ok": True, **await to_thread.run_sync(
            lambda: remote.rename_entry(body.path, body.new_name))}

    @app.get("/api/v1/remote/describe")
    async def remote_describe(path: str) -> dict[str, Any]:
        # "정말 지울까요?" 는 아무 정보도 주지 않는다. 몇 개가 몇 GB 사라지는지 센다.
        remote = jobs.remote
        return {"ok": True, **await to_thread.run_sync(lambda: remote.describe_target(path))}

    @app.post("/api/v1/remote/delete")
    async def remote_delete(body: RemotePathRequest) -> dict[str, Any]:
        remote = jobs.remote
        return {"ok": True, **await to_thread.run_sync(lambda: remote.delete_entry(body.path))}

    @app.post("/api/v1/remote/upload")
    async def remote_upload(path: str, local_path: str | None = None) -> dict[str, Any]:
        # local_path 를 안 주면 사용자 PC 의 파일 선택창을 연다(백엔드가 로컬에서 돈다).
        chosen = local_path or await to_thread.run_sync(select_any_file)
        if not chosen:
            return {"ok": True, "selected": False, "file": None}
        remote = jobs.remote
        info = await to_thread.run_sync(lambda: remote.upload_into(chosen, path))
        return {"ok": True, "selected": True, "file": info}

    @app.get("/api/v1/remote/download")
    async def remote_download(path: str) -> StreamingResponse:
        """서버 파일을 내 PC 로 내려받는다. 폴더면 tar.gz 로 묶어서 흘려보낸다.

        결과물을 보려고 scp 명령을 외우게 하지 않으려는 것이다. 브라우저의 다운로드
        기능을 그대로 쓰므로 진행률·이어받기·저장 위치를 브라우저가 처리한다.
        """
        remote = jobs.remote
        stream, meta = await to_thread.run_sync(lambda: remote.open_download(path))
        # 한글 파일명은 latin-1 헤더에 그대로 못 넣는다. RFC 5987 로 함께 준다.
        quoted = urllib.parse.quote(meta["filename"])
        headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"}
        if meta.get("size") is not None:
            headers["Content-Length"] = str(meta["size"])
        return StreamingResponse(stream, media_type=meta["media_type"], headers=headers)

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

    # 화면은 저장소에 커밋하지 않고 시작할 때 만든다(frontend_build.py). 그래서
    # 아직 안 만들어진 상태로 여기 올 수 있다 — 그때 빈 404 를 주면 사용자는
    # 무엇이 잘못됐는지 알 수 없다. 무엇을 해야 하는지 말해준다.
    static_dir = ROOT / "frontend" / "dist"
    if (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="gui")
    else:
        @app.get("/", include_in_schema=False)
        async def frontend_missing() -> HTMLResponse:
            return HTMLResponse(_FRONTEND_MISSING_HTML, status_code=503)

    return app


# 실제로 서버를 띄울 때만 비밀을 읽는다.
#
# 시작 스크립트에 맡기면 그 스크립트를 거치지 않는 실행(윈도우 배치, uvicorn 직접,
# 에디터의 실행 버튼)은 전부 토큰을 못 본다 — 토큰을 .env 에 넣어두고도 공지가
# 계속 예시로 나오던 이유가 이것이었다.
#
# 반대로 create_app() 안에서 읽으면 테스트마다 개발자의 진짜 토큰이 실려서,
# 공지 테스트가 실제 Slack 워크스페이스를 때리게 된다. 그래서 여기, 프로세스가
# 서버로 뜨는 지점에서만 읽는다.
config_module.load_env_file()
app = create_app()
