import hashlib
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import psutil
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import (
    Path as ApiPath,
)
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict

from app.accounts import (
    AccountStore,
    DashboardLogin,
    InvalidCurrentPasswordError,
    PasswordChange,
    PasswordReset,
    SessionIdentity,
    UserCreate,
    UserExistsError,
    UserNotFoundError,
    UserRead,
    UserUpdate,
    decode_session,
    encode_session,
)
from app.batch_script import BatchScriptError, validate_project_archive
from app.identity import ADMIN_USER_ID
from app.job_http import (
    create_batch_submission,
    finish_job,
    referenced_uploads,
    release_uploads,
)
from app.job_http import create_job as create_job_from_client
from app.power import (
    PowerAction,
    PowerControlUnavailableError,
    PowerRequestPendingError,
    queue_power_request,
)
from app.service import (
    IdempotencyConflictError,
    JobGroupNotFoundError,
    JobNotFoundError,
    JobService,
    JobTransitionError,
    SchedulingCapacityError,
    WorkerNotFoundError,
)
from app.storage import (
    STORAGE_ID,
    StoragePolicyError,
    browse_storage,
    package_storage_project,
    resolve_storage_path,
    storage_file_reference,
)
from app.version import VERSION
from contracts.models import (
    BatchSubmissionCreate,
    JobCreate,
    JobGroupCreate,
    JobGroupRead,
    JobRead,
    JobStatus,
    StorageInputReference,
    UploadedDatasetReference,
    UploadedInputReference,
    UploadedProjectReference,
    UploadedScriptReference,
    WorkerCapacityUpdate,
    WorkerRead,
    WorkerUpdate,
)

SESSION_COOKIE = "home_platform_dashboard"
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text()
JOBS_HTML = Path(__file__).with_name("jobs.html").read_text()


class StoragePathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class PiPowerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PowerAction
    confirmation: str


