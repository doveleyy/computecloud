import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def create_sleep_job(
    client: TestClient, seconds: int = 1, name: str | None = None
) -> dict:
    response = client.post(
        "/jobs",
        json={"name": name, "type": "sleep", "parameters": {"seconds": seconds}},
    )
    assert response.status_code == 201
    return response.json()


def claim_job(client: TestClient, worker_id: str = "mac-one") -> dict | None:
    response = client.post(
        "/workers/claim",
        json={"worker_id": worker_id, "supported_types": ["sleep"]},
    )
    assert response.status_code == 200
    return response.json()


def enable_worker(
    client: TestClient,
    worker_id: str = "mac-one",
    supported_types: list[str] | None = None,
) -> None:
    """Register a worker and turn its scheduling on.

    Workers register with scheduling disabled, so any test that expects a
    claim to succeed has to enable the worker first.
    """
    registration = client.post(
        "/workers/heartbeat",
        json={
            "worker_id": worker_id,
            "supported_types": supported_types or ["sleep"],
        },
    )
    assert registration.status_code == 204
    update = client.patch(f"/workers/{worker_id}", json={"enabled": True})
    assert update.status_code == 200


def test_liveness_and_readiness(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.json() == {"status": "healthy"}
    assert ready.json() == {"status": "ready"}


def test_readiness_reports_database_failure(tmp_path: Path) -> None:
    class UnavailableService:
        def ping(self) -> None:
            raise sqlite3.OperationalError("database unavailable")

    app = create_app(tmp_path / "jobs.db")
    with TestClient(app) as client:
        app.state.job_service = UnavailableService()
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database is unavailable"}


def test_create_and_get_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client, seconds=2, name="  Nightly check  ")
        response = client.get(f"/jobs/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created
    assert created["name"] == "Nightly check"


def test_job_name_is_optional_validated_and_persists(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.db"
    with TestClient(create_app(database_path)) as client:
        unnamed = create_sleep_job(client)
        named = create_sleep_job(client, name="Iris summary / run 02")
        empty = client.post(
            "/jobs",
            json={"name": "   ", "type": "sleep", "parameters": {"seconds": 1}},
        )
        control = client.post(
            "/jobs",
            json={
                "name": "bad\nname",
                "type": "sleep",
                "parameters": {"seconds": 1},
            },
        )
    with TestClient(create_app(database_path)) as restarted:
        preserved = restarted.get(f"/jobs/{named['id']}")

    assert unnamed["name"] is None
    assert named["name"] == "Iris summary / run 02"
    assert empty.status_code == 422
    assert control.status_code == 422
    assert preserved.json()["name"] == "Iris summary / run 02"


def test_completed_job_survives_application_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.db"
    with TestClient(create_app(database_path)) as first_client:
        created = create_sleep_job(first_client)
        enable_worker(first_client)
        claimed = claim_job(first_client)
        assert claimed is not None
        completed = first_client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        assert completed.status_code == 200

    with TestClient(create_app(database_path)) as restarted_client:
        response = restarted_client.get(f"/jobs/{created['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["result"] == {"slept_seconds": 1}


def test_validation_and_missing_job_responses(tmp_path: Path) -> None:
    missing_job_id = uuid4()
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        invalid = client.post(
            "/jobs",
            json={"type": "sleep", "parameters": {"seconds": 31}},
        )
        missing = client.get(f"/jobs/{missing_job_id}")
        malformed = client.get("/jobs/not-a-uuid")

    assert invalid.status_code == 422
    assert missing.status_code == 404
    assert malformed.status_code == 422


def test_idempotency_key_returns_same_job_and_rejects_different_request(
    tmp_path: Path,
) -> None:
    headers = {"Idempotency-Key": "upload-batch-42"}
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first = client.post(
            "/jobs",
            headers=headers,
            json={"type": "sleep", "parameters": {"seconds": 1}},
        )
        repeated = client.post(
            "/jobs",
            headers=headers,
            json={"type": "sleep", "parameters": {"seconds": 1}},
        )
        conflicting = client.post(
            "/jobs",
            headers=headers,
            json={"type": "sleep", "parameters": {"seconds": 2}},
        )
        renamed = client.post(
            "/jobs",
            headers=headers,
            json={
                "name": "different run",
                "type": "sleep",
                "parameters": {"seconds": 1},
            },
        )
        jobs = client.get("/jobs")

    assert first.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["id"] == first.json()["id"]
    assert conflicting.status_code == 409
    assert renamed.status_code == 409
    assert len(jobs.json()) == 1


def test_token_protects_client_and_worker_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        missing_token = client.get("/jobs")
        wrong_token = client.get("/jobs", headers={"X-API-Token": "wrong-secret"})
        accepted = client.get("/jobs", headers={"X-API-Token": "test-secret"})
        worker_without_token = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )

    assert missing_token.status_code == 401
    assert wrong_token.status_code == 401
    assert accepted.status_code == 200
    assert worker_without_token.status_code == 401


def test_health_does_not_require_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_token_can_be_loaded_from_file(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "api-token"
    token_file.write_text("file-secret\n")
    monkeypatch.delenv("HOME_PLATFORM_API_TOKEN", raising=False)
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(token_file))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        response = client.get("/jobs", headers={"X-API-Token": "file-secret"})
    assert response.status_code == 200


def test_explicit_empty_token_file_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token_file = tmp_path / "api-token"
    token_file.write_text("")
    monkeypatch.delenv("HOME_PLATFORM_API_TOKEN", raising=False)
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(token_file))

    with pytest.raises(RuntimeError, match="required but empty"):
        create_app(tmp_path / "jobs.db")


def test_worker_claims_and_completes_oldest_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first = create_sleep_job(client, seconds=2)
        create_sleep_job(client, seconds=3)
        enable_worker(client)
        running = claim_job(client)

        assert running is not None
        assert running["id"] == first["id"]
        assert running["status"] == "RUNNING"
        assert running["worker_id"] == "mac-one"
        assert running["started_at"] is not None

        completion = client.post(
            f"/jobs/{first['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": running["lease_token"],
                "result": {"slept_seconds": 2},
            },
        )

    assert completion.status_code == 200
    assert completion.json()["status"] == "COMPLETED"
    assert completion.json()["result"] == {"slept_seconds": 2}


def test_empty_queue_returns_null_claim(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        enable_worker(client)
        response = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
    assert response.status_code == 200
    assert response.json() is None


def test_dataset_script_contract_claim_and_completion(tmp_path: Path) -> None:
    sha256 = "a" * 64
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created_response = client.post(
            "/jobs",
            json={
                "type": "dataset_script",
                "parameters": {
                    "script": "csv_summary",
                    "dataset": {
                        "url": "https://datasets.example/input.csv",
                        "sha256": sha256,
                        "size_bytes": 100,
                    },
                    "timeout_seconds": 60,
                },
            },
        )
        created = created_response.json()
        enable_worker(client, "sleep-only")
        enable_worker(client, "windows-primary", ["dataset_script"])
        sleep_only = claim_job(client, "sleep-only")
        claimed_response = client.post(
            "/workers/claim",
            json={
                "worker_id": "windows-primary",
                "supported_types": ["dataset_script"],
            },
        )
        claimed = claimed_response.json()
        completed = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "windows-primary",
                "lease_token": claimed["lease_token"],
                "result": {
                    "script": "csv_summary",
                    "dataset_sha256": sha256,
                    "dataset_bytes": 100,
                    "rows": 2,
                    "columns": ["name", "value"],
                    "artifact_uri": (
                        f"worker://windows-primary/{created['id']}/summary.json"
                    ),
                },
            },
        )

    assert created_response.status_code == 201
    assert sleep_only is None
    assert claimed_response.status_code == 200
    assert claimed["id"] == created["id"]
    assert completed.status_code == 200
    assert completed.json()["result"]["rows"] == 2


