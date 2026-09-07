import argparse
import json
import os
from pathlib import Path
from typing import Any

import httpx

from app.config import load_api_token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal home platform client")
    parser.add_argument(
        "--url",
        default=os.environ.get("HOME_PLATFORM_API_URL", "http://127.0.0.1:8000"),
        help="control-plane base URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="check control-plane health")
    subparsers.add_parser("list", help="list jobs")

    submit_parser = subparsers.add_parser("submit-sleep", help="submit a sleep job")
    submit_parser.add_argument("seconds", type=int)
    submit_parser.add_argument("--name", help="human-readable job name")
    submit_parser.add_argument(
        "--idempotency-key",
        help="reuse this key to retry a submission without creating another job",
    )

    dataset_parser = subparsers.add_parser(
        "submit-dataset-script",
        help="run a reviewed script against a content-addressed HTTPS dataset",
    )
    dataset_parser.add_argument("script", choices=["csv_summary"])
    dataset_parser.add_argument("url")
    dataset_parser.add_argument("sha256")
    dataset_parser.add_argument("size_bytes", type=int)
    dataset_parser.add_argument("--name", help="human-readable job name")
    dataset_parser.add_argument("--timeout-seconds", type=int, default=300)
    dataset_parser.add_argument(
        "--idempotency-key",
        help="reuse this key to retry a submission without creating another job",
    )

    batch_parser = subparsers.add_parser(
        "submit-python-batch",
        help="upload a Python script and CSV for isolated container execution",
    )
    batch_parser.add_argument("script", type=Path)
    batch_parser.add_argument("dataset", type=Path)
    batch_parser.add_argument("--name", required=True, help="human-readable job name")
    batch_parser.add_argument("--timeout-seconds", type=int, default=1800)
    batch_parser.add_argument("--cpus", type=float, default=2.0)
    batch_parser.add_argument("--memory-mb", type=int, default=2048)
    batch_parser.add_argument(
        "--idempotency-key",
        help="reuse this key to retry job creation without creating another job",
    )

    get_parser = subparsers.add_parser("get", help="retrieve one job")
    get_parser.add_argument("job_id")

    subparsers.add_parser("workers", help="list registered workers")

    enable_parser = subparsers.add_parser(
        "worker-enable", help="allow a worker to claim new jobs"
    )
    enable_parser.add_argument("worker_id")

    disable_parser = subparsers.add_parser(
        "worker-disable", help="stop assigning new jobs to a worker"
    )
    disable_parser.add_argument("worker_id")
    return parser


def request(
    method: str,
    url: str,
    *,
    token: str | None,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {"X-API-Token": token} if token else {}
    headers.update(extra_headers or {})
    response = httpx.request(method, url, headers=headers, json=body, timeout=10)
    response.raise_for_status()
    return response.json()


def upload_file(url: str, path: Path, *, token: str | None) -> Any:
    headers = {"X-API-Token": token} if token else {}
    with path.open("rb") as source:
        response = httpx.post(
            url,
            headers=headers,
            files={"file": (path.name, source)},
            timeout=30,
        )
    response.raise_for_status()
    return response.json()


def main() -> None:
    args = build_parser().parse_args()
    base_url = args.url.rstrip("/")
    token_file = Path(
        os.environ.get(
            "HOME_PLATFORM_API_TOKEN_FILE",
            Path.home() / ".config/home-platform/api-token",
        )
    )
    token = load_api_token(default_file=token_file)

    if args.command == "health":
        result = request("GET", f"{base_url}/health", token=token)
    elif args.command == "list":
        result = request("GET", f"{base_url}/jobs", token=token)
    elif args.command == "submit-sleep":
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "type": "sleep",
                "parameters": {"seconds": args.seconds},
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
    elif args.command == "submit-dataset-script":
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "type": "dataset_script",
                "parameters": {
                    "script": args.script,
                    "dataset": {
                        "url": args.url,
                        "sha256": args.sha256,
                        "size_bytes": args.size_bytes,
                    },
                    "timeout_seconds": args.timeout_seconds,
                },
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
    elif args.command == "submit-python-batch":
        script = upload_file(f"{base_url}/uploads/scripts", args.script, token=token)
        dataset = upload_file(f"{base_url}/uploads/datasets", args.dataset, token=token)
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "type": "python_batch",
                "parameters": {
                    "script": script,
                    "dataset": dataset,
                    "timeout_seconds": args.timeout_seconds,
                    "cpu_limit": args.cpus,
                    "memory_mb": args.memory_mb,
                },
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
    elif args.command == "workers":
        result = request("GET", f"{base_url}/workers", token=token)
    elif args.command in {"worker-enable", "worker-disable"}:
        result = request(
            "PATCH",
            f"{base_url}/workers/{args.worker_id}",
            token=token,
            body={"enabled": args.command == "worker-enable"},
        )
    else:
        result = request("GET", f"{base_url}/jobs/{args.job_id}", token=token)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
