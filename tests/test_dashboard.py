from types import SimpleNamespace

from app.dashboard import collect_service_health


class AvailableService:
    def ping(self) -> None:
        return None


def test_service_health_reports_mounted_healthy_nas(monkeypatch) -> None:
    monkeypatch.delenv("HOME_PLATFORM_SYNOLOGY_HOST", raising=False)
    monkeypatch.setattr("app.dashboard.Path.is_mount", lambda _path: True)
    monkeypatch.setattr(
        "app.dashboard.psutil.disk_usage",
        lambda _path: SimpleNamespace(
            total=1_000_000,
            used=250_000,
            free=750_000,
            percent=25.0,
        ),
    )

    def command_output(command: list[str]) -> str | None:
        if command[0] == "findmnt":
            return "/dev/sda1 ext4"
        if command[0] == "systemctl":
            return "active"
        return None

    monkeypatch.setattr("app.dashboard.command_output", command_output)
    monkeypatch.setattr("app.dashboard.tcp_reachable", lambda _host, _port: True)

    services = collect_service_health(AvailableService())  # type: ignore[arg-type]

    assert services["control_plane"]["state"] == "ONLINE"
    assert services["database"]["state"] == "ONLINE"
    assert services["nas"] == {
        "state": "ONLINE",
        "mount": "/srv/home-platform/storage",
        "mounted": True,
        "mount_identity": "/dev/sda1 ext4",
        "total": 1_000_000,
        "used": 250_000,
        "free": 750_000,
        "percent": 25.0,
        "service": "active",
        "smb": "reachable",
    }
    assert services["synology_nas"] == {
        "state": "UNKNOWN",
        "configured": False,
        "host": None,
        "smb": "unreachable",
        "management": "unreachable",
        "role": "migration target",
    }


def test_service_health_reports_unmounted_nas_as_offline(monkeypatch) -> None:
    monkeypatch.delenv("HOME_PLATFORM_SYNOLOGY_HOST", raising=False)
    monkeypatch.setattr("app.dashboard.Path.is_mount", lambda _path: False)
    monkeypatch.setattr("app.dashboard.command_output", lambda _command: None)
    monkeypatch.setattr("app.dashboard.tcp_reachable", lambda _host, _port: False)

    services = collect_service_health(AvailableService())  # type: ignore[arg-type]

    assert services["nas"]["state"] == "OFFLINE"
    assert services["nas"]["mounted"] is False
    assert services["nas"]["percent"] is None


def test_service_health_reports_synology_smb_independently(monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_SYNOLOGY_HOST", "storage.example.internal")
    monkeypatch.setattr("app.dashboard.Path.is_mount", lambda _path: False)
    monkeypatch.setattr("app.dashboard.command_output", lambda _command: None)
    monkeypatch.setattr(
        "app.dashboard.tcp_reachable",
        lambda host, port: host == "storage.example.internal" and port in {445, 5001},
    )

    services = collect_service_health(AvailableService())  # type: ignore[arg-type]

    assert services["nas"]["state"] == "OFFLINE"
    assert services["synology_nas"] == {
        "state": "ONLINE",
        "configured": True,
        "host": "storage.example.internal",
        "smb": "reachable",
        "management": "reachable",
        "role": "migration target",
    }
