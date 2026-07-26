import asyncio
import io
import tarfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend import dependencies
from backend.cache import SnapshotCache
from backend.main import create_app
from backend.remote import PACKAGE_HOSTS
from backend.schemas import ConnectRequest
from seraph import config as config_module
from seraph.connection import AuthError


def _job_payload(code: Path, **overrides):
    payload = {
        "name": "image-train",
        "local_code_path": str(code),
        "entrypoint": "train.py",
        "arguments": ["--data", "{dataset}", "--output", "{output}"],
        "dataset_path": "/data/datasets/tarfiles/images.tar.gz",
        "output_path": "/data/mockuser/results/image-train",
        "copy_dataset_to_local": True,
        "gpus": 1,
        "high_perf": False,
        "cpus": 8,
        "memory": "32G",
        "time_limit": "02:00:00",
    }
    payload.update(overrides)
    return payload


def test_health_and_dashboard_apis():
    app = create_app()
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "mock"
        assert health.json()["seraph_reachable"] is True
        assert health.json()["ssh_host"] == "ariel.khu.ac.kr"
        assert health.json()["ssh_port"] == 30080

        me = client.get("/api/v1/me").json()
        assert me["ok"] is True
        assert me["user"]
        assert me["default_partition"] in ("batch_grad", "batch_ugrad")

        assert client.get("/api/v1/cluster/status").json()["total_gpus"] > 0
        assert "partitions" in client.get("/api/v1/cluster/partitions").json()
        assert "gpus_in_use" in client.get("/api/v1/cluster/usage").json()
        assert "headline" in client.get("/api/v1/jobs/diagnosis").json()

        disconnected = client.post("/api/v1/session/disconnect").json()
        assert disconnected["ok"] is True
        assert disconnected["seraph_reachable"] is False

        overview = client.get("/api/v1/clusters").json()
        assert overview["primary"] == "ariel"
        assert set(overview["clusters"]) == {"ariel", "moana", "aurora"}
        # 실시간 여부는 이 정적 표가 아니라 whoami().connected_cluster 가 정한다.
        assert "connectable" not in overview["clusters"]["moana"]
        assert overview["clusters"]["moana"]["total_gpus"] == 105
        # 학과 -> 클러스터 라우팅을 화면이 다시 구현하지 않도록 서버가 내려준다.
        assert overview["routing"]["assign"]["ce:undergrad"] == "moana"
        assert overview["routing"]["assign"]["ce:grad"] == "ariel"


def test_queue_eta_and_history_apis():
    app = create_app()
    with TestClient(app) as client:
        queue = client.get("/api/v1/queue").json()
        assert queue["ok"] is True
        assert queue["pending_count"] == len(queue["pending"])
        assert queue["running_count"] == len(queue["running"])

        # /queue 요청이 스냅샷을 만들었으므로 추세 표본이 최소 1개 쌓인다.
        history_body = client.get("/api/v1/cluster/history").json()
        assert history_body["ok"] is True
        assert history_body["count"] == len(history_body["samples"])
        assert history_body["count"] >= 1
        assert "utilization" in history_body["samples"][0]

        # 대기열의 실제 job id 로 ETA 조회.
        if queue["pending"]:
            job_id = queue["pending"][0]["job_id"]
            eta = client.get(f"/api/v1/jobs/eta/{job_id}")
            assert eta.status_code == 200
            body = eta.json()
            assert body["found"] is True
            assert body["confidence"] in ("medium", "low", "unknown")

        # 없는 job 은 404 (라우트가 catch-all 보다 먼저 잡혀야 함).
        assert client.get("/api/v1/jobs/eta/999999999").status_code == 404


def test_tutorial_api():
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/tutorial").json()
        assert body["ok"] is True
        assert body["mode"] == "practice"          # 항상 mock 위에서 돈다
        assert [s["id"] for s in body["steps"]] == ["ssh", "quota", "status", "data", "submit", "result"]
        for step in body["steps"]:
            assert step["title"] and step["body"]
            assert isinstance(step["commands"], list)
        assert "sample_status" in body


def test_announcements_api():
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/announcements").json()
        assert "ok" in body and "announcements" in body   # 실패해도 200 + 구조 유지
        if body["ok"] and body["announcements"]:
            first = body["announcements"][0]
            for key in ("ts", "posted_at", "author", "text", "summary", "is_bot", "reply_count", "reactions"):
                assert key in first


