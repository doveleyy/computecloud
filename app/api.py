import sqlite3
from collections.abc import Callable
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
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        return finish_job(lambda: job_service.complete(job_id, completion), job_id)

    @router.post("/jobs/{job_id}/fail", response_model=JobRead)
    def fail_job(
        job_id: UUID,
        failure: JobFailure,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        return finish_job(lambda: job_service.fail(job_id, failure), job_id)

    return router


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
