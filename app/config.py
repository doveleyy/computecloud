import os
from dataclasses import dataclass
from pathlib import Path


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


def load_api_token(
    default_file: Path | None = None,
    *,
    required: bool = False,
    explicit_file: Path | None = None,
) -> str | None:
    token = (
        None
        if explicit_file is not None
        else os.environ.get("HOME_PLATFORM_API_TOKEN") or None
    )
    configured_file = os.environ.get("HOME_PLATFORM_API_TOKEN_FILE")
    token_file = (
        explicit_file
        if explicit_file is not None
        else Path(configured_file)
        if configured_file
        else default_file
    )
    if token is None and token_file is not None:
        try:
            token = token_file.read_text().strip() or None
        except FileNotFoundError:
            if required:
                raise RuntimeError(
                    f"API token file does not exist: {token_file}"
                ) from None
    if required and token is None:
        raise RuntimeError("API token is required but empty")
    return token
