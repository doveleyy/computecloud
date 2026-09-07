from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from contracts.models import (
    JobRead,
    JobResult,
    JobStatus,
    JobType,
    SleepParameters,
    SleepResult,
)
from worker.data_plane import WorkerWorkspace
from worker.main import (
    _libre_hardware_temperatures,
    execute,
    load_settings,
    publish_artifacts,
    run_once,
)


def running_job(seconds: int = 1) -> JobRead:
    now = datetime.now(UTC)
    return JobRead(
        id=uuid4(),
        type=JobType.SLEEP,
        parameters=SleepParameters(seconds=seconds),
        status=JobStatus.RUNNING,
        created_at=now,
        updated_at=now,
        worker_id="mac-one",
        started_at=now,
        lease_token=uuid4(),
        lease_expires_at=now,
    )


class FakeWorkerAPI:
    worker_id = "mac-one"

    def __init__(self, job: JobRead | None) -> None:
        self.job = job
        self.completed_result: JobResult | None = None
        self.failure: str | None = None
        self.heartbeats: list[JobRead | None] = []
        self.uploaded: list[str] = []

    def claim(self) -> JobRead | None:
        return self.job

    def complete(self, job: JobRead, result: JobResult) -> JobRead:
        self.completed_result = result
        return job.model_copy(update={"status": JobStatus.COMPLETED, "result": result})

    def fail(self, job: JobRead, error: str) -> JobRead:
        self.failure = error
        return job.model_copy(update={"status": JobStatus.FAILED, "error": error})

    def heartbeat(self, job: JobRead | None = None) -> None:
        self.heartbeats.append(job)

    def upload_artifact(self, job: JobRead, path: Path) -> None:
        self.uploaded.append(path.name)


def test_sleep_executor_uses_validated_duration(monkeypatch) -> None:
    slept_for: list[int] = []
    monkeypatch.setattr("worker.main.time.sleep", slept_for.append)

    result = execute(running_job(seconds=3))

    assert result.model_dump() == {"slept_seconds": 3}
    assert slept_for == [3]


def test_worker_reports_success(monkeypatch) -> None:
    client = FakeWorkerAPI(running_job(seconds=2))
    monkeypatch.setattr(
        "worker.main.execute",
        lambda job, workspace, worker_id: SleepResult(slept_seconds=2),
    )

    assert run_once(client) is True
    assert client.completed_result == SleepResult(slept_seconds=2)
    assert client.failure is None


def test_worker_does_nothing_when_queue_is_empty() -> None:
    client = FakeWorkerAPI(None)

    assert run_once(client) is False
    assert client.completed_result is None
    assert client.failure is None
    assert client.heartbeats == [None]


def test_worker_reports_execution_failure(monkeypatch) -> None:
    client = FakeWorkerAPI(running_job())

    def fail_execution(job: JobRead, workspace: object, worker_id: str) -> SleepResult:
        raise RuntimeError(f"cannot execute {job.id}")

    monkeypatch.setattr("worker.main.execute", fail_execution)

    assert run_once(client) is True
    assert client.completed_result is None
    assert client.failure is not None
    assert client.failure.startswith("RuntimeError: cannot execute")


def test_worker_settings_reject_invalid_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token_file = tmp_path / "api-token"
    token_file.write_text("test-secret")
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("HOME_PLATFORM_WORKER_ID", "spaces are invalid")

    with pytest.raises(ValueError, match="worker ID"):
        load_settings()

    monkeypatch.setenv("HOME_PLATFORM_WORKER_ID", "mac-one")
    with pytest.raises(ValueError, match="poll interval"):
        load_settings(poll_seconds=0)


