from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from app.batch_script import (
    MAX_EXPANDED_PROJECT_BYTES,
    MAX_PROJECT_FILES,
    BatchScriptError,
    validate_project_archive,
)
from contracts.models import StorageInputReference, UploadedProjectReference

STORAGE_ID = "home-storage"


class StoragePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class StorageEntry:
    name: str
    path: str
    kind: str
    size_bytes: int | None


def resolve_storage_path(root: Path, relative_path: str) -> Path:
    normalized = relative_path.strip().replace("\\", "/")
    relative = PurePosixPath(normalized or ".")
    if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
        raise StoragePolicyError("storage path must stay inside HomeStorage")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = resolved_root.joinpath(*relative.parts).resolve(strict=True)
    except OSError as error:
        raise StoragePolicyError("storage path does not exist") from error
    if not candidate.is_relative_to(resolved_root):
        raise StoragePolicyError("storage path escapes HomeStorage")
    current = resolved_root
    for part in relative.parts:
        if part == ".":
            continue
        current /= part
        if current.is_symlink():
            raise StoragePolicyError("storage paths cannot contain symbolic links")
    return candidate


def browse_storage(root: Path, relative_path: str = "") -> list[StorageEntry]:
    directory = resolve_storage_path(root, relative_path)
    if not directory.is_dir():
        raise StoragePolicyError("storage path is not a directory")
    prefix = PurePosixPath(relative_path.strip().replace("\\", "/"))
    if str(prefix) == ".":
        prefix = PurePosixPath()
    entries: list[StorageEntry] = []
    for item in sorted(
        directory.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower())
    ):
        if item.is_symlink() or item.name.startswith("."):
            continue
        path = (prefix / item.name).as_posix()
        if item.is_dir():
            entries.append(StorageEntry(item.name, path, "directory", None))
        elif item.is_file():
            entries.append(StorageEntry(item.name, path, "file", item.stat().st_size))
    return entries


def member_storage_path(user_id: UUID, logical_path: str) -> str:
    """Map member-facing Home/Shared paths to stable provider paths."""
    normalized = logical_path.strip().replace("\\", "/").strip("/")
    if not normalized:
        raise StoragePolicyError("choose Home or Shared")
    path = PurePosixPath(normalized)
    if any(part in {"..", ""} for part in path.parts):
        raise StoragePolicyError("storage path must stay inside Home or Shared")
    area, *remainder = path.parts
    if area.lower() == "home":
        physical = PurePosixPath("users", str(user_id), *remainder)
    elif area.lower() == "shared":
        physical = PurePosixPath("shared", *remainder)
    else:
        raise StoragePolicyError("member storage paths must start with Home or Shared")
    return physical.as_posix()


def member_storage_entries(
    root: Path, user_id: UUID, logical_path: str = ""
) -> list[StorageEntry]:
    normalized = logical_path.strip().replace("\\", "/").strip("/")
    if not normalized:
        return [
            StorageEntry("Home", "Home", "directory", None),
            StorageEntry("Shared", "Shared", "directory", None),
        ]
    physical_path = member_storage_path(user_id, normalized)
    entries = browse_storage(root, physical_path)
    physical_prefix = PurePosixPath(physical_path)
    logical_prefix = PurePosixPath(normalized)
    return [
        StorageEntry(
            entry.name,
            (
                logical_prefix / PurePosixPath(entry.path).relative_to(physical_prefix)
            ).as_posix(),
            entry.kind,
            entry.size_bytes,
        )
        for entry in entries
    ]


def member_storage_path_allowed(user_id: UUID, physical_path: str) -> bool:
    normalized = PurePosixPath(physical_path.strip().replace("\\", "/"))
    parts = normalized.parts
    return (len(parts) >= 2 and parts[:2] == ("users", str(user_id))) or (
        len(parts) >= 1 and parts[0] == "shared"
    )


def storage_file_reference(root: Path, relative_path: str) -> StorageInputReference:
    path = resolve_storage_path(root, relative_path)
    if not path.is_file():
        raise StoragePolicyError("storage input must be a regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size == 0:
        raise StoragePolicyError("storage input cannot be empty")
    return StorageInputReference(
        storage_id=STORAGE_ID,
        path=PurePosixPath(relative_path).as_posix(),
        sha256=digest.hexdigest(),
        size_bytes=size,
    )


def package_storage_project(
    root: Path,
    relative_path: str,
    upload_directory: Path,
    max_archive_bytes: int,
) -> UploadedProjectReference:
    source = resolve_storage_path(root, relative_path)
    if not source.is_dir():
        raise StoragePolicyError("project path must be a directory")
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise StoragePolicyError("project directory cannot be empty")
    if len(files) > MAX_PROJECT_FILES:
        raise StoragePolicyError(
            f"project contains more than {MAX_PROJECT_FILES} files"
        )
    if any(path.is_symlink() for path in source.rglob("*")):
        raise StoragePolicyError("project directory cannot contain symbolic links")
    expanded = sum(path.stat().st_size for path in files)
    if expanded > MAX_EXPANDED_PROJECT_BYTES:
        raise StoragePolicyError("expanded project exceeds the 100 MiB safety limit")

    upload_id: UUID = uuid4()
    upload_directory.mkdir(parents=True, exist_ok=True)
    target = upload_directory / f"{upload_id}.zip"
    temporary = upload_directory / f".{upload_id}.part"
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in files:
                archive.write(path, path.relative_to(source).as_posix())
        size = temporary.stat().st_size
        if size > max_archive_bytes:
            raise StoragePolicyError(
                f"compressed project exceeds the {max_archive_bytes // 1024} KB limit"
            )
        validate_project_archive(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, target)
    except (BatchScriptError, OSError, zipfile.BadZipFile) as error:
        if isinstance(error, StoragePolicyError):
            raise
        raise StoragePolicyError(str(error)) from error
    finally:
        temporary.unlink(missing_ok=True)
    return UploadedProjectReference(
        upload_id=upload_id,
        sha256=digest,
        size_bytes=size,
    )
