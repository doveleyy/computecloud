from types import SimpleNamespace

from app.dashboard import collect_service_health


class AvailableService:
    def ping(self) -> None:
        return None


def test_service_health_reports_mounted_healthy_nas(monkeypatch) -> None:
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


def test_service_health_reports_unmounted_nas_as_offline(monkeypatch) -> None:
    monkeypatch.setattr("app.dashboard.Path.is_mount", lambda _path: False)
    monkeypatch.setattr("app.dashboard.command_output", lambda _command: None)
    monkeypatch.setattr("app.dashboard.tcp_reachable", lambda _host, _port: False)

    services = collect_service_health(AvailableService())  # type: ignore[arg-type]

    assert services["nas"]["state"] == "OFFLINE"
    assert services["nas"]["mounted"] is False
    assert services["nas"]["percent"] is None
