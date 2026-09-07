import sqlite3
from collections.abc import Callable
from pathlib import Path as Path_
from secrets import compare_digest
from typing import Annotated, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Request,
    Security,
    status,
)
from fastapi.security import APIKeyHeader

from app.service import (
    IdempotencyConflictError,
    JobNotFoundError,
    JobService,
    JobTransitionError,
    WorkerNotFoundError,
)
from app.version import VERSION
from contracts.models import (
    JobCompletion,
    JobCreate,
    JobFailure,
    JobRead,
    JobStatus,
    UploadedDatasetReference,
    UploadedScriptReference,
    WorkerClaim,
    WorkerHeartbeat,
    WorkerRead,
    WorkerUpdate,
)


def create_router() -> APIRouter:
    router = APIRouter()
    api_token_header = APIKeyHeader(name="X-API-Token", auto_error=False)

    def get_job_service(request: Request) -> JobService:
        return cast(JobService, request.app.state.job_service)

    def require_api_token(
        request: Request,
        supplied_token: Annotated[str | None, Security(api_token_header)],
    ) -> None:
        expected_token = request.app.state.settings.api_token
        if expected_token is None:
            return
        if supplied_token is None or not compare_digest(supplied_token, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API token",
            )

    JobServiceDependency = Annotated[JobService, Depends(get_job_service)]
    Authorized = Annotated[None, Depends(require_api_token)]

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    @router.get("/ready")
    def ready(job_service: JobServiceDependency) -> dict[str, str]:
        try:
            job_service.ping()
        except sqlite3.Error as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database is unavailable",
            ) from error
        return {"status": "ready"}

    @router.get("/version")
    def version() -> dict[str, str]:
        return {"name": "personal-home-platform", "version": VERSION}

    @router.post(
        "/jobs",
        response_model=JobRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_job(
        job_create: JobCreate,
        job_service: JobServiceDependency,
        _: Authorized,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
            ),
        ] = None,
    ) -> JobRead:
        try:
            return job_service.create(job_create, idempotency_key)
        except IdempotencyConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

    @router.get("/jobs", response_model=list[JobRead])
    def list_jobs(
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> list[JobRead]:
        return job_service.list()

    @router.get("/jobs/{job_id}", response_model=JobRead)
    def get_job(
        job_id: UUID,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        job = job_service.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job with ID {job_id} not found",
            )
        return job

    @router.post("/workers/claim", response_model=JobRead | None)
    def claim_job(
        claim: WorkerClaim,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead | None:
        return job_service.claim(claim)

    @router.post("/workers/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
    def worker_heartbeat(
        heartbeat: WorkerHeartbeat,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> None:
        try:
            job_service.heartbeat(heartbeat)
        except JobTransitionError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

    @router.get("/workers", response_model=list[WorkerRead])
    def list_workers(
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> list[WorkerRead]:
        return job_service.list_workers()

    @router.patch("/workers/{worker_id}", response_model=WorkerRead)
    def update_worker(
        worker_id: Annotated[
            str,
            Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        ],
        update: WorkerUpdate,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_enabled(worker_id, update.enabled)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.post("/jobs/{job_id}/complete", response_model=JobRead)
    def complete_job(
        job_id: UUID,
        completion: JobCompletion,
        request: Request,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        job = finish_job(lambda: job_service.complete(job_id, completion), job_id)
        release_uploads(request, job_service, job)
        return job

    @router.post("/jobs/{job_id}/fail", response_model=JobRead)
    def fail_job(
        job_id: UUID,
        failure: JobFailure,
        request: Request,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        job = finish_job(lambda: job_service.fail(job_id, failure), job_id)
        release_uploads(request, job_service, job)
        return job

    return router


def referenced_uploads(job: JobRead) -> set[UUID]:
    """Upload IDs a job depends on, if any.

    A linked HTTPS dataset has no upload, and `dataset_script` names a reviewed
    script rather than uploading one, so both fields have to be type-checked
    rather than assumed present.
    """
    found: set[UUID] = set()
    dataset = getattr(job.parameters, "dataset", None)
    if isinstance(dataset, UploadedDatasetReference):
        found.add(dataset.upload_id)
    script = getattr(job.parameters, "script", None)
    if isinstance(script, UploadedScriptReference):
        found.add(script.upload_id)
    return found


def release_uploads(request: Request, job_service: JobService, job: JobRead) -> None:
    """Delete a finished job's staged inputs.

    Uploads exist so a worker can fetch them; once the job reaches a terminal
    state nothing will ask for them again. They live on the coordinator's system
    disk and had no cleanup at all, so they accumulated forever.

    Two jobs can legitimately reference the same upload — the API allows reusing
    an upload ID — so anything still needed by an unfinished job is left alone.
    """
    wanted = referenced_uploads(job)
    if not wanted:
        return
    still_needed: set[UUID] = set()
    for other in job_service.list():
        if other.id != job.id and other.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            still_needed |= referenced_uploads(other)

    directory: Path_ = request.app.state.settings.upload_directory
    for upload_id in wanted - still_needed:
        (directory / f"{upload_id}.csv").unlink(missing_ok=True)
        (directory / "scripts" / f"{upload_id}.py").unlink(missing_ok=True)


def finish_job(action: Callable[[], JobRead], job_id: UUID) -> JobRead:
    try:
        return action()
    except JobNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID {job_id} not found",
        ) from None
    except JobTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
