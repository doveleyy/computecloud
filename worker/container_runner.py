from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from contracts.models import PythonBatchParameters, PythonBatchResult
from worker.data_plane import DatasetPolicyError, WorkerWorkspace, materialize_dataset


def run_python_batch(
    job_id: UUID,
    worker_id: str,
    parameters: PythonBatchParameters,
    workspace: WorkerWorkspace,
) -> PythonBatchResult:
    if workspace.docker_executable is None:
        raise RuntimeError("Docker runtime is unavailable")
    dataset_path = materialize_dataset(parameters.dataset, workspace)
    script_path = materialize_script(parameters, workspace)

    run_directory = workspace.root / "runs" / str(job_id)
    input_directory = run_directory / "input"
    output_directory = workspace.root / "artifacts" / str(job_id)
    if run_directory.exists():
        shutil.rmtree(run_directory)
    if output_directory.exists():
        shutil.rmtree(output_directory)
    input_directory.mkdir(parents=True)
    output_directory.mkdir(parents=True)
    output_directory.chmod(0o777)
    shutil.copy2(script_path, input_directory / "job.py")
    shutil.copy2(dataset_path, input_directory / "dataset.csv")

    container_name = f"home-platform-{str(job_id)[:12]}-{uuid4().hex[:6]}"
    memory = f"{parameters.memory_mb}m"

    # Match library thread pools to the CPU quota.
    #
    # `--cpus` caps how much CPU time the container may consume, but it does not
    # change how many cores the container *sees*. joblib reads the cgroup quota
    # and behaves, but native BLAS libraries do not: OpenBLAS starts one thread
    # per host core regardless. Under `--cpus 1` on an 8-core host that means 8
    # threads contending for one core's worth of quota — measured at roughly
    # 3.5x slower than the same work with one thread, for identical CPU budget.
    #
    # This matters most for exactly the case the limit exists to serve: running
    # something deliberately slowly to keep a laptop cool.
    threads = max(1, int(parameters.cpu_limit))
    thread_environment = [
        argument
        for variable in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        )
        for argument in ("--env", f"{variable}={threads}")
    ]
    command = [
        workspace.docker_executable,
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"home-platform.job-id={job_id}",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--cpus",
        str(parameters.cpu_limit),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--mount",
        f"type=bind,source={input_directory.resolve()},target=/workspace/input,readonly",
        "--mount",
        f"type=bind,source={output_directory.resolve()},target=/workspace/output",
        "--env",
        "HOME_PLATFORM_DATASET=/workspace/input/dataset.csv",
        "--env",
        "HOME_PLATFORM_OUTPUT_DIR=/workspace/output",
        "--env",
        f"HOME_PLATFORM_JOB_ID={job_id}",
        # Advertised so a script can size its own parallelism to the quota
        # rather than to the host, e.g. GridSearchCV(n_jobs=...).
        "--env",
        f"HOME_PLATFORM_CPU_LIMIT={parameters.cpu_limit}",
        *thread_environment,
        workspace.container_image,
        "python",
        "-I",
        "/workspace/input/job.py",
    ]
    logging.info(
        "job=%s container=%s image=%s cpus=%s memory_mb=%s",
        job_id,
        container_name,
        workspace.container_image,
        parameters.cpu_limit,
        parameters.memory_mb,
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=parameters.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        subprocess.run(
            [workspace.docker_executable, "rm", "-f", container_name],
            capture_output=True,
            timeout=15,
            check=False,
        )
        raise TimeoutError(
            f"container exceeded {parameters.timeout_seconds} second timeout"
        ) from error

    stdout = completed.stdout[-8000:]
    stderr = completed.stderr[-8000:]
    if completed.returncode != 0:
        detail = (stderr or stdout or "no diagnostic output")[-1000:]
        raise RuntimeError(
            f"batch container exited with code {completed.returncode}: {detail}"
        )
    output_files = sorted(
        path.name
        for path in output_directory.iterdir()
        if path.is_file() and len(path.name) <= 200
    )[:100]
    return PythonBatchResult(
        script_sha256=parameters.script.sha256,
        dataset_sha256=parameters.dataset.sha256,
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        output_files=output_files,
        artifact_uri=f"worker://{worker_id}/{job_id}/",
    )


def materialize_script(
    parameters: PythonBatchParameters,
    workspace: WorkerWorkspace,
) -> Path:
    if workspace.control_plane_url is None or workspace.api_token is None:
        raise DatasetPolicyError("uploaded script access is not configured")
    reference = parameters.script
    cache_directory = workspace.root / "scripts"
    cache_directory.mkdir(parents=True, exist_ok=True)
    target = cache_directory / f"{reference.sha256}.py"
    if target.is_file() and _matches(target, reference.sha256, reference.size_bytes):
        return target
    target.unlink(missing_ok=True)
    temporary = cache_directory / f".{reference.sha256}.{uuid4().hex}.part"
    url = (
        f"{workspace.control_plane_url.rstrip('/')}/scripts/uploads/"
        f"{reference.upload_id}"
    )
    digest = hashlib.sha256()
    received = 0
    try:
        with httpx.stream(
            "GET",
            url,
            headers={
                "Accept-Encoding": "identity",
                "X-API-Token": workspace.api_token,
            },
            follow_redirects=False,
            timeout=httpx.Timeout(30, read=120),
        ) as response:
            response.raise_for_status()
            with temporary.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    received += len(chunk)
                    if received > reference.size_bytes:
                        raise DatasetPolicyError("script exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
        if received != reference.size_bytes:
            raise DatasetPolicyError("script size does not match declaration")
        if digest.hexdigest() != reference.sha256:
            raise DatasetPolicyError("script SHA-256 does not match declaration")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _matches(path: Path, sha256: str, size_bytes: int) -> bool:
    if path.stat().st_size != size_bytes:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == sha256