def test_worker_settings_accept_cli_overrides(tmp_path: Path, monkeypatch) -> None:
    environment_token = tmp_path / "environment-token"
    environment_token.write_text("wrong-secret")
    explicit_token = tmp_path / "explicit-token"
    explicit_token.write_text("test-secret")
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(environment_token))

    settings = load_settings(
        poll_seconds=3,
        worker_id="windows-primary",
        api_url="http://raspberrypi.local:8000",
        heartbeat_seconds=4,
        token_file=explicit_token,
    )

    assert settings.api_token == "test-secret"
    assert settings.worker_id == "windows-primary"
    assert settings.api_url == "http://raspberrypi.local:8000"
    assert settings.poll_seconds == 3
    assert settings.heartbeat_seconds == 4


def test_libre_hardware_monitor_temperature_parsing(monkeypatch) -> None:
    monkeypatch.setattr("worker.main.platform.system", lambda: "Windows")
    monkeypatch.setattr("worker.main.shutil.which", lambda name: "powershell.exe")

    class Completed:
        stdout = (
            '[{"Identifier":"/intelcpu/0/temperature/0","Name":"CPU Core",'
            '"Value":68.5},{"Identifier":"/gpu-intel/0/temperature/0",'
            '"Name":"GPU Core","Value":54.0}]'
        )

    monkeypatch.setattr(
        "worker.main.subprocess.run", lambda *args, **kwargs: Completed()
    )

    assert _libre_hardware_temperatures() == (68.5, 54.0)


def test_libre_hardware_monitor_unavailable_is_not_fatal(monkeypatch) -> None:
    monkeypatch.setattr("worker.main.platform.system", lambda: "Windows")
    monkeypatch.setattr("worker.main.shutil.which", lambda name: None)

    assert _libre_hardware_temperatures() == (None, None)


def artifact_workspace(root: Path, job: JobRead, names: list[str]) -> WorkerWorkspace:
    directory = root / "artifacts" / str(job.id)
    directory.mkdir(parents=True)
    for name in names:
        (directory / name).write_bytes(b"payload")
    (directory / ".partial.tmp").write_bytes(b"ignore me")
    return WorkerWorkspace(
        root=root,
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
    )


def test_worker_publishes_artifacts_before_completing(tmp_path: Path) -> None:
    job = running_job()
    client = FakeWorkerAPI(job)
    workspace = artifact_workspace(tmp_path, job, ["model.joblib", "metrics.json"])

    published = publish_artifacts(client, job, workspace)

    # Sorted, and the in-progress dotfile is skipped.
    assert published == 2
    assert client.uploaded == ["metrics.json", "model.joblib"]


def test_publishing_is_a_no_op_without_artifacts(tmp_path: Path) -> None:
    job = running_job()
    client = FakeWorkerAPI(job)
    workspace = WorkerWorkspace(
        root=tmp_path,
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
    )

    assert publish_artifacts(client, job, workspace) == 0
    assert publish_artifacts(client, job, None) == 0
    assert client.uploaded == []


def test_artifact_upload_retries_then_fails_the_job(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("worker.main.time.sleep", lambda _seconds: None)
    job = running_job()
    workspace = artifact_workspace(tmp_path, job, ["model.joblib"])

    class FlakyThenFatal(FakeWorkerAPI):
        def __init__(self, job: JobRead | None, failures: int) -> None:
            super().__init__(job)
            self.failures = failures
            self.attempts = 0

        def upload_artifact(self, job: JobRead, path: Path) -> None:
            self.attempts += 1
            if self.attempts <= self.failures:
                raise RuntimeError("network hiccup")
            super().upload_artifact(job, path)

    recovers = FlakyThenFatal(job, failures=2)
    assert publish_artifacts(recovers, job, workspace) == 1
    assert recovers.attempts == 3

    # A publish that never succeeds must raise, so run_once fails the job rather
    # than reporting COMPLETED for results that went nowhere.
    persistent = FlakyThenFatal(job, failures=99)
    with pytest.raises(RuntimeError, match="network hiccup"):
        publish_artifacts(persistent, job, workspace)
    assert persistent.attempts == 3
