import hashlib
import hmac
import os
import platform
import re
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

from app.service import (
    IdempotencyConflictError,
    JobNotFoundError,
    JobService,
    JobTransitionError,
    WorkerNotFoundError,
)
from app.version import VERSION
from contracts.models import (
    JobCreate,
    JobRead,
    UploadedDatasetReference,
    UploadedScriptReference,
    WorkerRead,
    WorkerUpdate,
)

SESSION_COOKIE = "home_platform_dashboard"
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text()
JOBS_HTML = Path(__file__).with_name("jobs.html").read_text()


class DashboardLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


def create_dashboard_router() -> APIRouter:
    router = APIRouter()
    anonymous_session = token_urlsafe(32)

    def session_value(request: Request) -> str:
        token = request.app.state.settings.api_token
        if token is None:
            return anonymous_session
        return hmac.new(
            token.encode(), b"home-platform-dashboard-v1", hashlib.sha256
        ).hexdigest()

    def require_dashboard_session(
        request: Request,
        supplied: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> None:
        if supplied is None or not compare_digest(supplied, session_value(request)):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Dashboard login required",
            )

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

    DashboardSession = Annotated[None, Depends(require_dashboard_session)]
    ApiToken = Annotated[None, Depends(require_api_token)]

    def get_job_service(request: Request) -> JobService:
        return cast(JobService, request.app.state.job_service)

    JobServiceDependency = Annotated[JobService, Depends(get_job_service)]

    async def save_upload(
        file: UploadFile,
        *,
        directory: Path,
        suffix: str,
        max_bytes: int,
        label: str,
    ) -> tuple[UUID, str, int]:
        filename = file.filename or ""
        if Path(filename).suffix.lower() != suffix:
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

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    @router.post("/dashboard/login", status_code=status.HTTP_204_NO_CONTENT)
    def login(
        credentials: DashboardLogin, request: Request, response: Response
    ) -> None:
        expected = request.app.state.settings.api_token
        valid = expected is None or compare_digest(credentials.token, expected)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API token",
            )
        response.set_cookie(
            SESSION_COOKIE,
            session_value(request),
            httponly=True,
            samesite="strict",
            max_age=30 * 24 * 60 * 60,
            path="/",
        )

    @router.post("/dashboard/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    @router.get("/jobs-ui", response_class=HTMLResponse)
    def jobs_page() -> str:
        return JOBS_HTML

    @router.get("/dashboard/api/system")
    def system_metrics(_: DashboardSession) -> dict[str, Any]:
        return collect_system_metrics()

    @router.get("/dashboard/api/services")
    def dashboard_services(
        job_service: JobServiceDependency,
        _: DashboardSession,
    ) -> dict[str, Any]:
        return collect_service_health(job_service)

    @router.get("/dashboard/api/workers", response_model=list[WorkerRead])
    def dashboard_workers(
        job_service: JobServiceDependency,
        _: DashboardSession,
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
        _: DashboardSession,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_enabled(worker_id, update.enabled)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.get("/dashboard/api/jobs", response_model=list[JobRead])
    def dashboard_jobs(
        job_service: JobServiceDependency,
        _: DashboardSession,
    ) -> list[JobRead]:
        return job_service.list()

    @router.get("/jobs-ui/api/jobs", response_model=list[JobRead])
    def jobs_portal_list(
        job_service: JobServiceDependency,
        _: DashboardSession,
    ) -> list[JobRead]:
        return job_service.list()

    @router.post(
        "/jobs-ui/api/jobs",
        response_model=JobRead,
        status_code=status.HTTP_201_CREATED,
    )
    def jobs_portal_create(
        job_create: JobCreate,
        job_service: JobServiceDependency,
        _: DashboardSession,
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

    @router.post(
        "/jobs-ui/api/uploads",
        response_model=UploadedDatasetReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_upload(
        request: Request,
        _: DashboardSession,
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
        _: DashboardSession,
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
            upload_id=upload_id,
            sha256=digest,
            size_bytes=size,
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

        return {
            "job_id": str(job_id),
            "filename": name,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
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