def test_recommendation_and_preview_reuse_core():
    app = create_app()
    with TestClient(app) as client:
        recommendation = client.post(
            "/api/v1/recommendations",
            json={"gpus": 1, "hours": 2, "high_perf": False},
        )
        assert recommendation.status_code == 200
        assert recommendation.json()["best"]["node"].startswith("ariel-")

        preview = client.post(
            "/api/v1/jobs/preview",
            json={
                "name": "probe",
                "command": "python train.py",
                "gpus": 1,
                "paths": ["/data/datasets/ImageNet"],
            },
        ).json()
        assert preview["ok"] is False
        assert any(item["code"] == "BLOCKED_PATH" for item in preview["lint"]["problems"])


def test_data_nas_requires_gpu_local_copy(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "train.py").write_text("print('ok')\n", encoding="utf-8")
    app = create_app()
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/jobs/validate",
            json=_job_payload(code, copy_dataset_to_local=False),
        )
        assert result.status_code == 200
        body = result.json()
        assert body["ok"] is False
        codes = {problem["code"] for problem in body["problems"]}
        assert "NAS_LOCAL_COPY_REQUIRED" in codes
        assert "BLOCKED_PATH" in codes


def test_dataset_must_be_compressed_archive(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "train.py").write_text("print('ok')\n", encoding="utf-8")
    app = create_app()
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/jobs/validate",
            json=_job_payload(code, dataset_path="/data/datasets/ImageNet"),
        ).json()
        assert result["ok"] is False
        assert any(item["code"] == "DATASET_ARCHIVE_REQUIRED" for item in result["problems"])