def test_python_batch_contract_claim_and_completion(tmp_path: Path) -> None:
    script_id = uuid4()
    dataset_id = uuid4()
    sha256 = "a" * 64
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created_response = client.post(
            "/jobs",
            json={
                "name": "Train tiny model",
                "type": "python_batch",
                "parameters": {
                    "script": {
                        "upload_id": str(script_id),
                        "sha256": sha256,
                        "size_bytes": 100,
                    },
                    "dataset": {
                        "upload_id": str(dataset_id),
                        "sha256": sha256,
                        "size_bytes": 200,
                    },
                    "timeout_seconds": 600,
                    "cpu_limit": 2,
                    "memory_mb": 2048,
                },
            },
        )
        created = created_response.json()
        enable_worker(client, "windows-primary", ["python_batch"])
        claimed_response = client.post(
            "/workers/claim",
            json={
                "worker_id": "windows-primary",
                "supported_types": ["python_batch"],
            },
        )
        claimed = claimed_response.json()
        completed = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "windows-primary",
                "lease_token": claimed["lease_token"],
                "result": {
                    "script_sha256": sha256,
                    "dataset_sha256": sha256,
                    "exit_code": 0,
                    "stdout": "accuracy=0.95\n",
                    "stderr": "",
                    "output_files": ["model.joblib", "metrics.json"],
                    "artifact_uri": f"worker://windows-primary/{created['id']}/",
                },
            },
        )

    assert created_response.status_code == 201
    assert claimed_response.status_code == 200
    assert claimed["id"] == created["id"]
    assert completed.status_code == 200
    assert completed.json()["result"]["output_files"] == [
        "model.joblib",
        "metrics.json",
    ]


def test_dataset_contract_rejects_unsafe_url_and_wrong_result(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        unsafe = client.post(
            "/jobs",
            json={
                "type": "dataset_script",
                "parameters": {
                    "script": "csv_summary",
                    "dataset": {
                        "url": "http://127.0.0.1/private.csv",
                        "sha256": "a" * 64,
                        "size_bytes": 100,
                    },
                },
            },
        )
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        mismatch = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {
                    "script": "csv_summary",
                    "dataset_sha256": "a" * 64,
                    "dataset_bytes": 100,
                    "rows": 2,
                    "columns": [],
                    "artifact_uri": f"worker://mac-one/{created['id']}/summary.json",
                },
            },
        )

    assert unsafe.status_code == 422
    assert mismatch.status_code == 409


