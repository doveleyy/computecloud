import os
from dataclasses import dataclass
from pathlib import Path

from contracts.tokens import load_api_token


@dataclass(frozen=True)
class Settings:
    database_path: Path
    api_token: str | None
    lease_seconds: int
    worker_stale_seconds: int
    recovery_interval_seconds: float
    max_attempts: int
    upload_directory: Path
    max_upload_bytes: int
    max_script_upload_bytes: int
    max_project_upload_bytes: int
    artifact_directory: Path
    max_artifact_bytes: int
    max_job_artifact_bytes: int
    max_artifact_store_bytes: int
    artifact_requires_mount: bool
    storage_directory: Path
    power_request_directory: Path | None


def load_settings() -> Settings:
    database_path = Path(
        os.environ.get("HOME_PLATFORM_DB_PATH", "data/home-platform.db")
    )
    api_token_file = os.environ.get("HOME_PLATFORM_API_TOKEN_FILE")
    api_token = load_api_token(
        default_file=Path(api_token_file) if api_token_file else None,
        required=api_token_file is not None,
    )
    lease_seconds = positive_int("HOME_PLATFORM_LEASE_SECONDS", 15)
    worker_stale_seconds = positive_int("HOME_PLATFORM_WORKER_STALE_SECONDS", 20)
    recovery_interval_seconds = positive_float(
        "HOME_PLATFORM_RECOVERY_INTERVAL_SECONDS", 2.0
    )
    max_attempts = positive_int("HOME_PLATFORM_MAX_ATTEMPTS", 3)
    upload_directory = Path(os.environ.get("HOME_PLATFORM_UPLOAD_DIR", "data/uploads"))
    max_upload_bytes = positive_int("HOME_PLATFORM_MAX_UPLOAD_BYTES", 10 * 1024**2)
    max_script_upload_bytes = positive_int(
        "HOME_PLATFORM_MAX_SCRIPT_UPLOAD_BYTES", 256 * 1024
    )
    max_project_upload_bytes = positive_int(
        "HOME_PLATFORM_MAX_PROJECT_UPLOAD_BYTES", 20 * 1024**2
    )
    artifact_directory = Path(
        os.environ.get("HOME_PLATFORM_ARTIFACT_DIR", "data/artifacts")
    )
    max_artifact_bytes = positive_int("HOME_PLATFORM_MAX_ARTIFACT_BYTES", 100 * 1024**2)
    max_job_artifact_bytes = positive_int(
        "HOME_PLATFORM_MAX_JOB_ARTIFACT_BYTES", 512 * 1024**2
    )
    # A backstop, not a policy. Nothing expires because it is old — results are
    # kept until you delete them. This only stops a runaway from filling the
    # disk, by evicting the least recently touched jobs once the store exceeds
    # the cap. Set it generously: eviction is a failure mode, not routine.
    max_artifact_store_bytes = positive_int(
        "HOME_PLATFORM_MAX_ARTIFACT_STORE_BYTES", 50 * 1024**3
    )
    # On the Pi the artifact directory lives on the external SSD. If that disk is
    # absent the mount point is an ordinary directory on the small system card,
    # and writes would silently fill the boot disk. Set this so the API refuses
    # to write unless the intended filesystem is actually mounted.
    artifact_requires_mount = os.environ.get(
        "HOME_PLATFORM_ARTIFACT_REQUIRE_MOUNT", ""
    ).strip().lower() in {"1", "true", "yes"}
    storage_directory = Path(
        os.environ.get("HOME_PLATFORM_STORAGE_DIR", "/srv/home-platform/storage/nas")
    )
    power_request_value = os.environ.get("HOME_PLATFORM_POWER_REQUEST_DIR", "").strip()
    power_request_directory = Path(power_request_value) if power_request_value else None
    return Settings(
        database_path=database_path,
        api_token=api_token,
        lease_seconds=lease_seconds,
        worker_stale_seconds=worker_stale_seconds,
        recovery_interval_seconds=recovery_interval_seconds,
        max_attempts=max_attempts,
        upload_directory=upload_directory,
        max_upload_bytes=max_upload_bytes,
        max_script_upload_bytes=max_script_upload_bytes,
        max_project_upload_bytes=max_project_upload_bytes,
        artifact_directory=artifact_directory,
        max_artifact_bytes=max_artifact_bytes,
        max_job_artifact_bytes=max_job_artifact_bytes,
        max_artifact_store_bytes=max_artifact_store_bytes,
        artifact_requires_mount=artifact_requires_mount,
        storage_directory=storage_directory,
        power_request_directory=power_request_directory,
    )


def positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def positive_float(name: str, default: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value
