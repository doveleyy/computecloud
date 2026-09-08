"""Smoke tests for the CLI.

These exist because a runtime crash in the argument parser once passed ruff,
strict mypy, and the whole suite: `argparse._SubParsersAction[Any]` type-checks
but is not subscriptable at runtime. Static analysis cannot catch that; building
the parser can.
"""

import pytest

from cli import render
from cli.client import build_parser


def test_parser_builds_and_documents_every_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    # Building the parser at all is the point: this is what crashed before.
    assert "commands and their arguments:" in help_text

    # Every command must appear with its arguments, not just its name.
    assert "submit-python-batch --name NAME" in help_text
    assert "script dataset" in help_text
    assert "download [--output OUTPUT] job_id filename" in help_text

    # Argument-less commands should not leak argparse's own flag.
    assert "health [-h]" not in help_text
    assert "\n  health\n" in help_text


def test_every_subcommand_parses_and_has_its_own_help() -> None:
    parser = build_parser()
    invocations = [
        ["health"],
        ["list"],
        ["workers"],
        ["get", "job-id"],
        ["cancel", "job-id"],
        ["artifacts", "job-id"],
        ["download", "job-id", "model.joblib"],
        ["worker-enable", "mac-primary"],
        ["worker-disable", "mac-primary"],
        [
            "worker-capacity",
            "mac-primary",
            "--cpus",
            "4",
            "--memory-mb",
            "8192",
        ],
        ["submit-sleep", "5"],
        ["submit-sleep", "5", "--worker", "windows-primary"],
        ["submit-python-batch", "a.py", "b.csv", "--name", "run"],
    ]
    for argv in invocations:
        parsed = parser.parse_args(argv)
        assert parsed.command == argv[0]
        assert parsed.json is False


def test_json_flag_and_url_default() -> None:
    parser = build_parser()
    assert parser.parse_args(["--json", "list"]).json is True
    assert parser.parse_args(["list"]).url.startswith("http")


def test_missing_required_argument_is_rejected() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        # --name is required for python_batch
        parser.parse_args(["submit-python-batch", "a.py", "b.csv"])


def test_renderers_survive_realistic_payloads() -> None:
    assert "mac-primary" in render.workers(
        [
            {
                "id": "mac-primary",
                "enabled": True,
                "state": "ONLINE",
                "supported_types": ["sleep"],
                "last_seen": "2026-09-07T06:00:00Z",
                "metrics": {"cpu_percent": 12.0, "memory_percent": 50.0},
            }
        ]
    )
    # A worker that has never reported metrics must not crash the table.
    assert "unknown-worker" in render.workers(
        [
            {
                "id": "unknown-worker",
                "enabled": False,
                "state": "STALE",
                "supported_types": [],
                "last_seen": None,
                "metrics": None,
            }
        ]
    )
    assert "(none)" in render.jobs([])
    assert "COMPLETED" in render.job(
        {
            "id": "abc",
            "name": None,
            "type": "sleep",
            "status": "COMPLETED",
            "worker_id": "mac-primary",
            "created_at": "2026-09-07T06:00:00Z",
            "attempt": 1,
            "max_attempts": 3,
            "result": {"slept_seconds": 1, "stdout": "hello\n"},
        }
    )