def test_disabled_worker_stays_connected_but_cannot_claim(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        registered = client.post(
            "/workers/heartbeat",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        registered_enabled = client.get("/workers").json()[0]["enabled"]
        enabled = client.patch("/workers/mac-one", json={"enabled": True})
        disabled = client.patch("/workers/mac-one", json={"enabled": False})
        created = create_sleep_job(client)
        blocked_claim = claim_job(client)
        heartbeat = client.post(
            "/workers/heartbeat",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        workers = client.get("/workers")
        reenabled = client.patch("/workers/mac-one", json={"enabled": True})
        accepted_claim = claim_job(client)

    assert registered.status_code == 204
    assert registered_enabled is False
    assert enabled.json()["enabled"] is True
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert blocked_claim is None
    assert heartbeat.status_code == 204
    assert workers.json()[0]["enabled"] is False
    assert workers.json()[0]["state"] == "ONLINE"
    assert reenabled.json()["enabled"] is True
    assert accepted_claim is not None
    assert accepted_claim["id"] == created["id"]


def test_disabling_busy_worker_does_not_cancel_its_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        disabled = client.patch("/workers/mac-one", json={"enabled": False})
        heartbeat = client.post(
            "/workers/heartbeat",
            json={
                "worker_id": "mac-one",
                "supported_types": ["sleep"],
                "current_job_id": created["id"],
                "lease_token": claimed["lease_token"],
            },
        )
        completed = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )

    assert disabled.json()["enabled"] is False
    assert disabled.json()["state"] == "BUSY"
    assert heartbeat.status_code == 204
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"


def test_updating_unknown_or_invalid_worker_is_rejected(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        unknown = client.patch("/workers/not-registered", json={"enabled": False})
        invalid_id = client.patch("/workers/not%20valid", json={"enabled": False})
        invalid_body = client.patch("/workers/not-registered", json={"enabled": "no"})

    assert unknown.status_code == 404
    assert invalid_id.status_code == 422
    assert invalid_body.status_code == 422


def test_only_owner_can_finish_running_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client, "mac-owner")
        claimed = claim_job(client, "mac-owner")
        assert claimed is not None
        wrong_worker = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-impostor",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        completed = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-owner",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        duplicate = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-owner",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )

    assert wrong_worker.status_code == 409
    assert completed.status_code == 200
    assert duplicate.status_code == 409


def test_missing_job_cannot_be_finished(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        response = client.post(
            f"/jobs/{uuid4()}/fail",
            json={
                "worker_id": "mac-one",
                "lease_token": str(uuid4()),
                "error": "not found",
            },
        )
    assert response.status_code == 404


def test_worker_can_report_failure(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        response = client.post(
            f"/jobs/{created['id']}/fail",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "error": "test failure",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["error"] == "test failure"


def test_heartbeat_registers_worker_and_renews_lease(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        response = client.post(
            "/workers/heartbeat",
            json={
                "worker_id": "mac-one",
                "supported_types": ["sleep"],
                "current_job_id": created["id"],
                "lease_token": claimed["lease_token"],
                "metrics": {
                    "platform": "TestOS",
                    "logical_cores": 8,
                    "cpu_percent": 25.5,
                    "memory_percent": 50.0,
                    "memory_available": 8000000000,
                    "memory_total": 16000000000,
                    "storage_percent": 75.0,
                    "storage_free": 25000000000,
                    "storage_total": 100000000000,
                    "temperature_c": 51.0,
                    "gpus": [
                        {
                            "name": "Test GPU",
                            "percent": 12.0,
                            "memory_used": 1000000000,
                            "memory_total": 8000000000,
                            "temperature_c": 48.0,
                        }
                    ],
                },
            },
        )
        workers = client.get("/workers")
        renewed = client.get(f"/jobs/{created['id']}")

    assert response.status_code == 204
    assert workers.json()[0]["state"] == "BUSY"
    assert workers.json()[0]["current_job_id"] == created["id"]
    assert workers.json()[0]["metrics"]["cpu_percent"] == 25.5
    assert workers.json()[0]["metrics"]["gpus"][0]["name"] == "Test GPU"
    assert renewed.json()["lease_expires_at"] >= claimed["lease_expires_at"]


def test_old_lease_token_cannot_complete_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        rejected = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": str(uuid4()),
                "result": {"slept_seconds": 1},
            },
        )

    assert rejected.status_code == 409


def test_dashboard_requires_login_and_exposes_operational_data(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        page = client.get("/dashboard")
        jobs_page = client.get("/jobs-ui")
        unauthenticated = client.get("/dashboard/api/system")
        unauthenticated_jobs = client.get("/jobs-ui/api/jobs")
        unauthenticated_artifacts = client.get(f"/jobs-ui/api/jobs/{uuid4()}/artifacts")
        unauthenticated_submit = client.post(
            "/jobs-ui/api/jobs",
            json={
                "name": "Family check",
                "type": "sleep",
                "parameters": {"seconds": 5},
            },
        )
        wrong = client.post("/dashboard/login", json={"token": "wrong"})
        registered = client.post(
            "/workers/heartbeat",
            headers={"X-API-Token": "test-secret"},
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        dashboard_update_without_session = client.patch(
            "/dashboard/api/workers/mac-one", json={"enabled": False}
        )
        api_update_without_token = client.patch(
            "/workers/mac-one", json={"enabled": False}
        )
        accepted = client.post("/dashboard/login", json={"token": "test-secret"})
        uploaded = client.post(
            "/jobs-ui/api/uploads",
            files={"file": ("family.csv", b"name,value\na,1\n", "text/csv")},
        )
        upload_body = uploaded.json()
        script_uploaded = client.post(
            "/jobs-ui/api/script-uploads",
            files={"file": ("train.py", b"print('ok')\n", "text/x-python")},
        )
        script_body = script_uploaded.json()
        unauthenticated_download = client.get(
            f"/datasets/uploads/{upload_body['upload_id']}"
        )
        downloaded = client.get(
            f"/datasets/uploads/{upload_body['upload_id']}",
            headers={"X-API-Token": "test-secret"},
        )
        script_downloaded = client.get(
            f"/scripts/uploads/{script_body['upload_id']}",
            headers={"X-API-Token": "test-secret"},
        )
        dashboard_update = client.patch(
            "/dashboard/api/workers/mac-one", json={"enabled": False}
        )
        metrics = client.get("/dashboard/api/system")
        services = client.get("/dashboard/api/services")
        jobs = client.get("/dashboard/api/jobs")
        portal_submit = client.post(
            "/jobs-ui/api/jobs",
            headers={"Idempotency-Key": "family-check-01"},
            json={
                "name": "Family check",
                "type": "sleep",
                "parameters": {"seconds": 5},
            },
        )
        artifact_directory = tmp_path / "artifacts" / portal_submit.json()["id"]
        artifact_directory.mkdir(parents=True)
        (artifact_directory / "metrics.json").write_text('{"accuracy": 0.95}\n')
        portal_artifacts = client.get(
            f"/jobs-ui/api/jobs/{portal_submit.json()['id']}/artifacts"
        )
        portal_artifact_download = client.get(
            f"/jobs-ui/api/jobs/{portal_submit.json()['id']}/artifacts/metrics.json"
        )
        portal_jobs = client.get("/jobs-ui/api/jobs")
        uploaded_job = client.post(
            "/jobs-ui/api/jobs",
            json={
                "name": "Uploaded family CSV",
                "type": "dataset_script",
                "parameters": {
                    "script": "csv_summary",
                    "dataset": upload_body,
                    "timeout_seconds": 60,
                },
            },
        )

    assert page.status_code == 200
    assert "Homelab Dashboard" in page.text
    assert "homelab dashboard" in page.text
    assert "viewport-fit=cover" in page.text
    assert 'href="/jobs-ui"' in page.text
    jobs_card_position = page.text.index('class="service-card jobs"')
    jobs_link_position = page.text.index('href="/jobs-ui"')
    assert jobs_card_position < jobs_link_position
    assert "Control plane</div>" not in page.text
    assert "Job database</div>" not in page.text
    assert '<div class="service-name">Jobs</div>' in page.text
    assert '<div class="metric-label">RAM</div>' in page.text
    assert '<div class="metric-label">Disk space</div>' in page.text
    assert '<div class="metric-label">Pi root</div>' not in page.text
    assert 'api("/dashboard/api/jobs")' not in page.text
    assert jobs_page.status_code == 200
    assert "Submit a job" in jobs_page.text
    assert "Queue &amp; history" in jobs_page.text
    assert '"Idempotency-Key":submissionKey()' in jobs_page.text
    assert 'data-sort="name"' in jobs_page.text
    assert 'data-sort="id"' in jobs_page.text
    assert 'data-sort="created_at"' in jobs_page.text
    assert '"label","Artifacts"' in jobs_page.text
    assert '"DOWNLOAD"' in jobs_page.text
    assert "setInterval(refresh,10000)" in jobs_page.text
    assert "/dashboard/api/system" not in jobs_page.text
    assert "/dashboard/api/workers" not in jobs_page.text
    assert "sort-button" not in page.text
    assert "HomeStorage NAS" in page.text
    assert "GPU thermal" in page.text
    assert "thermal-critical" in page.text
    assert "DEACTIVATE" in page.text
    assert "setInterval(refresh,15000)" in page.text
    assert 'method:"PATCH"' in page.text
    assert "<table" not in page.text
    assert unauthenticated.status_code == 401
    assert unauthenticated_jobs.status_code == 401
    assert unauthenticated_artifacts.status_code == 401
    assert unauthenticated_submit.status_code == 401
    assert wrong.status_code == 401
    assert registered.status_code == 204
    assert dashboard_update_without_session.status_code == 401
    assert api_update_without_token.status_code == 401
    assert accepted.status_code == 204
    assert uploaded.status_code == 201
    assert script_uploaded.status_code == 201
    assert upload_body["size_bytes"] == len(b"name,value\na,1\n")
    assert len(upload_body["sha256"]) == 64
    assert unauthenticated_download.status_code == 401
    assert downloaded.status_code == 200
    assert downloaded.content == b"name,value\na,1\n"
    assert script_downloaded.status_code == 200
    assert script_downloaded.content == b"print('ok')\n"
    assert dashboard_update.status_code == 200
    assert dashboard_update.json()["enabled"] is False
    assert metrics.status_code == 200
    assert {"cpu", "memory", "storage", "uptime_seconds"} <= metrics.json().keys()
    assert services.status_code == 200
    assert {"control_plane", "database", "nas"} == services.json().keys()
    assert jobs.status_code == 200
    assert portal_submit.status_code == 201
    assert portal_submit.json()["name"] == "Family check"
    assert portal_submit.json()["status"] == "QUEUED"
    assert portal_jobs.status_code == 200
    assert portal_jobs.json()[0]["id"] == portal_submit.json()["id"]
    assert portal_artifacts.status_code == 200
    assert portal_artifacts.json() == [{"filename": "metrics.json", "size_bytes": 19}]
    assert portal_artifact_download.status_code == 200
    assert portal_artifact_download.content == b'{"accuracy": 0.95}\n'
    assert uploaded_job.status_code == 201
    assert uploaded_job.json()["parameters"]["dataset"] == upload_body


def test_dashboard_upload_rejects_invalid_or_oversized_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("HOME_PLATFORM_MAX_UPLOAD_BYTES", "8")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        client.post("/dashboard/login", json={"token": "test-secret"})
        wrong_type = client.post(
            "/jobs-ui/api/uploads",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        oversized = client.post(
            "/jobs-ui/api/uploads",
            files={"file": ("large.csv", b"123456789", "text/csv")},
        )

    assert wrong_type.status_code == 415
    assert oversized.status_code == 413
    assert list((tmp_path / "uploads").iterdir()) == []


def test_dashboard_session_survives_application_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    database_path = tmp_path / "jobs.db"
    with TestClient(create_app(database_path)) as first_client:
        login = first_client.post("/dashboard/login", json={"token": "test-secret"})
        session_cookie = first_client.cookies.get("home_platform_dashboard")

    assert login.status_code == 204
    assert session_cookie is not None

    with TestClient(create_app(database_path)) as restarted_client:
        restarted_client.cookies.set("home_platform_dashboard", session_cookie)
        metrics = restarted_client.get("/dashboard/api/system")

    assert metrics.status_code == 200


def running_job_with_lease(client: TestClient) -> tuple[dict, dict]:
    """Create a job and claim it, returning (job, claim) with a live lease."""
    created = create_sleep_job(client)
    enable_worker(client)
    claimed = claim_job(client)
    assert claimed is not None
    return created, claimed


def test_artifacts_upload_list_and_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {
            "worker_id": "mac-one",
            "lease_token": claimed["lease_token"],
        }
        first = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("model.joblib", b"weights")},
        )
        second = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("metrics.json", b'{"accuracy": 1.0}')},
        )
        listing = client.get(f"/jobs/{created['id']}/artifacts")
        download = client.get(f"/jobs/{created['id']}/artifacts/model.joblib")
        missing = client.get(f"/jobs/{created['id']}/artifacts/absent.bin")

    assert first.status_code == 201
    assert first.json()["sha256"] == hashlib.sha256(b"weights").hexdigest()
    assert first.json()["size_bytes"] == 7
    assert second.status_code == 201
    assert [item["filename"] for item in listing.json()] == [
        "metrics.json",
        "model.joblib",
    ]
    assert download.content == b"weights"
    assert missing.status_code == 404


def test_artifact_upload_requires_the_current_lease(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        payload = {"file": ("model.joblib", b"weights")}

        impostor = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-impostor", "lease_token": claimed["lease_token"]},
            files=payload,
        )
        stale_token = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": str(uuid4())},
            files=payload,
        )
        unknown_job = client.post(
            f"/jobs/{uuid4()}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files=payload,
        )
        # Finishing the job ends the lease, so publishing must stop working too.
        client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        after_completion = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files=payload,
        )
        listing = client.get(f"/jobs/{created['id']}/artifacts")

    assert impostor.status_code == 409
    assert stale_token.status_code == 409
    assert unknown_job.status_code == 404
    assert after_completion.status_code == 409
    assert listing.json() == []


def test_artifact_names_cannot_escape_the_job_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {
            "worker_id": "mac-one",
            "lease_token": claimed["lease_token"],
        }
        rejected = [
            client.post(
                f"/jobs/{created['id']}/artifacts",
                data=credentials,
                files={"file": (name, b"x")},
            ).status_code
            for name in ("../escape.txt", "/etc/passwd", ".hidden", "", "a/b.txt")
        ]
        download_escape = client.get(
            f"/jobs/{created['id']}/artifacts/..%2F..%2Fetc%2Fpasswd"
        )

    assert rejected == [422, 422, 422, 422, 422]
    assert download_escape.status_code in {404, 422}
    assert not (tmp_path / "escape.txt").exists()


def test_artifact_size_limits_are_enforced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HOME_PLATFORM_MAX_ARTIFACT_BYTES", "16")
    monkeypatch.setenv("HOME_PLATFORM_MAX_JOB_ARTIFACT_BYTES", "24")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {
            "worker_id": "mac-one",
            "lease_token": claimed["lease_token"],
        }
        too_big = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("big.bin", b"x" * 32)},
        )
        accepted = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("ok.bin", b"x" * 16)},
        )
        over_job_budget = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("second.bin", b"x" * 16)},
        )

    assert too_big.status_code == 413
    assert accepted.status_code == 201
    assert over_job_budget.status_code == 413