def create_dashboard_router() -> APIRouter:
    router = APIRouter()
    anonymous_session = token_urlsafe(32)

    def session_secret(request: Request) -> str:
        return request.app.state.settings.api_token or anonymous_session

    def get_account_store(request: Request) -> AccountStore:
        return cast(AccountStore, request.app.state.account_store)

    def require_dashboard_session(
        request: Request,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        supplied: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> SessionIdentity:
        claims = (
            decode_session(supplied, session_secret(request), int(time.time()))
            if supplied is not None
            else None
        )
        identity = (
            account_store.get_identity(claims[0], claims[1])
            if claims is not None
            else None
        )
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Dashboard login required",
            )
        return identity

    def require_admin_session(
        identity: Annotated[SessionIdentity, Depends(require_dashboard_session)],
    ) -> SessionIdentity:
        if not identity.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access required",
            )
        return identity

    def require_api_token(
        request: Request,
        supplied: Annotated[str | None, Header(alias="X-API-Token")] = None,
    ) -> None:
        expected = request.app.state.settings.api_token
        if expected is not None and (
            supplied is None or not compare_digest(supplied, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API token",
            )

    DashboardSession = Annotated[SessionIdentity, Depends(require_dashboard_session)]
    AdminSession = Annotated[SessionIdentity, Depends(require_admin_session)]
    ApiToken = Annotated[None, Depends(require_api_token)]

    def get_job_service(request: Request) -> JobService:
        return cast(JobService, request.app.state.job_service)

    JobServiceDependency = Annotated[JobService, Depends(get_job_service)]

    def owner_scope(identity: SessionIdentity) -> str | None:
        return None if identity.is_admin else str(identity.id)

    def validate_upload_ownership(
        account_store: AccountStore,
        identity: SessionIdentity,
        jobs: list[JobCreate],
    ) -> None:
        if identity.is_admin:
            return
        for job in jobs:
            for source in getattr(job.parameters, "inputs", {}).values():
                if isinstance(source, StorageInputReference):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Personal NAS storage is not provisioned yet",
                    )
            for upload_id in referenced_uploads(job):
                if not account_store.upload_belongs_to(upload_id, identity.id):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Uploaded object not found",
                    )

    def validate_upload_ids(
        account_store: AccountStore,
        identity: SessionIdentity,
        upload_ids: set[UUID],
    ) -> None:
        if identity.is_admin:
            return
        for upload_id in upload_ids:
            if not account_store.upload_belongs_to(upload_id, identity.id):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Uploaded object not found",
                )

    async def save_upload(
        file: UploadFile,
        *,
        directory: Path,
        suffix: str,
        max_bytes: int,
        label: str,
        validate_suffix: bool = True,
    ) -> tuple[UUID, str, int]:
        filename = file.filename or ""
        if validate_suffix and Path(filename).suffix.lower() != suffix:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Only {suffix} files are accepted",
            )
        upload_id = uuid4()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{upload_id}{suffix}"
        temporary = directory / f".{upload_id}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=f"{label} exceeds the {max_bytes // 1024} KB limit",
                        )
                    digest.update(chunk)
                    output.write(chunk)
            if size == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{label} cannot be empty",
                )
            os.replace(temporary, target)
        finally:
            await file.close()
            temporary.unlink(missing_ok=True)
        return upload_id, digest.hexdigest(), size

    async def save_project_upload(
        request: Request, file: UploadFile
    ) -> UploadedProjectReference:
        directory = request.app.state.settings.upload_directory / "projects"
        upload_id, digest, size = await save_upload(
            file,
            directory=directory,
            suffix=".zip",
            max_bytes=request.app.state.settings.max_project_upload_bytes,
            label="Project archive",
        )
        target = directory / f"{upload_id}.zip"
        try:
            validate_project_archive(target)
        except BatchScriptError as error:
            target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        return UploadedProjectReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    @router.get("/dashboard/operations", response_class=HTMLResponse)
    def dashboard_operations() -> str:
        return DASHBOARD_HTML

    @router.post("/dashboard/login", status_code=status.HTTP_204_NO_CONTENT)
    def login(
        credentials: DashboardLogin,
        request: Request,
        response: Response,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
    ) -> None:
        expected = request.app.state.settings.api_token
        identity: SessionIdentity | None = None
        if credentials.token is not None:
            valid = expected is None or compare_digest(credentials.token, expected)
            if valid:
                identity = account_store.get_identity(UUID(ADMIN_USER_ID))
        elif credentials.username is not None and credentials.password is not None:
            identity = account_store.authenticate(
                credentials.username.strip().lower(), credentials.password
            )
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        expires_at = int(time.time()) + 30 * 24 * 60 * 60
        response.set_cookie(
            SESSION_COOKIE,
            encode_session(
                identity,
                session_secret(request),
                expires_at,
                account_store.session_version(identity.id),
            ),
            httponly=True,
            samesite="strict",
            max_age=30 * 24 * 60 * 60,
            path="/",
        )

    @router.post("/dashboard/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    @router.get("/jobs-ui/api/session", response_model=SessionIdentity)
    def jobs_portal_session(identity: DashboardSession) -> SessionIdentity:
        return identity

    @router.post(
        "/jobs-ui/api/account/password",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def jobs_portal_change_password(
        password: PasswordChange,
        response: Response,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
    ) -> None:
        if identity.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The owner API token is managed outside Job Desk",
            )
        try:
            account_store.change_password(
                identity.id, password.current_password, password.new_password
            )
        except InvalidCurrentPasswordError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            ) from error
        response.delete_cookie(SESSION_COOKIE, path="/")

    @router.get("/jobs-ui/api/workload-owners")
    def jobs_portal_workload_owners(
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> dict[str, dict[str, str]]:
        return account_store.workload_owners()

    @router.get("/dashboard/api/users", response_model=list[UserRead])
    def dashboard_users(
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> list[UserRead]:
        return account_store.list()

    @router.post(
        "/dashboard/api/users",
        response_model=UserRead,
        status_code=status.HTTP_201_CREATED,
    )
    def dashboard_create_user(
        user_create: UserCreate,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> UserRead:
        try:
            return account_store.create(user_create)
        except UserExistsError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already exists",
            ) from error

    @router.patch("/dashboard/api/users/{user_id}", response_model=UserRead)
    def dashboard_update_user(
        user_id: UUID,
        update: UserUpdate,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> UserRead:
        try:
            return account_store.set_disabled(user_id, update.disabled)
        except UserNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Member account not found",
            ) from error

    @router.put(
        "/dashboard/api/users/{user_id}/password",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def dashboard_reset_user_password(
        user_id: UUID,
        password: PasswordReset,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> None:
        try:
            account_store.reset_password(user_id, password.new_password)
        except UserNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Member account not found",
            ) from error

    @router.get("/jobs-ui", response_class=HTMLResponse)
    def jobs_page() -> str:
        return JOBS_HTML

    @router.get("/jobs-ui/new", response_class=HTMLResponse)
    def jobs_submit_page() -> str:
        return JOBS_HTML

    @router.get("/dashboard/api/system")
    def system_metrics(_: AdminSession) -> dict[str, Any]:
        return collect_system_metrics()

    @router.get("/dashboard/api/services")
    def dashboard_services(
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> dict[str, Any]:
        return collect_service_health(job_service)

    @router.get("/dashboard/api/workers", response_model=list[WorkerRead])
    def dashboard_workers(
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> list[WorkerRead]:
        return job_service.list_workers()

    @router.patch("/dashboard/api/workers/{worker_id}", response_model=WorkerRead)
    def dashboard_update_worker(
        worker_id: Annotated[
            str,
            ApiPath(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        ],
        update: WorkerUpdate,
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_enabled(worker_id, update.enabled)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.put(
        "/dashboard/api/workers/{worker_id}/capacity", response_model=WorkerRead
    )
    def dashboard_update_worker_capacity(
        worker_id: Annotated[
            str,
            ApiPath(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        ],
        capacity: WorkerCapacityUpdate,
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_capacity(worker_id, capacity)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.get("/dashboard/api/jobs", response_model=list[JobRead])
    def dashboard_jobs(
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> list[JobRead]:
        return job_service.list()

    @router.post("/dashboard/api/system/power", status_code=status.HTTP_202_ACCEPTED)
    def dashboard_power(
        power_request: PiPowerRequest,
        request: Request,
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> dict[str, str]:
        if request.app.state.settings.api_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pi power control requires configured authentication",
            )
        expected = power_request.action.value.upper()
        if power_request.confirmation != expected:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Type {expected} exactly to confirm",
            )

        workers = job_service.list_workers()
        enabled_workers = [worker.id for worker in workers if worker.enabled]
        if enabled_workers:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Disable scheduling on every worker before controlling Pi power: "
                    + ", ".join(enabled_workers)
                ),
            )
        running_jobs = [
            str(job.id) for job in job_service.list() if job.status is JobStatus.RUNNING
        ]
        if running_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Wait for or cancel running jobs before controlling Pi power: "
                    + ", ".join(running_jobs)
                ),
            )
        try:
            queue_power_request(
                request.app.state.settings.power_request_directory,
                power_request.action,
            )
        except PowerControlUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error
        except PowerRequestPendingError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        return {
            "action": power_request.action.value,
            "state": "ACCEPTED",
            "message": "The NAS will be stopped and the SSD safely unmounted first",
        }

    @router.get("/jobs-ui/api/jobs", response_model=list[JobRead])
    def jobs_portal_list(
        job_service: JobServiceDependency,
        identity: DashboardSession,
    ) -> list[JobRead]:
        return job_service.list(owner_scope(identity))

    @router.get("/jobs-ui/api/workers", response_model=list[WorkerRead])
    def jobs_portal_workers(
        job_service: JobServiceDependency,
        identity: DashboardSession,
    ) -> list[WorkerRead]:
        workers = job_service.list_workers()
        if identity.is_admin:
            return workers
        return [
            worker.model_copy(update={"metrics": None, "current_job_id": None})
            for worker in workers
        ]

    @router.post(
        "/jobs-ui/api/jobs",
        response_model=JobRead,
        status_code=status.HTTP_201_CREATED,
    )
    def jobs_portal_create(
        job_create: JobCreate,
        job_service: JobServiceDependency,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
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
        validate_upload_ownership(account_store, identity, [job_create])
        return create_job_from_client(
            job_service, job_create, idempotency_key, str(identity.id)
        )

    @router.get("/jobs-ui/api/job-groups", response_model=list[JobGroupRead])
    def jobs_portal_groups(
        job_service: JobServiceDependency,
        identity: DashboardSession,
    ) -> list[JobGroupRead]:
        return job_service.list_groups(owner_scope(identity))

    @router.get("/jobs-ui/api/job-groups/{group_id}", response_model=JobGroupRead)
    def jobs_portal_group(
        group_id: UUID,
        job_service: JobServiceDependency,
        identity: DashboardSession,
    ) -> JobGroupRead:
        try:
            return job_service.get_group(group_id, owner_scope(identity))
        except JobGroupNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job group with ID {group_id} not found",
            ) from None

    @router.post(
        "/jobs-ui/api/job-groups",
        response_model=JobGroupRead,
        status_code=status.HTTP_201_CREATED,
    )
    def jobs_portal_create_group(
        group_create: JobGroupCreate,
        job_service: JobServiceDependency,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
            ),
        ] = None,
    ) -> JobGroupRead:
        try:
            validate_upload_ownership(
                account_store, identity, [task.job for task in group_create.tasks]
            )
            return job_service.create_group(
                group_create, idempotency_key, str(identity.id)
            )
        except WorkerNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error
        except SchedulingCapacityError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        except IdempotencyConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

    @router.post("/jobs-ui/api/jobs/{job_id}/cancel", response_model=JobRead)
    def jobs_portal_cancel(
        job_id: UUID,
        request: Request,
        job_service: JobServiceDependency,
        identity: DashboardSession,
    ) -> JobRead:
        job = finish_job(
            lambda: job_service.cancel(job_id, owner_scope(identity)), job_id
        )
        if job.status is JobStatus.FAILED:
            release_uploads(request, job_service, job)
        return job

    @router.post(
        "/jobs-ui/api/uploads",
        response_model=UploadedDatasetReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_upload(
        request: Request,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedDatasetReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory,
            suffix=".csv",
            max_bytes=settings.max_upload_bytes,
            label="CSV file",
        )
        account_store.record_upload(upload_id, identity.id, "dataset")
        return UploadedDatasetReference(
            upload_id=upload_id,
            sha256=digest,
            size_bytes=size,
        )

    @router.post(
        "/jobs-ui/api/script-uploads",
        response_model=UploadedScriptReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_script_upload(
        request: Request,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedScriptReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory / "scripts",
            suffix=".py",
            max_bytes=settings.max_script_upload_bytes,
            label="Python script",
        )
        account_store.record_upload(upload_id, identity.id, "script")
        return UploadedScriptReference(
            upload_id=upload_id,
            sha256=digest,
            size_bytes=size,
        )

    @router.post(
        "/jobs-ui/api/project-uploads",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_project_upload(
        request: Request,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedProjectReference:
        project = await save_project_upload(request, file)
        account_store.record_upload(project.upload_id, identity.id, "project")
        return project

    @router.post(
        "/jobs-ui/api/input-uploads",
        response_model=UploadedInputReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_input_upload(
        request: Request,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedInputReference:
        upload_id, digest, size = await save_upload(
            file,
            directory=request.app.state.settings.upload_directory / "inputs",
            suffix=".input",
            max_bytes=request.app.state.settings.max_project_upload_bytes,
            label="Input file",
            validate_suffix=False,
        )
        account_store.record_upload(upload_id, identity.id, "input")
        return UploadedInputReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    def storage_error(error: StoragePolicyError) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )

    @router.get("/jobs-ui/api/storage")
    def jobs_portal_storage(
        request: Request,
        _: AdminSession,
        path: str = "",
    ) -> dict[str, Any]:
        try:
            entries = browse_storage(request.app.state.settings.storage_directory, path)
        except StoragePolicyError as error:
            raise storage_error(error) from error
        return {
            "storage_id": STORAGE_ID,
            "path": path,
            "entries": [entry.__dict__ for entry in entries],
        }

    @router.post(
        "/jobs-ui/api/storage/references",
        response_model=StorageInputReference,
    )
    def jobs_portal_storage_reference(
        selection: StoragePathRequest,
        request: Request,
        _: AdminSession,
    ) -> StorageInputReference:
        try:
            return storage_file_reference(
                request.app.state.settings.storage_directory, selection.path
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error

    @router.post(
        "/jobs-ui/api/storage/project-uploads",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    def jobs_portal_storage_project(
        selection: StoragePathRequest,
        request: Request,
        _: AdminSession,
    ) -> UploadedProjectReference:
        settings = request.app.state.settings
        try:
            return package_storage_project(
                settings.storage_directory,
                selection.path,
                settings.upload_directory / "projects",
                settings.max_project_upload_bytes,
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error

    @router.post(
        "/jobs-ui/api/batch-submissions",
        response_model=JobGroupRead,
        status_code=status.HTTP_201_CREATED,
    )
    def jobs_portal_batch_submission(
        submission: BatchSubmissionCreate,
        request: Request,
        job_service: JobServiceDependency,
        identity: DashboardSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ] = None,
    ) -> JobGroupRead:
        upload_ids = {submission.project.upload_id}
        for source in submission.inputs.values():
            if not identity.is_admin and isinstance(source, StorageInputReference):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Personal NAS storage is not provisioned yet",
                )
            if isinstance(source, UploadedInputReference):
                upload_ids.add(source.upload_id)
        validate_upload_ids(account_store, identity, upload_ids)
        return create_batch_submission(
            request, job_service, submission, idempotency_key, str(identity.id)
        )

    @router.post(
        "/uploads/datasets",
        response_model=UploadedDatasetReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_dataset_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedDatasetReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory,
            suffix=".csv",
            max_bytes=settings.max_upload_bytes,
            label="CSV file",
        )
        return UploadedDatasetReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.post(
        "/uploads/scripts",
        response_model=UploadedScriptReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_script_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedScriptReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory / "scripts",
            suffix=".py",
            max_bytes=settings.max_script_upload_bytes,
            label="Python script",
        )
        return UploadedScriptReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.post(
        "/uploads/projects",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_project_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedProjectReference:
        return await save_project_upload(request, file)

    @router.post(
        "/uploads/inputs",
        response_model=UploadedInputReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_input_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedInputReference:
        upload_id, digest, size = await save_upload(
            file,
            directory=request.app.state.settings.upload_directory / "inputs",
            suffix=".input",
            max_bytes=request.app.state.settings.max_project_upload_bytes,
            label="Input file",
            validate_suffix=False,
        )
        return UploadedInputReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.get("/storage")
    def api_storage(
        request: Request,
        _: ApiToken,
        path: str = "",
    ) -> dict[str, Any]:
        try:
            entries = browse_storage(request.app.state.settings.storage_directory, path)
        except StoragePolicyError as error:
            raise storage_error(error) from error
        return {
            "storage_id": STORAGE_ID,
            "path": path,
            "entries": [entry.__dict__ for entry in entries],
        }

    @router.post("/storage/references", response_model=StorageInputReference)
    def api_storage_reference(
        selection: StoragePathRequest,
        request: Request,
        _: ApiToken,
    ) -> StorageInputReference:
        try:
            return storage_file_reference(
                request.app.state.settings.storage_directory, selection.path
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error

    @router.post(
        "/storage/project-uploads",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    def api_storage_project(
        selection: StoragePathRequest,
        request: Request,
        _: ApiToken,
    ) -> UploadedProjectReference:
        settings = request.app.state.settings
        try:
            return package_storage_project(
                settings.storage_directory,
                selection.path,
                settings.upload_directory / "projects",
                settings.max_project_upload_bytes,
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error

    @router.get("/storage/files/{file_path:path}", response_class=FileResponse)
    def api_storage_file(
        file_path: str,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        try:
            target = resolve_storage_path(
                request.app.state.settings.storage_directory, file_path
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="storage path is not a regular file",
            )
        return FileResponse(target, media_type="application/octet-stream")

    @router.post(
        "/batch-submissions",
        response_model=JobGroupRead,
        status_code=status.HTTP_201_CREATED,
    )
    def api_batch_submission(
        submission: BatchSubmissionCreate,
        request: Request,
        job_service: JobServiceDependency,
        _: ApiToken,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ] = None,
    ) -> JobGroupRead:
        return create_batch_submission(
            request, job_service, submission, idempotency_key
        )

    @router.get("/datasets/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_dataset(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = request.app.state.settings.upload_directory / f"{upload_id}.csv"
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded dataset not found",
            )
        return FileResponse(
            target,
            media_type="text/csv",
            filename=f"{upload_id}.csv",
        )

    @router.get("/scripts/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_script(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = (
            request.app.state.settings.upload_directory / "scripts" / f"{upload_id}.py"
        )
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded script not found",
            )
        return FileResponse(
            target,
            media_type="text/x-python",
            filename=f"{upload_id}.py",
        )

    @router.get("/projects/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_project(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = (
            request.app.state.settings.upload_directory
            / "projects"
            / f"{upload_id}.zip"
        )
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded project not found",
            )
        return FileResponse(
            target,
            media_type="application/zip",
            filename=f"{upload_id}.zip",
        )

    @router.get("/inputs/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_input(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = (
            request.app.state.settings.upload_directory
            / "inputs"
            / f"{upload_id}.input"
        )
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded input not found",
            )
        return FileResponse(target, media_type="application/octet-stream")

    def artifact_directory_for(request: Request, job_id: UUID) -> Path:
        """Resolve a job's artifact directory, refusing to write to the wrong disk.

        On the Pi this lives on the external SSD. If that disk is absent the
        mount point is an ordinary directory on the small system card, so a
        write would silently fill the boot disk instead of failing. Comparing
        device IDs against `/` catches that regardless of how the path is
        arranged, which a path-shape check would not.
        """
        settings = request.app.state.settings
        root: Path = settings.artifact_directory
        if settings.artifact_requires_mount:
            probe = root if root.exists() else root.parent
            try:
                on_root_filesystem = probe.stat().st_dev == Path("/").stat().st_dev
            except OSError:
                on_root_filesystem = True
            if on_root_filesystem:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Artifact storage is not mounted",
                )
        return root / str(job_id)

    def directory_size(directory: Path) -> int:
        return sum(
            item.stat().st_size for item in directory.rglob("*") if item.is_file()
        )

    def evict_artifacts_over_cap(request: Request) -> list[str]:
        """Delete whole job directories until the store is back under its cap.

        Nothing expires because it is old. This is only a backstop against a
        runaway filling the disk, so it evicts the least recently touched jobs
        first and logs every removal loudly — losing results silently would be
        worse than running out of space.
        """
        settings = request.app.state.settings
        root: Path = settings.artifact_directory
        if not root.is_dir():
            return []
        jobs = sorted(
            (item for item in root.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
        )
        total = sum(directory_size(job) for job in jobs)
        evicted: list[str] = []
        for job in jobs:
            if total <= settings.max_artifact_store_bytes:
                break
            size = directory_size(job)
            shutil.rmtree(job, ignore_errors=True)
            total -= size
            evicted.append(job.name)
            logging.warning(
                "artifact store over %s bytes; evicted job=%s freeing %s bytes",
                settings.max_artifact_store_bytes,
                job.name,
                size,
            )
        return evicted

    def safe_artifact_name(filename: str) -> str:
        """Reject anything that is not a plain, self-contained file name.

        Artifact names come from a worker executing user-supplied code, so they
        are untrusted input used to build a path. Allow-list rather than strip:
        no separators, no traversal, no leading dot, no control characters.
        """
        name = (filename or "").strip()
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
            or ".." in name
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Artifact file name is not acceptable",
            )
        return name

    @router.post(
        "/jobs/{job_id}/artifacts",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_artifact(
        job_id: UUID,
        request: Request,
        job_service: JobServiceDependency,
        _: ApiToken,
        worker_id: Annotated[str, Form()],
        lease_token: Annotated[UUID, Form()],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        # Publishing results mutates a job's output, so it needs the same
        # authority as completing it: the caller must hold the current lease.
        try:
            job_service.authorize_lease(job_id, worker_id, lease_token)
        except JobNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error
        except JobTransitionError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

        settings = request.app.state.settings
        name = safe_artifact_name(file.filename or "")
        directory = artifact_directory_for(request, job_id)
        directory.mkdir(parents=True, exist_ok=True)

        used = sum(
            item.stat().st_size for item in directory.glob("*") if item.is_file()
        )
        target = directory / name
        temporary = directory / f".{uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_artifact_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                "Artifact exceeds the "
                                f"{settings.max_artifact_bytes} byte limit"
                            ),
                        )
                    if used + size > settings.max_job_artifact_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                "Job artifacts exceed the "
                                f"{settings.max_job_artifact_bytes} byte limit"
                            ),
                        )
                    digest.update(chunk)
                    output.write(chunk)
            os.replace(temporary, target)
        finally:
            await file.close()
            temporary.unlink(missing_ok=True)

        evicted = evict_artifacts_over_cap(request)

        return {
            "job_id": str(job_id),
            "filename": name,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "evicted_jobs": evicted,
        }

    @router.get("/jobs/{job_id}/artifacts")
    def list_artifacts(
        job_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> list[dict[str, Any]]:
        directory = artifact_directory_for(request, job_id)
        if not directory.is_dir():
            return []
        return sorted(
            (
                {"filename": item.name, "size_bytes": item.stat().st_size}
                for item in directory.iterdir()
                if item.is_file() and not item.name.startswith(".")
            ),
            key=lambda item: cast(str, item["filename"]),
        )

    @router.get("/jobs/{job_id}/artifacts/{filename}", response_class=FileResponse)
    def download_artifact(
        job_id: UUID,
        filename: str,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        name = safe_artifact_name(filename)
        target = artifact_directory_for(request, job_id) / name
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
            )
        return FileResponse(
            target, media_type="application/octet-stream", filename=name
        )

    @router.get("/jobs-ui/api/jobs/{job_id}/artifacts")
    def jobs_portal_list_artifacts(
        job_id: UUID,
        request: Request,
        identity: DashboardSession,
        job_service: JobServiceDependency,
    ) -> list[dict[str, Any]]:
        if job_service.get(job_id, owner_scope(identity)) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
            )
        directory = artifact_directory_for(request, job_id)
        if not directory.is_dir():
            return []
        return sorted(
            (
                {"filename": item.name, "size_bytes": item.stat().st_size}
                for item in directory.iterdir()
                if item.is_file() and not item.name.startswith(".")
            ),
            key=lambda item: cast(str, item["filename"]),
        )

    @router.get(
        "/jobs-ui/api/jobs/{job_id}/artifacts/{filename}",
        response_class=FileResponse,
    )
    def jobs_portal_download_artifact(
        job_id: UUID,
        filename: str,
        request: Request,
        identity: DashboardSession,
        job_service: JobServiceDependency,
    ) -> FileResponse:
        if job_service.get(job_id, owner_scope(identity)) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
            )
        name = safe_artifact_name(filename)
        target = artifact_directory_for(request, job_id) / name
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
            )
        return FileResponse(
            target, media_type="application/octet-stream", filename=name
        )

    @router.delete("/jobs/{job_id}/artifacts")
    def delete_artifacts(
        job_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> dict[str, Any]:
        """Delete every published file for a job.

        Deletion is deliberate and operator-driven: results are kept until you
        say otherwise. The job record itself is untouched, so its history,
        stdout and file names survive — only the bytes go.
        """
        directory = artifact_directory_for(request, job_id)
        if not directory.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No artifacts for this job",
            )
        removed = sorted(item.name for item in directory.iterdir() if item.is_file())
        freed = directory_size(directory)
        shutil.rmtree(directory, ignore_errors=True)
        logging.info(
            "artifacts deleted job=%s files=%d bytes=%d", job_id, len(removed), freed
        )
        return {"job_id": str(job_id), "deleted": removed, "freed_bytes": freed}

    @router.delete("/jobs/{job_id}/artifacts/{filename}")
    def delete_artifact(
        job_id: UUID,
        filename: str,
        request: Request,
        _: ApiToken,
    ) -> dict[str, Any]:
        name = safe_artifact_name(filename)
        target = artifact_directory_for(request, job_id) / name
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
            )
        freed = target.stat().st_size
        target.unlink()
        return {"job_id": str(job_id), "deleted": [name], "freed_bytes": freed}

    return router


def collect_system_metrics() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = os.getloadavg()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "logical_cores": psutil.cpu_count(logical=True),
            "load_1": load[0],
            "load_5": load[1],
            "load_15": load[2],
            "temperature_c": cpu_temperature(),
        },
        "memory": {
            "total": memory.total,
            "used": memory.used,
            "available": memory.available,
            "percent": memory.percent,
        },
        "storage": {
            "mount": "/",
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
        "uptime_seconds": uptime_seconds(),
    }


def collect_service_health(job_service: JobService) -> dict[str, Any]:
    try:
        job_service.ping()
        database_state = "ONLINE"
        database_detail = "SQLite ready"
    except sqlite3.Error:
        database_state = "OFFLINE"
        database_detail = "SQLite unavailable"

    mount_path = Path(
        os.environ.get("HOME_PLATFORM_NAS_MOUNT", "/srv/home-platform/storage")
    )
    mounted = mount_path.is_mount()
    try:
        usage = psutil.disk_usage(str(mount_path)) if mounted else None
    except OSError:
        mounted = False
        usage = None
    mount_identity = (
        command_output(
            ["findmnt", "--noheadings", "--output", "SOURCE,FSTYPE", str(mount_path)]
        )
        if mounted
        else None
    )
    service_name = os.environ.get("HOME_PLATFORM_NAS_SERVICE", "home-platform-nas")
    service_state = command_output(["systemctl", "is-active", service_name])
    smb_reachable = tcp_reachable("127.0.0.1", 445)
    if not mounted:
        nas_state = "OFFLINE"
    elif service_state == "active" and smb_reachable:
        nas_state = "ONLINE"
    else:
        nas_state = "DEGRADED"

    synology_host = os.environ.get("HOME_PLATFORM_SYNOLOGY_HOST", "").strip()
    if synology_host:
        synology_smb_reachable = tcp_reachable(synology_host, 445)
        synology_dsm_reachable = tcp_reachable(synology_host, 5001)
        synology_state = "ONLINE" if synology_smb_reachable else "OFFLINE"
    else:
        synology_smb_reachable = False
        synology_dsm_reachable = False
        synology_state = "UNKNOWN"

    return {
        "control_plane": {
            "state": "ONLINE",
            "version": VERSION,
            "detail": "FastAPI coordination service",
        },
        "database": {
            "state": database_state,
            "engine": "SQLite",
            "detail": database_detail,
        },
        "nas": {
            "state": nas_state,
            "mount": str(mount_path),
            "mounted": mounted,
            "mount_identity": mount_identity,
            "total": usage.total if usage is not None else None,
            "used": usage.used if usage is not None else None,
            "free": usage.free if usage is not None else None,
            "percent": usage.percent if usage is not None else None,
            "service": service_state or "unknown",
            "smb": "reachable" if smb_reachable else "unreachable",
        },
        "synology_nas": {
            "state": synology_state,
            "configured": bool(synology_host),
            "host": synology_host or None,
            "smb": "reachable" if synology_smb_reachable else "unreachable",
            "management": ("reachable" if synology_dsm_reachable else "unreachable"),
            "role": "migration target",
        },
    }


def command_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    return output or None


def tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def uptime_seconds() -> int:
    try:
        return max(0, int(time.time() - psutil.boot_time()))
    except OSError:
        return 0


def cpu_temperature() -> float | None:
    sensor_reader = getattr(psutil, "sensors_temperatures", None)
    if sensor_reader is None:
        return None
    try:
        temperatures = sensor_reader()
    except OSError:
        return None
    for readings in temperatures.values():
        if readings:
            return round(float(readings[0].current), 1)
    return None
