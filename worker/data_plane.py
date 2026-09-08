from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from contracts.models import (
    DatasetScriptParameters,
    DatasetScriptResult,
    DatasetSource,
    UploadedDatasetReference,
)


class DatasetPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerWorkspace:
    root: Path
    allowed_dataset_hosts: frozenset[str]
    max_dataset_bytes: int
    control_plane_url: str | None = None
    api_token: str | None = None
    docker_executable: str | None = None
    container_image: str = "home-platform-ml:0.1"


def run_dataset_script(
    job_id: UUID,
    worker_id: str,
    parameters: DatasetScriptParameters,
    workspace: WorkerWorkspace,
    cancellation_event: threading.Event | None = None,
) -> DatasetScriptResult:
    dataset_path = materialize_dataset(parameters.dataset, workspace)
    artifact_directory = workspace.root / "artifacts" / str(job_id)
    artifact_directory.mkdir(parents=True, exist_ok=True)
    summary_path = artifact_directory / "summary.json"
    command = [
        sys.executable,
        str(Path(__file__).with_name("scripts") / f"{parameters.script.value}.py"),
        "--input",
        str(dataset_path),
        "--output",
        str(summary_path),
    ]
    if cancellation_event is None:
        completed = subprocess.run(
            command,
            cwd=artifact_directory,
            env=_restricted_environment(),
            capture_output=True,
            text=True,
            timeout=parameters.timeout_seconds,
            check=False,
        )
    else:
        process = subprocess.Popen(
            command,
            cwd=artifact_directory,
            env=_restricted_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + parameters.timeout_seconds
        while True:
            if cancellation_event.is_set():
                process.kill()
                process.communicate()
                raise RuntimeError("job cancelled while running approved script")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(command, parameters.timeout_seconds)
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                completed = subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr
                )
                break
            except subprocess.TimeoutExpired:
                continue
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        raise RuntimeError(
            f"approved script {parameters.script.value!r} exited "
            f"with code {completed.returncode}: {detail}"
        )
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        rows = int(summary["rows"])
        columns = [str(value) for value in summary["columns"]]
    except (
        FileNotFoundError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise RuntimeError("approved script produced an invalid summary") from error

    result = DatasetScriptResult(
        script=parameters.script,
        dataset_sha256=parameters.dataset.sha256,
        dataset_bytes=parameters.dataset.size_bytes,
        rows=rows,
        columns=columns,
        artifact_uri=f"worker://{worker_id}/{job_id}/summary.json",
    )
    summary_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def materialize_dataset(
    reference: DatasetSource,
    workspace: WorkerWorkspace,
    cancellation_event: threading.Event | None = None,
) -> Path:
    if cancellation_event is not None and cancellation_event.is_set():
        raise RuntimeError("job cancelled while preparing its dataset")
    if isinstance(reference, UploadedDatasetReference):
        if workspace.control_plane_url is None or workspace.api_token is None:
            raise DatasetPolicyError("uploaded dataset access is not configured")
        source_url = (
            f"{workspace.control_plane_url.rstrip('/')}/datasets/uploads/"
            f"{reference.upload_id}"
        )
        headers = {
            "Accept-Encoding": "identity",
            "X-API-Token": workspace.api_token,
        }
        source_label = f"upload:{reference.upload_id}"
    else:
        hostname = (reference.url.host or "").lower()
        if hostname not in workspace.allowed_dataset_hosts:
            raise DatasetPolicyError(
                f"dataset host {hostname!r} is not in this worker's allowlist"
            )
        source_url = str(reference.url)
        headers = {"Accept-Encoding": "identity"}
        source_label = hostname
    if reference.size_bytes > workspace.max_dataset_bytes:
        raise DatasetPolicyError(
            f"declared dataset size {reference.size_bytes} exceeds worker limit "
            f"{workspace.max_dataset_bytes}"
        )

    cache_directory = workspace.root / "cache"
    cache_directory.mkdir(parents=True, exist_ok=True)
    target = cache_directory / reference.sha256
    if target.is_file() and _matches_reference(target, reference):
        logging.info("dataset=%s cache=hit", reference.sha256)
        return target
    if target.exists():
        target.unlink()

    temporary = cache_directory / f".{reference.sha256}.{uuid4().hex}.part"
    digest = hashlib.sha256()
    received = 0
    logging.info(
        "dataset=%s cache=miss bytes=%s source_host=%s",
        reference.sha256,
        reference.size_bytes,
        source_label,
    )
    try:
        with httpx.stream(
            "GET",
            source_url,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(30, read=120),
        ) as response:
            if response.is_redirect:
                raise DatasetPolicyError("dataset redirects are not allowed")
            response.raise_for_status()
            declared_length = response.headers.get("Content-Length")
            if (
                declared_length is not None
                and int(declared_length) != reference.size_bytes
            ):
                raise DatasetPolicyError(
                    "dataset Content-Length does not match declared size"
                )
            with temporary.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise RuntimeError(
                            "job cancelled while downloading its dataset"
                        )
                    received += len(chunk)
                    if received > reference.size_bytes:
                        raise DatasetPolicyError("dataset exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
        if received != reference.size_bytes:
            raise DatasetPolicyError("dataset size does not match declared size")
        if digest.hexdigest() != reference.sha256:
            raise DatasetPolicyError("dataset SHA-256 does not match declaration")
        os.replace(temporary, target)
        logging.info("dataset=%s cache=stored bytes=%s", reference.sha256, received)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _matches_reference(path: Path, reference: DatasetSource) -> bool:
    if path.stat().st_size != reference.size_bytes:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as dataset:
        for chunk in iter(lambda: dataset.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == reference.sha256


def _restricted_environment() -> dict[str, str]:
    allowed = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment
