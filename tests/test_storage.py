import hashlib
import zipfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import (
    StoragePolicyError,
    member_storage_entries,
    member_storage_path,
    member_storage_path_allowed,
    resolve_storage_path,
)


def test_storage_routes_browse_reference_download_and_package_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "nas"
    project = storage / "projects" / "demo"
    inputs = storage / "inputs"
    project.mkdir(parents=True)
    inputs.mkdir()
    (project / "submit.hp").write_text(
        """#!/usr/bin/env bash
#HP --version 1
#HP --name "NAS array"
#HP --runtime scientific-python:1
#HP --cpus 1
#HP --memory-mb 512
#HP --time-limit 00:05:00
#HP --input dataset
#HP --array 1-2
echo "$HOME_PLATFORM_ARRAY_INDEX"
"""
    )
    content = b"sample,value\na,1\n"
    (inputs / "cohort.csv").write_bytes(content)
    monkeypatch.setenv("HOME_PLATFORM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        listing = client.get("/storage", params={"path": "inputs"})
        assert listing.status_code == 200
        assert listing.json()["entries"] == [
            {
                "name": "cohort.csv",
                "path": "inputs/cohort.csv",
                "kind": "file",
                "size_bytes": len(content),
            }
        ]

        reference = client.post(
            "/storage/references",
            json={"path": "inputs/cohort.csv"},
        )
        assert reference.status_code == 200
        assert reference.json() == {
            "storage_id": "home-storage",
            "path": "inputs/cohort.csv",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }

        downloaded = client.get("/storage/files/inputs/cohort.csv")
        assert downloaded.status_code == 200
        assert downloaded.content == content

        packaged = client.post(
            "/storage/project-uploads",
            json={"path": "projects/demo"},
        )
        assert packaged.status_code == 201
        archive = (
            tmp_path / "uploads" / "projects" / f"{packaged.json()['upload_id']}.zip"
        )
        with zipfile.ZipFile(archive) as bundle:
            assert bundle.namelist() == ["submit.hp"]

        client.post(
            "/workers/heartbeat",
            json={"worker_id": "worker-a", "supported_types": ["batch"]},
        )
        client.put(
            "/workers/worker-a/capacity",
            json={"max_job_cpu": 2, "max_job_memory_mb": 1024},
        )
        submitted = client.post(
            "/batch-submissions",
            json={
                "project": packaged.json(),
                "entrypoint": "submit.hp",
                "inputs": {"dataset": reference.json()},
            },
        )
        assert submitted.status_code == 201, submitted.text
        assert len(submitted.json()["tasks"]) == 2
        assert submitted.json()["tasks"][0]["parameters"]["inputs"]["dataset"] == (
            reference.json()
        )


def test_storage_resolution_rejects_escape_and_symlink(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "nas"
    storage.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (storage / "link").symlink_to(outside)

    with pytest.raises(StoragePolicyError, match=r"escapes|symbolic"):
        resolve_storage_path(storage, "link")
    with pytest.raises(StoragePolicyError, match="stay inside"):
        resolve_storage_path(storage, "../outside.txt")


def test_member_storage_maps_only_home_and_shared(tmp_path: Path) -> None:
    user_id = UUID("00000000-0000-0000-0000-000000000123")
    root = tmp_path / "nas"
    home = root / "users" / str(user_id)
    shared = root / "shared"
    home.mkdir(parents=True)
    shared.mkdir()
    (home / "private.txt").write_text("private")
    (shared / "common.txt").write_text("shared")

    assert member_storage_path(user_id, "Home/private.txt") == (
        f"users/{user_id}/private.txt"
    )
    assert member_storage_path(user_id, "Shared/common.txt") == "shared/common.txt"
    assert [entry.path for entry in member_storage_entries(root, user_id)] == [
        "Home",
        "Shared",
    ]
    assert [entry.path for entry in member_storage_entries(root, user_id, "Home")] == [
        "Home/private.txt"
    ]
    assert member_storage_path_allowed(user_id, f"users/{user_id}/private.txt")
    assert member_storage_path_allowed(user_id, "shared/common.txt")
    assert not member_storage_path_allowed(
        user_id, "users/00000000-0000-0000-0000-000000000999/private.txt"
    )
    with pytest.raises(StoragePolicyError, match="Home or Shared"):
        member_storage_path(user_id, "users/someone-else/private.txt")