def test_artifacts_refuse_to_write_when_storage_is_not_mounted(
    tmp_path: Path, monkeypatch
) -> None:
    # Pointing at "/" makes the device check see the root filesystem, which is
    # exactly the "SSD is absent, do not fill the boot disk" condition.
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", "/")
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_REQUIRE_MOUNT", "true")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        refused = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files={"file": ("model.joblib", b"weights")},
        )
        listing = client.get(f"/jobs/{created['id']}/artifacts")

    assert refused.status_code == 503
    assert listing.status_code == 503


def python_batch_job(client: TestClient, name: str = "batch") -> tuple[dict, dict]:
    """Submit a python_batch job the way the CLI does: upload, then reference."""
    script = client.post(
        "/uploads/scripts", files={"file": ("train.py", b"print('hi')")}
    ).json()
    dataset = client.post(
        "/uploads/datasets", files={"file": ("data.csv", b"a,b\n1,2\n")}
    ).json()
    created = client.post(
        "/jobs",
        json={
            "name": name,
            "type": "python_batch",
            "parameters": {
                "script": script,
                "dataset": dataset,
                "timeout_seconds": 600,
                "cpu_limit": 2,
                "memory_mb": 2048,
            },
        },
    )
    assert created.status_code == 201
    return created.json(), {"script": script, "dataset": dataset}