def test_full_mock_prepare_submit_monitor_logs_and_cancel(tmp_path):
    code = tmp_path / "project"
    code.mkdir()
    (code / "train.py").write_text(
        "import os\nprint(os.environ['SERAPH_DATASET_PATH'])\n",
        encoding="utf-8",
    )
    local_path = str(code.resolve())
    app = create_app()
    with TestClient(app) as client:
        validated = client.post("/api/v1/jobs/validate", json=_job_payload(code))
        assert validated.status_code == 200
        assert validated.json()["ok"] is True

        prepared = client.post("/api/v1/jobs/prepare", json=_job_payload(code))
        assert prepared.status_code == 200, prepared.text
        prepared_body = prepared.json()
        local_id = prepared_body["job"]["local_job_id"]
        assert prepared_body["job"]["status"] == "STAGED"
        assert local_path not in prepared.text
        assert "SERAPH_DATASET_PATH" in prepared_body["script"]
        assert "/data/mockuser/.seraph-gui/jobs/" in prepared_body["job"]["remote_dir"]
        assert "/local_datasets/mockuser/" in prepared_body["script"]
        assert "#SBATCH --cpus-per-gpu=8" in prepared_body["script"]
        assert "#SBATCH --mem-per-gpu=32G" in prepared_body["script"]
        assert "#SBATCH --nodelist=" in prepared_body["script"]
        assert prepared_body["test_only"]["ok"] is True

        blocked_submit = client.post(
            f"/api/v1/jobs/{local_id}/submit",
            json={"request_id": "test-request-before-preflight", "confirmed": True},
        )
        assert blocked_submit.status_code == 409
        assert blocked_submit.json()["error"]["code"] == "SRUN_PREFLIGHT_REQUIRED"

        preflight = client.post(f"/api/v1/jobs/{local_id}/preflight")
        assert preflight.status_code == 200, preflight.text
        assert preflight.json()["preflight"]["ok"] is True
        assert "srun preflight OK" in preflight.json()["preflight"]["output"]

        submitted = client.post(
            f"/api/v1/jobs/{local_id}/submit",
            json={"request_id": "test-request-0001", "confirmed": True},
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["job"]["slurm_job_id"].isdigit()
        assert submitted.json()["idempotent"] is False

        repeated = client.post(
            f"/api/v1/jobs/{local_id}/submit",
            json={"request_id": "test-request-0001", "confirmed": True},
        )
        assert repeated.status_code == 200
        assert repeated.json()["idempotent"] is True

        duplicate = client.post(
            f"/api/v1/jobs/{local_id}/submit",
            json={"request_id": "test-request-0002", "confirmed": True},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "JOB_ALREADY_SUBMITTED"

        detail = client.get(f"/api/v1/jobs/{local_id}")
        assert detail.status_code == 200
        assert detail.json()["job"]["status"] in {"PENDING", "RUNNING"}

        logs = client.get(f"/api/v1/jobs/{local_id}/logs")
        assert logs.status_code == 200
        assert "submitted" in logs.json()["stdout"]

        cancelled = client.post(f"/api/v1/jobs/{local_id}/cancel")
        assert cancelled.status_code == 200
        after_cancel = client.get(f"/api/v1/jobs/{local_id}").json()
        assert after_cancel["job"]["status"] == "CANCELLED"

        listed = client.get("/api/v1/jobs").json()
        assert any(job["local_job_id"] == local_id for job in listed["jobs"])


def test_archive_traversal_is_blocked(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("../train.py")
        payload = b"print('unsafe')\n"
        info.size = len(payload)
        handle.addfile(info, io.BytesIO(payload))

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/api/v1/jobs/validate", json=_job_payload(archive))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSAFE_ARCHIVE"


def test_srun_preflight_failure_returns_diagnostic_output(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "train.py").write_text("print('ok')\n", encoding="utf-8")
    app = create_app()
    with TestClient(app) as client:
        prepared = client.post("/api/v1/jobs/prepare", json=_job_payload(code)).json()
        local_id = prepared["job"]["local_job_id"]

        def fail_preflight(*args, **kwargs):
            raise RuntimeError("debug partition unavailable")

        app.state.manager.remote.run_preflight = fail_preflight
        response = client.post(f"/api/v1/jobs/{local_id}/preflight")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["preflight"]["ok"] is False
        assert "debug partition unavailable" in body["preflight"]["output"]


def test_disconnected_app_returns_standard_error():
    app = create_app(auto_connect=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["seraph_reachable"] is False
        response = client.get("/api/v1/me")
        assert response.status_code == 503
        assert response.json() == {
            "ok": False,
            "error": {
                "code": "SERAPH_UNREACHABLE",
                "message": "SERAPH에 연결되어 있지 않습니다.",
                "retryable": True,
            },
        }


def test_ssh_connection_requires_actual_username(tmp_path):
    path = tmp_path / "ssh.yaml"
    path.write_text(
        "connection:\n  mode: ssh\n  host: ariel.khu.ac.kr\n  port: 30080\n",
        encoding="utf-8",
    )
    app = create_app(config_module.load(path), auto_connect=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/session/connect",
            json={"username": None, "host": "ariel.khu.ac.kr", "port": 30080, "password": None},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "SSH_USERNAME_REQUIRED"


def test_connect_request_preserves_password_exactly():
    body = ConnectRequest(
        username="  actual-user  ",
        host="  ariel.khu.ac.kr  ",
        port=30080,
        password=" secret ",
    )
    assert body.username == "actual-user"
    assert body.host == "ariel.khu.ac.kr"
    assert body.password.get_secret_value() == " secret "


def test_ssh_startup_waits_for_user_and_auth_failure_is_401(tmp_path, monkeypatch):
    path = tmp_path / "ssh.yaml"
    path.write_text(
        "connection:\n  mode: ssh\n  host: ariel.khu.ac.kr\n  port: 30080\n",
        encoding="utf-8",
    )
    calls = []

    def reject(*args, **kwargs):
        calls.append((args, kwargs))
        raise AuthError("wrong password")

    monkeypatch.setattr(dependencies.connection_module, "SSHConnection", reject)
    app = create_app(config_module.load(path))
    with TestClient(app) as client:
        assert calls == []
        response = client.post(
            "/api/v1/session/connect",
            json={
                "username": "actual-user",
                "host": "ariel.khu.ac.kr",
                "port": 30080,
                "password": "wrong",
            },
        )
    assert len(calls) == 1
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SSH_AUTH_FAILED"


def test_snapshot_cache_deduplicates_concurrent_refreshes():
    class Connection:
        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            return object()

    connection = Connection()
    cache = SnapshotCache(lambda: connection, ttl_seconds=7)

    async def run():
        await asyncio.gather(*(cache.get() for _ in range(20)))

    asyncio.run(run())
    assert connection.calls == 1


def test_request_validation_uses_common_error_shape():
    app = create_app()
    with TestClient(app) as client:
        # gpus=0 은 이제 CPU 전용으로 유효하다. 무효값(한도 초과)으로 에러 모양을 검사한다.
        response = client.post(
            "/api/v1/recommendations",
            json={"gpus": 99, "hours": 2, "high_perf": False},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


# --- 웹에서 NAS 를 다루는 기능 (터미널 없이 첫 job 을 낼 수 있어야 한다) ---------
# 예전에는 데이터 경로를 맨 입력창에 직접 타이핑해야 했고, 폼 기본값조차 서버에
# 없는 경로였다. 목록·업로드·conda 탐지가 없으면 "터미널 없이"가 거짓말이 된다.

def test_remote_ls_lists_entries_and_marks_archives(tmp_path):
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/remote/ls").json()
        assert body["ok"] is True
        assert body["path"] == body["data_root"]
        assert "parent" in body and "entries" in body
        for entry in body["entries"]:
            assert {"name", "path", "is_dir", "is_archive"} <= set(entry)
            if entry["is_dir"]:
                assert entry["size"] is None


def test_remote_ls_hides_internal_job_folder():
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/remote/ls").json()
        # .seraph-gui 같은 내부 폴더가 사용자에게 보이면 안 된다.
        assert all(not e["name"].startswith(".") for e in body["entries"])


def test_dataset_upload_rejects_non_archive(tmp_path):
    loose = tmp_path / "notes.txt"
    loose.write_text("not an archive", encoding="utf-8")
    app = create_app()
    with TestClient(app) as client:
        r = client.post(f"/api/v1/remote/datasets/upload?local_path={loose}")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "DATASET_ARCHIVE_REQUIRED"


def test_dataset_upload_lands_in_user_datasets_folder(tmp_path):
    import tarfile

    member = tmp_path / "a.txt"
    member.write_text("hello", encoding="utf-8")
    archive = tmp_path / "smoke.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(member, arcname="a.txt")

    app = create_app()
    with TestClient(app) as client:
        body = client.post(f"/api/v1/remote/datasets/upload?local_path={archive}").json()
        assert body["ok"] is True and body["selected"] is True
        target = body["dataset"]["path"]
        assert target.endswith("/datasets/smoke.tar.gz")
        # 업로드는 사용자 자기 폴더 안으로만 간다(샌드박스가 열리면 안 된다).
        listed = client.get(f"/api/v1/remote/ls?path={target.rsplit('/', 1)[0]}").json()
        names = {e["name"]: e for e in listed["entries"]}
        assert "smoke.tar.gz" in names and names["smoke.tar.gz"]["is_archive"] is True


def test_conda_discovery_reports_installs_and_envs():
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/remote/conda").json()
        assert body["ok"] is True
        installs = body["installs"]
        assert installs, "conda 설치를 하나도 못 찾으면 환경을 고를 수 없다"
        for inst in installs:
            assert {"root", "conda_sh", "is_personal", "envs"} <= set(inst)
            assert inst["conda_sh"].endswith("/etc/profile.d/conda.sh")


# --- 작업 기록 삭제 ------------------------------------------------------------
# 끝난 작업이 계속 쌓이면 목록이 못 쓰게 된다. 다만 도는 작업을 지우면 Slurm 에는
# job 이 남고 우리만 추적을 잃으므로, 그건 취소가 먼저다.

def test_delete_removes_job_from_list(tmp_path):
    code = tmp_path / "project"
    code.mkdir()
    (code / "train.py").write_text("print('ok')\n", encoding="utf-8")
    app = create_app()
    with TestClient(app) as client:
        prepared = client.post("/api/v1/jobs/prepare", json=_job_payload(code))
        assert prepared.status_code == 200, prepared.text
        local_id = prepared.json()["job"]["local_job_id"]
        assert any(j["local_job_id"] == local_id for j in client.get("/api/v1/jobs").json()["jobs"])

        removed = client.request("DELETE", f"/api/v1/jobs/{local_id}")
        assert removed.status_code == 200, removed.text
        assert removed.json()["deleted"] == local_id

        remaining = client.get("/api/v1/jobs").json()["jobs"]
        assert all(j["local_job_id"] != local_id for j in remaining)
        # 지운 작업은 상세 조회도 404 여야 한다.
        assert client.get(f"/api/v1/jobs/{local_id}").status_code == 404


def test_delete_refuses_while_job_is_active(tmp_path):
    code = tmp_path / "project"
    code.mkdir()
    (code / "train.py").write_text("print('ok')\n", encoding="utf-8")
    app = create_app()
    with TestClient(app) as client:
        prepared = client.post("/api/v1/jobs/prepare", json=_job_payload(code)).json()
        local_id = prepared["job"]["local_job_id"]
        # 도구는 srun 사전 점검을 통과해야만 제출을 받는다(튜토리얼 절차).
        assert client.post(f"/api/v1/jobs/{local_id}/preflight").status_code == 200
        submitted = client.post(
            f"/api/v1/jobs/{local_id}/submit",
            json={"request_id": "req-delete-guard", "confirmed": True},
        )
        assert submitted.status_code == 200, submitted.text

        # 방금 제출한 job 은 아직 돈다 -> 삭제 거부
        blocked = client.request("DELETE", f"/api/v1/jobs/{local_id}")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "JOB_STILL_ACTIVE"
        # 목록에는 그대로 남아 있어야 한다
        assert any(j["local_job_id"] == local_id for j in client.get("/api/v1/jobs").json()["jobs"])


# --- 웹에서 파이썬 환경 만들기 -------------------------------------------------
# 공용 설치(/data/opt/anaconda3)는 읽기 전용이라 pip 하나 넣을 수 없다. 원하는
# torch 버전이 없는 순간 학생은 이 도구를 닫고 터미널을 열었다 — 마지막 구멍이다.

def _wait_for_build(client, build_id, tries=30):
    for _ in range(tries):
        build = client.get(f"/api/v1/envs/builds/{build_id}").json()["build"]
        if build["state"] != "running":
            return build
        time.sleep(0.3)
    raise AssertionError("빌드가 끝나지 않았습니다")


def test_env_tools_reports_readiness_with_evidence():
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/envs/tools").json()
        assert body["can_create"] is True
        assert body["blockers"] == []
        # "가능하다"만 말하고 근거를 감추면, 실패했을 때 아무도 이유를 모른다.
        assert body["conda_version"] and body["envs_root"].endswith("/envs")
        assert body["python_versions"] and body["presets"]
        assert [n["url"] for n in body["network"]] == list(PACKAGE_HOSTS)
        assert all(n["ok"] for n in body["network"])


def test_env_create_build_and_delete_round_trip():
    app = create_app()
    with TestClient(app) as client:
        created = client.post("/api/v1/envs", json={
            "name": "torch25", "mode": "scratch", "python": "3.11",
            "conda_packages": ["pytorch", "pytorch-cuda=12.1"],
            "channels": ["pytorch", "nvidia"], "pip_packages": ["wandb"],
        })
        assert created.status_code == 200
        build = created.json()["build"]
        assert build["prefix"].endswith("/envs/torch25")

        done = _wait_for_build(client, build["build_id"])
        assert done["state"] == "succeeded"

        names = [e["name"] for e in client.get("/api/v1/envs").json()["envs"]]
        assert "torch25" in names
        assert client.delete("/api/v1/envs/torch25").status_code == 200
        assert "torch25" not in [e["name"] for e in client.get("/api/v1/envs").json()["envs"]]


def test_env_shared_installs_are_listed_but_not_removable():
    app = create_app()
    with TestClient(app) as client:
        envs = client.get("/api/v1/envs").json()["envs"]
        shared = [e for e in envs if e["source"] != "personal"]
        assert shared, "공용 환경이 안 보이면 복제할 대상을 고를 수 없다"
        assert all(e["removable"] is False for e in shared)
        # 공용 설치는 읽기 전용이다. 지워지는 척하면 안 된다.
        refused = client.delete(f"/api/v1/envs/{shared[0]['name']}")
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "ENV_NOT_FOUND"


def test_env_rejects_shell_flags_disguised_as_packages():
    app = create_app()
    with TestClient(app) as client:
        # 따옴표로 감싸도 '--force-reinstall' 이 그대로 들어가면 conda 가 옵션으로 읽는다.
        for payload in (
            {"name": "bad1", "conda_packages": ["--force-reinstall"]},
            {"name": "bad2", "pip_packages": ["-rrequirements.txt"]},
            {"name": "bad3", "channels": ["--override-channels"]},
            {"name": "../escape"},
            {"name": "bad4", "python": "3.11; rm -rf /"},
        ):
            assert client.post("/api/v1/envs", json=payload).status_code == 422


def test_env_duplicate_name_is_refused_before_a_long_build():
    app = create_app()
    with TestClient(app) as client:
        first = client.post("/api/v1/envs", json={"name": "dup", "mode": "scratch"})
        _wait_for_build(client, first.json()["build"]["build_id"])
        again = client.post("/api/v1/envs", json={"name": "dup", "mode": "scratch"})
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "ENV_ALREADY_EXISTS"


def test_clone_mode_requires_an_existing_source():
    app = create_app()
    with TestClient(app) as client:
        missing = client.post("/api/v1/envs", json={"name": "c1", "mode": "clone", "source": "nope"})
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "ENV_SOURCE_NOT_FOUND"
        assert client.post("/api/v1/envs", json={"name": "c2", "mode": "clone"}).status_code == 422


def test_job_script_activates_a_web_made_env_by_prefix(tmp_path):
    """웹에서 만든 환경은 공용 설치의 envs 폴더에 없다.

    이름만으로 `conda activate` 하면 conda 는 자기 envs_dirs 만 뒤지므로 job 이
    "환경을 찾을 수 없다"로 죽는다. 절대 경로를 켜야 한다.
    """
    app = create_app()
    with TestClient(app) as client:
        made = client.post("/api/v1/envs", json={"name": "mine25", "mode": "scratch"})
        _wait_for_build(client, made.json()["build"]["build_id"])

        code = tmp_path / "code"
        code.mkdir()
        (code / "train.py").write_text("print('hi')", encoding="utf-8")
        prepared = client.post("/api/v1/jobs/prepare", json=_job_payload(code, conda_env="mine25"))
        assert prepared.status_code == 200
        script = prepared.json()["script"]
        assert "conda activate /data/mockuser/envs/mine25" in script


# --- 알림 (작업 완료 · 환경 준비 완료) -----------------------------------------
# 몇 시간짜리 학습을 화면 보면서 기다리게 하면 `watch squeue` 와 다를 게 없다.
# 감시를 백엔드가 하는 이유는, 화면 폴링이 탭을 가리는 순간 멈추기 때문이다.

def _drain_events(client, session=None, since=0, tries=40):
    for _ in range(tries):
        query = f"/api/v1/events?since={since}&can_notify=true"
        if session:
            query += f"&session={session}"
        body = client.get(query).json()
        if body["events"]:
            return body
        time.sleep(0.2)
    return body


def test_finished_env_build_becomes_an_event():
    app = create_app()
    with TestClient(app) as client:
        app.state.watcher.interval = 0.2
        base = client.get("/api/v1/events?can_notify=true").json()
        assert base["latest_id"] == 0 and base["watching"] == {"jobs": 0, "builds": 0}

        client.post("/api/v1/envs", json={"name": "alertenv", "mode": "scratch"})
        assert client.get("/api/v1/events?can_notify=true").json()["watching"]["builds"] == 1

        body = _drain_events(client, base["session"])
        assert [e["kind"] for e in body["events"]] == ["env"]
        event = body["events"][0]
        assert event["ok"] is True and "alertenv" in event["title"]
        # 끝난 것은 감시 목록에서 빠져야 한다. 안 그러면 매 주기마다 서버에 묻는다.
        assert body["watching"]["builds"] == 0


def test_events_are_not_replayed_to_a_caller_that_already_saw_them():
    app = create_app()
    with TestClient(app) as client:
        app.state.watcher.interval = 0.2
        session = client.get("/api/v1/events?can_notify=true").json()["session"]
        client.post("/api/v1/envs", json={"name": "onceonly", "mode": "scratch"})
        body = _drain_events(client, session)
        latest = body["latest_id"]
        again = client.get(f"/api/v1/events?since={latest}&session={session}&can_notify=true").json()
        assert again["events"] == []


def test_restarted_backend_tells_the_browser_to_rebaseline():
    """백엔드를 다시 띄우면 이벤트 번호가 1부터 시작한다.

    화면이 들고 있던 '42번까지 봤다'를 그대로 믿으면 새 이벤트를 영영 못 알아본다.
    세션이 다르면 다시 맞추라고 알려줘야 한다.
    """
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/events?since=99&session=ffffffffffff&can_notify=true").json()
        assert body["reset"] is True and body["events"] == []
        assert body["session"] != "ffffffffffff"


def test_os_notification_only_when_no_browser_can_show_it(monkeypatch):
    """같은 일로 두 번 울리면 안 되고, 아무도 못 받는 상태는 더 나쁘다.

    브라우저가 알림을 띄울 수 있으면 화면이 맡고, 그렇지 않으면(탭을 닫았거나
    알림 권한이 없거나) 백엔드가 OS 알림을 띄운다.
    """
    from backend import watcher as watcher_module

    sent = []
    monkeypatch.setattr(watcher_module.os_notify, "send",
                        lambda title, body: sent.append(title) or True)

    app = create_app()
    with TestClient(app) as client:
        watcher = app.state.watcher
        watcher.interval = 0.2

        # 1) 알림을 띄울 수 있는 브라우저가 살아 있다 -> OS 알림은 건너뛴다.
        client.get("/api/v1/events?can_notify=true")
        client.post("/api/v1/envs", json={"name": "hasbrowser", "mode": "scratch"})
        _drain_events(client, since=0)
        assert sent == []

        # 2) 권한 없는 브라우저는 '살아 있음'으로 치지 않는다 -> 백엔드가 띄운다.
        watcher._last_client_seen = 0.0
        client.post("/api/v1/envs", json={"name": "nopermission", "mode": "scratch"})
        for _ in range(40):
            if sent:
                break
            client.get("/api/v1/events?can_notify=false")
            time.sleep(0.2)
        assert any("nopermission" in title for title in sent)


def test_watcher_recovers_active_jobs_after_a_restart():
    """백엔드 재시작은 흔하다. 재시작했다고 이미 낸 작업의 완료를 놓치면 안 된다."""
    app = create_app()
    with TestClient(app) as client:
        watcher = app.state.watcher
        watcher._jobs_watched.clear()
        watcher.resync()
        listed = client.get("/api/v1/jobs").json()["jobs"]
        active = [j for j in listed if j["status"] in
                  {"SUBMITTING", "SUBMITTED", "PENDING", "RUNNING", "COMPLETING", "CANCEL_REQUESTED"}]
        assert len(watcher._jobs_watched) == len(active)


def test_job_never_vanishes_while_its_status_is_being_written(tmp_path):
    """상태를 쓰는 동안 다른 요청이 같은 작업을 읽어도 404 가 나면 안 된다.

    알림 감시 스레드를 붙이면서 드러났지만 원래 있던 결함이다. 상태 갱신은
    job.json 을 다시 쓰는데, 그 사이에 읽은 쪽에는 작업이 사라진 것으로 보였다.
    브라우저 탭 두 개만 열어도 날 수 있었다.
    """
    import threading

    code = tmp_path / "race"
    code.mkdir()
    (code / "train.py").write_text("print('ok')\n", encoding="utf-8")

    app = create_app()
    with TestClient(app) as client:
        prepared = client.post("/api/v1/jobs/prepare", json=_job_payload(code))
        local_id = prepared.json()["job"]["local_job_id"]
        client.post(f"/api/v1/jobs/{local_id}/preflight")
        client.post(f"/api/v1/jobs/{local_id}/submit",
                    json={"request_id": "race-0001", "confirmed": True})

        codes, stop = [], threading.Event()

        def hammer():
            while not stop.is_set():
                # 예외도 실패로 센다. 서버가 500 으로 죽으면 TestClient 는 상태
                # 코드를 주는 대신 예외를 다시 던지는데, 그걸 안 잡으면 스레드만
                # 조용히 죽고 테스트는 통과한 것처럼 보인다.
                for path in (f"/api/v1/jobs/{local_id}", "/api/v1/jobs"):
                    try:
                        codes.append(client.get(path).status_code)
                    except Exception as exc:       # noqa: BLE001
                        codes.append(type(exc).__name__)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(1.5)          # 그동안 mock 상태가 PENDING -> RUNNING -> COMPLETED 로 바뀐다
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

        assert codes, "경합 구간을 한 번도 못 밟았다면 이 테스트는 아무것도 지키지 못한다"
        assert set(codes) == {200}, f"작업이 잠깐 사라졌다: {sorted(set(codes))}"


def test_env_list_surfaces_builds_the_browser_did_not_start():
    """'다시 열면 진행 상황이 이어집니다'가 참이 되려면 서버가 알려줘야 한다.

    화면은 자기가 시작한 빌드 번호를 브라우저에 저장한다. 다른 브라우저로 열거나
    저장소를 지우면 그 번호가 없어서, 20분짜리 빌드가 화면에서 통째로 사라진다.
    """
    app = create_app()
    with TestClient(app) as client:
        app.state.watcher.interval = 0.2
        assert client.get("/api/v1/envs").json()["running_builds"] == []

        created = client.post("/api/v1/envs", json={"name": "orphan", "mode": "scratch"}).json()
        running = client.get("/api/v1/envs").json()["running_builds"]
        assert [b["build_id"] for b in running] == [created["build"]["build_id"]]
        assert running[0]["name"] == "orphan"

        _drain_events(client, since=0)
        assert client.get("/api/v1/envs").json()["running_builds"] == []


# --- 파일 탐색기 ---------------------------------------------------------------
# 예전 것은 "탐색기"가 아니라 한 폴더만 보여주는 파일 고르기 창이었다. 폴더 구조가
# 어디에도 안 보여서 내 결과물이 서버 어디에 쌓이는지 알 수 없었다.

def _prepared_job_dir(client, tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "train.py").write_text("print('ok')\n", encoding="utf-8")
    client.post("/api/v1/jobs/prepare", json=_job_payload(code))
    jobs_root = "/data/mockuser/.seraph-gui/jobs"
    listed = client.get(f"/api/v1/remote/ls?path={jobs_root}&show_hidden=true").json()
    return listed["entries"][0]["path"]


def test_listing_carries_what_a_file_browser_needs():
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/api/v1/remote/ls").json()
        # 트리 루트를 화면이 /data/<사용자> 로 조립하지 않도록 서버가 내려준다.
        assert body["data_root"] and body["datasets_root"] and body["home"]
        for entry in body["entries"]:
            assert {"name", "path", "is_dir", "is_link", "hidden", "size", "mtime",
                    "is_archive"} <= set(entry)


def test_hidden_entries_appear_only_when_asked(tmp_path):
    app = create_app()
    with TestClient(app) as client:
        _prepared_job_dir(client, tmp_path)
        plain = client.get("/api/v1/remote/ls").json()["entries"]
        assert all(not e["name"].startswith(".") for e in plain)

        shown = client.get("/api/v1/remote/ls?show_hidden=true").json()["entries"]
        hidden = [e for e in shown if e["hidden"]]
        assert any(e["name"] == ".seraph-gui" for e in hidden)


def test_tree_asks_for_folders_only(tmp_path):
    """트리에 파일까지 실어 보내면 데이터셋 폴더 하나가 수만 개 항목을 끌고 온다."""
    app = create_app()
    with TestClient(app) as client:
        job_dir = _prepared_job_dir(client, tmp_path)
        everything = client.get(f"/api/v1/remote/ls?path={job_dir}").json()["entries"]
        assert any(not e["is_dir"] for e in everything), "이 폴더엔 파일이 있어야 시험이 된다"

        folders = client.get(f"/api/v1/remote/ls?path={job_dir}&dirs_only=true").json()["entries"]
        assert all(e["is_dir"] for e in folders)
        assert len(folders) < len(everything)


def test_file_preview_returns_text_and_marks_truncation(tmp_path):
    app = create_app()
    with TestClient(app) as client:
        job_dir = _prepared_job_dir(client, tmp_path)
        target = f"{job_dir}/job.json"

        full = client.get(f"/api/v1/remote/file?path={target}").json()
        assert full["binary"] is False and full["truncated"] is False
        assert full["text"].startswith("{")

        # 상한을 넘으면 잘렸다고 말해야 한다. 조용히 자르면 사용자는 그게 전부인 줄 안다.
        clipped = client.get(f"/api/v1/remote/file?path={target}&max_bytes=1024").json()
        assert clipped["truncated"] is True and len(clipped["text"]) == 1024
        assert clipped["size"] == full["size"]


def test_binary_file_is_named_not_rendered(tmp_path):
    """이진 파일을 텍스트로 우겨넣으면 화면이 깨진 문자로 뒤덮인다."""
    app = create_app()
    with TestClient(app) as client:
        job_dir = _prepared_job_dir(client, tmp_path)
        body = client.get(f"/api/v1/remote/file?path={job_dir}/code.tar.gz").json()
        assert body["binary"] is True and body["text"] is None
        assert body["size"] > 0


def test_preview_refuses_a_directory_and_a_missing_path():
    app = create_app()
    with TestClient(app) as client:
        is_dir = client.get("/api/v1/remote/file?path=/data/mockuser")
        assert is_dir.status_code == 400
        assert is_dir.json()["error"]["code"] == "REMOTE_PATH_IS_DIR"
        assert client.get("/api/v1/remote/file?path=/data/mockuser/nope.txt").status_code == 404


def test_preview_size_cap_cannot_be_raised_by_the_caller():
    """상한을 요청으로 올릴 수 있으면 체크포인트 하나로 백엔드를 메모리째 넘길 수 있다."""
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/v1/remote/file?path=/x&max_bytes=99999999").status_code == 422
