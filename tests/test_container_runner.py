import hashlib
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from contracts.models import (
    PythonBatchParameters,
    UploadedDatasetReference,
    UploadedScriptReference,
)
from worker.container_runner import materialize_script, run_python_batch
from worker.data_plane import WorkerWorkspace


class ResponseStream:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *_: object) -> None:
        self.response.close()


def batch_parameters(script: bytes, dataset: bytes) -> PythonBatchParameters:
    return PythonBatchParameters(
        script=UploadedScriptReference(
            upload_id=uuid4(),
            sha256=hashlib.sha256(script).hexdigest(),
            size_bytes=len(script),
        ),
        dataset=UploadedDatasetReference(
            upload_id=uuid4(),
            sha256=hashlib.sha256(dataset).hexdigest(),
            size_bytes=len(dataset),
        ),
        timeout_seconds=30,
        cpu_limit=1.5,
        memory_mb=1024,
    )


def workspace(tmp_path: Path) -> WorkerWorkspace:
    return WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024 * 1024,
        control_plane_url="http://pi.local:8000",
        api_token="worker-secret",
        docker_executable="docker",
        container_image="home-platform-ml:0.1",
    )


def test_python_batch_uses_isolated_limited_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = b"print('trained')\n"
    dataset = b"feature,target\n1,0\n"
    parameters = batch_parameters(script, dataset)
    source_script = tmp_path / "source.py"
    source_dataset = tmp_path / "source.csv"
    source_script.write_bytes(script)
    source_dataset.write_bytes(dataset)
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda _parameters, _workspace: source_script,
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset",
        lambda _reference, _workspace: source_dataset,
    )

    class Completed:
        returncode = 0
        stdout = "trained\n"
        stderr = ""

    def run(command: list[str], **_kwargs: object) -> Completed:
        commands.append(command)
        return Completed()

    monkeypatch.setattr("worker.container_runner.subprocess.run", run)
    job_id = uuid4()
    result = run_python_batch(
        job_id, "windows-primary", parameters, workspace(tmp_path)
    )

    command = commands[0]
    assert command[:2] == ["docker", "run"]
    assert command[command.index("--network") : command.index("--network") + 2] == [
        "--network",
        "none",
    ]
    assert "--read-only" in command
    assert "no-new-privileges" in command
    assert command[command.index("--cpus") + 1] == "1.5"
    assert command[command.index("--memory") + 1] == "1024m"
    assert "worker-secret" not in command
    assert result.stdout == "trained\n"
    assert result.artifact_uri == f"worker://windows-primary/{job_id}/"


def test_python_batch_timeout_force_removes_only_its_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = b"while True: pass\n"
    dataset = b"x\n1\n"
    parameters = batch_parameters(script, dataset)
    source_script = tmp_path / "source.py"
    source_dataset = tmp_path / "source.csv"
    source_script.write_bytes(script)
    source_dataset.write_bytes(dataset)
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda _parameters, _workspace: source_script,
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset",
        lambda _reference, _workspace: source_dataset,
    )

    def run(command: list[str], **_kwargs: object) -> object:
        commands.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 30)
        return object()

    monkeypatch.setattr("worker.container_runner.subprocess.run", run)

    with pytest.raises(TimeoutError, match="30 second timeout"):
        run_python_batch(uuid4(), "windows-primary", parameters, workspace(tmp_path))

    container_name = commands[0][commands[0].index("--name") + 1]
    assert commands[1] == ["docker", "rm", "-f", container_name]


def test_uploaded_script_is_downloaded_with_token_and_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"print('hello')\n"
    parameters = batch_parameters(content, b"x\n1\n")
    request_details: dict[str, object] = {}

    def stream(*args: object, **kwargs: object) -> ResponseStream:
        request_details["url"] = args[1]
        request_details["headers"] = kwargs["headers"]
        return ResponseStream(
            httpx.Response(
                200,
                content=content,
                request=httpx.Request("GET", str(args[1])),
            )
        )

    monkeypatch.setattr("worker.container_runner.httpx.stream", stream)
    path = materialize_script(parameters, workspace(tmp_path))

    assert path.read_bytes() == content
    assert request_details["url"] == (
        f"http://pi.local:8000/scripts/uploads/{parameters.script.upload_id}"
    )
    assert request_details["headers"] == {
        "Accept-Encoding": "identity",
        "X-API-Token": "worker-secret",
    }
