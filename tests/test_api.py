import asyncio
import io
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

from backend import dependencies
from backend.cache import SnapshotCache
from backend.main import create_app
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
        response = client.post(
            "/api/v1/recommendations",
            json={"gpus": 0, "hours": 2, "high_perf": False},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"