def test_finishing_a_job_releases_its_staged_uploads(
    tmp_path: Path, monkeypatch
) -> None:
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, refs = python_batch_job(client)
        script_file = uploads / "scripts" / f"{refs['script']['upload_id']}.py"
        dataset_file = uploads / f"{refs['dataset']['upload_id']}.csv"
        assert script_file.exists() and dataset_file.exists()

        enable_worker(client, "mac-one", ["python_batch"])
        claimed = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["python_batch"]},
        ).json()
        client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {
                    "script_sha256": refs["script"]["sha256"],
                    "dataset_sha256": refs["dataset"]["sha256"],
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "output_files": [],
                    "artifact_uri": f"worker://mac-one/{created['id']}/",
                },
            },
        )

    assert not script_file.exists(), "finished job left its script staged"
    assert not dataset_file.exists(), "finished job left its dataset staged"


def test_uploads_shared_with_an_unfinished_job_are_kept(
    tmp_path: Path, monkeypatch
) -> None:
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first, refs = python_batch_job(client, "first")
        # A second job reusing the same uploads, left QUEUED.
        client.post(
            "/jobs",
            json={
                "name": "second",
                "type": "python_batch",
                "parameters": {
                    "script": refs["script"],
                    "dataset": refs["dataset"],
                    "timeout_seconds": 600,
                    "cpu_limit": 2,
                    "memory_mb": 2048,
                },
            },
        )
        enable_worker(client, "mac-one", ["python_batch"])
        claimed = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["python_batch"]},
        ).json()
        client.post(
            f"/jobs/{claimed['id']}/fail",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "error": "boom",
            },
        )
        script_file = uploads / "scripts" / f"{refs['script']['upload_id']}.py"

    assert script_file.exists(), "deleted an upload another queued job still needs"
    assert first is not None


def test_artifacts_can_be_deleted_explicitly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {"worker_id": "mac-one", "lease_token": claimed["lease_token"]}
        for name in ("model.joblib", "metrics.json"):
            client.post(
                f"/jobs/{created['id']}/artifacts",
                data=credentials,
                files={"file": (name, b"payload")},
            )
        one = client.delete(f"/jobs/{created['id']}/artifacts/metrics.json")
        remaining = client.get(f"/jobs/{created['id']}/artifacts")
        everything = client.delete(f"/jobs/{created['id']}/artifacts")
        after = client.get(f"/jobs/{created['id']}/artifacts")
        again = client.delete(f"/jobs/{created['id']}/artifacts")
        # The job record itself must survive; only the bytes go.
        job = client.get(f"/jobs/{created['id']}")

    assert one.status_code == 200
    assert one.json()["deleted"] == ["metrics.json"]
    assert [item["filename"] for item in remaining.json()] == ["model.joblib"]
    assert everything.json()["deleted"] == ["model.joblib"]
    assert after.json() == []
    assert again.status_code == 404
    assert job.json()["status"] == "RUNNING"


def test_store_cap_evicts_the_least_recently_touched_job(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HOME_PLATFORM_MAX_ARTIFACT_STORE_BYTES", "600")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first, first_claim = running_job_with_lease(client)
        client.post(
            f"/jobs/{first['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": first_claim["lease_token"]},
            files={"file": ("old.bin", b"x" * 400)},
        )
        # Finish the first job so a second can be claimed by the same worker.
        client.post(
            f"/jobs/{first['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": first_claim["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        second = create_sleep_job(client)
        second_claim = claim_job(client)
        assert second_claim is not None
        pushed = client.post(
            f"/jobs/{second['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": second_claim["lease_token"]},
            files={"file": ("new.bin", b"y" * 400)},
        )
        old = client.get(f"/jobs/{first['id']}/artifacts")
        new = client.get(f"/jobs/{second['id']}/artifacts")

    assert pushed.status_code == 201
    assert pushed.json()["evicted_jobs"] == [first["id"]]
    assert old.json() == [], "the older job should have been evicted"
    assert [item["filename"] for item in new.json()] == ["new.bin"]
