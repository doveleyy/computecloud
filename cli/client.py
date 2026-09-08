import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from cli import render
from contracts.tokens import load_api_token

RETRY_HELP = "reuse this key to retry without creating a second job"

EPILOG = """\
detail:
  Every command above takes its own arguments. Run `%(prog)s COMMAND --help`
  for the full description of any one of them.

examples:
  %(prog)s --url https://pi.example.ts.net workers
  %(prog)s worker-enable mac-primary
  %(prog)s submit-sleep 5 --name "pipeline check"
  %(prog)s submit-python-batch train.py data.csv --name "Experiment 1"
  %(prog)s get 7dcf9099-4204-42d9-928e-b31929cb0a0e
  %(prog)s cancel 7dcf9099-4204-42d9-928e-b31929cb0a0e

configuration:
  --url defaults to http://127.0.0.1:8000, which is a LOCAL control plane.
  To talk to a remote one, pass --url or set HOME_PLATFORM_API_URL.
  Connection refused usually means this was forgotten.

  The API token is read from ~/.config/home-platform/api-token, or from
  HOME_PLATFORM_API_TOKEN_FILE. It is never taken as an argument, so it
  cannot leak into shell history or the process list.

if a submitted job stays QUEUED:
  Some worker must be ONLINE, enabled, and advertise that job type -- all
  three. Check with `%(prog)s workers`. Workers register disabled, so a
  freshly started one claims nothing until you enable it.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client",
        description=(
            "Submit and inspect jobs, and control which workers may claim them."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the raw API response instead of a readable summary",
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        default=os.environ.get("HOME_PLATFORM_API_URL", "http://127.0.0.1:8000"),
        help=(
            "control-plane base URL (env: HOME_PLATFORM_API_URL) [default: %(default)s]"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    subparsers.add_parser("health", help="check the control plane is alive")
    subparsers.add_parser("list", help="list all jobs, newest first")

    submit_parser = subparsers.add_parser(
        "submit-sleep",
        help="submit a do-nothing job, useful for proving the pipeline works",
    )
    submit_parser.add_argument(
        "seconds", type=int, help="how long the worker should sleep (1-30)"
    )
    submit_parser.add_argument("--name", help="human-readable job name, not unique")
    submit_parser.add_argument(
        "--worker", help="only this registered worker may claim the job"
    )
    submit_parser.add_argument("--idempotency-key", help=RETRY_HELP)

    dataset_parser = subparsers.add_parser(
        "submit-dataset-script",
        help="run a reviewed script against a dataset fetched by URL (legacy)",
        description=(
            "The worker downloads the dataset directly and verifies it against "
            "the digest and size you declare here, so state what you expect to "
            "receive. The URL host must be in that worker's allowlist."
        ),
    )
    dataset_parser.add_argument(
        "script", choices=["csv_summary"], help="which reviewed script to run"
    )
    dataset_parser.add_argument("url", help="HTTPS URL the worker will download")
    dataset_parser.add_argument("sha256", help="expected SHA-256 of the dataset")
    dataset_parser.add_argument(
        "size_bytes", type=int, help="expected size in bytes; a mismatch fails the job"
    )
    dataset_parser.add_argument("--name", help="human-readable job name, not unique")
    dataset_parser.add_argument(
        "--worker", help="only this registered worker may claim the job"
    )
    dataset_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="give up after this long [default: %(default)s, max 3600]",
    )
    dataset_parser.add_argument("--idempotency-key", help=RETRY_HELP)

    batch_parser = subparsers.add_parser(
        "submit-python-batch",
        help="run your own Python against a CSV in an isolated container",
        description=(
            "Uploads the script and the CSV, then creates a job referencing both "
            "by digest. The script runs in a fixed container with no network, a "
            "read-only root and no credentials. It reads the CSV path from "
            "HOME_PLATFORM_DATASET and writes results to HOME_PLATFORM_OUTPUT_DIR; "
            "whatever it leaves there is collected as artifacts. Only a worker "
            "that already has the container image will claim this. "
            "--cpus is a hard quota, so a fraction runs the job slowly and "
            "coolly rather than just deprioritising it; library thread pools "
            "are pinned to match, and HOME_PLATFORM_CPU_LIMIT is set so your "
            "script can size its own parallelism."
        ),
    )
    batch_parser.add_argument(
        "script", type=Path, help="path to a local .py file to run"
    )
    batch_parser.add_argument(
        "dataset", type=Path, help="path to a local .csv file to run it against"
    )
    batch_parser.add_argument(
        "--name", required=True, help="human-readable job name, not unique"
    )
    batch_parser.add_argument(
        "--worker",
        help="only this registered worker may claim the job; waits if unavailable",
    )
    batch_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="kill the container after this long [default: %(default)s, max 86400]",
    )
    batch_parser.add_argument(
        "--cpus",
        type=float,
        default=2.0,
        help=(
            "CPU cores to allow, 0.1-8. Fractions throttle deliberately: 0.5 runs "
            "at half a core to stay cool [default: %(default)s]"
        ),
    )
    batch_parser.add_argument(
        "--memory-mb",
        type=int,
        default=2048,
        help=(
            "hard memory limit in MiB, 256-16384; exceeding it fails the job "
            "as MEMORY_LIMIT_EXCEEDED [default: %(default)s]"
        ),
    )
    batch_parser.add_argument("--idempotency-key", help=RETRY_HELP)

    get_parser = subparsers.add_parser(
        "get", help="show one job in full, including its result"
    )
    get_parser.add_argument("job_id", help="job UUID, as printed by `list`")

    cancel_parser = subparsers.add_parser(
        "cancel",
        help="cancel a queued job or ask its worker to stop it",
        description=(
            "Queued jobs stop immediately. Running jobs stop cooperatively when "
            "the worker receives the request on its next lease heartbeat."
        ),
    )
    cancel_parser.add_argument("job_id", help="job UUID, as printed by `list`")

    subparsers.add_parser(
        "workers", help="list registered workers, their state and capabilities"
    )

    capacity_parser = subparsers.add_parser(
        "worker-capacity",
        help="set the largest single batch job a worker may accept",
        description=(
            "This is an admission and placement ceiling, not live telemetry. "
            "Targeted jobs cannot bypass it."
        ),
    )
    capacity_parser.add_argument("worker_id", help="worker ID, as printed by `workers`")
    capacity_parser.add_argument(
        "--cpus", type=float, required=True, help="maximum CPU quota, 0.1-8"
    )
    capacity_parser.add_argument(
        "--memory-mb",
        type=int,
        required=True,
        help="maximum container memory, 256-16384 MiB",
    )

    enable_parser = subparsers.add_parser(
        "worker-enable",
        help="allow a worker to claim new jobs",
        description=(
            "Changes durable state in the control plane. It does not start the "
            "worker process."
        ),
    )
    enable_parser.add_argument("worker_id", help="worker ID, as printed by `workers`")

    disable_parser = subparsers.add_parser(
        "worker-disable",
        help="stop giving a worker new jobs; it drains gracefully",
        description=(
            "The worker keeps heartbeating and finishes any job it already holds. "
            "This is a drain, not a cancel, and it does not stop the process."
        ),
    )
    disable_parser.add_argument("worker_id", help="worker ID, as printed by `workers`")

    artifacts_parser = subparsers.add_parser(
        "artifacts", help="list the files a job produced"
    )
    artifacts_parser.add_argument("job_id", help="job UUID, as printed by `list`")

    download_parser = subparsers.add_parser(
        "download",
        help="download one of a job's files",
        description="Saves into the current directory unless --output is given.",
    )
    download_parser.add_argument("job_id", help="job UUID, as printed by `list`")
    download_parser.add_argument(
        "filename", help="file name, as printed by `artifacts`"
    )
    download_parser.add_argument(
        "--output", type=Path, help="where to write it [default: ./<filename>]"
    )

    delete_parser = subparsers.add_parser(
        "delete",
        help="delete a job's published files",
        description=(
            "Removes the stored files. The job record itself is kept, so its "
            "history, output and file names remain visible. Nothing expires on "
            "its own, so this is how results are removed."
        ),
    )
    delete_parser.add_argument("job_id", help="job UUID, as printed by `list`")
    delete_parser.add_argument(
        "filename",
        nargs="?",
        help="one file to delete; omit to delete all of the job's files",
    )

    parser.epilog = _signatures(subparsers) + "\n" + (parser.epilog or "")
    return parser


def _signatures(subparsers: "argparse._SubParsersAction[Any]") -> str:
    """Render every command's argument signature for the top-level help.

    argparse only shows arguments one level down, so `--help` lists command
    names with no hint that each takes parameters, or that a second level of
    help exists. Generating this from the parsers themselves means it cannot
    drift out of step with the actual arguments.
    """
    lines = ["commands and their arguments:"]
    for name, sub in subparsers.choices.items():
        usage = " ".join(sub.format_usage().replace("usage:", "").split())
        usage = usage.replace(f"{sub.prog} ", "", 1).replace("[-h]", "").strip()
        lines.append(f"  {name} {usage}".rstrip())
    return "\n".join(lines) + "\n"


def download_artifact(
    base_url: str, job_id: str, filename: str, target: Path, *, token: str | None
) -> Path:
    headers = {"X-API-Token": token} if token else {}
    with httpx.stream(
        "GET",
        f"{base_url}/jobs/{job_id}/artifacts/{filename}",
        headers=headers,
        timeout=httpx.Timeout(30, read=300),
    ) as response:
        response.raise_for_status()
        with target.open("wb") as output:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                output.write(chunk)
    return target


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
    renderer: Callable[[Any], str] | None = None

    if args.command == "health":
        result = request("GET", f"{base_url}/health", token=token)
        renderer = render.status
    elif args.command == "list":
        result = request("GET", f"{base_url}/jobs", token=token)
        renderer = render.jobs
    elif args.command == "submit-sleep":
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "target_worker_id": args.worker,
                "type": "sleep",
                "parameters": {"seconds": args.seconds},
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
        renderer = render.submitted
    elif args.command == "submit-dataset-script":
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "target_worker_id": args.worker,
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
        renderer = render.submitted
    elif args.command == "submit-python-batch":
        script = upload_file(f"{base_url}/uploads/scripts", args.script, token=token)
        dataset = upload_file(f"{base_url}/uploads/datasets", args.dataset, token=token)
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "target_worker_id": args.worker,
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
        renderer = render.submitted
    elif args.command == "workers":
        result = request("GET", f"{base_url}/workers", token=token)
        renderer = render.workers
    elif args.command == "cancel":
        result = request("POST", f"{base_url}/jobs/{args.job_id}/cancel", token=token)
        renderer = render.cancelled
    elif args.command == "worker-capacity":
        result = request(
            "PUT",
            f"{base_url}/workers/{args.worker_id}/capacity",
            token=token,
            body={
                "max_job_cpu": args.cpus,
                "max_job_memory_mb": args.memory_mb,
            },
        )
        renderer = render.worker_updated
    elif args.command in {"worker-enable", "worker-disable"}:
        result = request(
            "PATCH",
            f"{base_url}/workers/{args.worker_id}",
            token=token,
            body={"enabled": args.command == "worker-enable"},
        )
        renderer = render.worker_updated
    elif args.command == "artifacts":
        result = request("GET", f"{base_url}/jobs/{args.job_id}/artifacts", token=token)
        renderer = render.artifacts
    elif args.command == "download":
        target = args.output or Path(args.filename)
        written = download_artifact(
            base_url, args.job_id, args.filename, target, token=token
        )
        result = {
            "filename": args.filename,
            "path": str(written.resolve()),
            "size_bytes": written.stat().st_size,
        }
        renderer = lambda payload: (  # noqa: E731
            f"{render.GREEN}Downloaded{render.RESET} {payload['path']} "
            f"({payload['size_bytes'] / 1024:.1f} KiB)"
        )
    elif args.command == "delete":
        path = f"{base_url}/jobs/{args.job_id}/artifacts"
        if args.filename:
            path = f"{path}/{args.filename}"
        result = request("DELETE", path, token=token)
        renderer = render.deleted
    else:
        result = request("GET", f"{base_url}/jobs/{args.job_id}", token=token)
        renderer = render.job

    if args.json or renderer is None:
        print(json.dumps(result, indent=2))
    else:
        print(renderer(result))


def run() -> None:
    """Entry point that reports API and network errors as messages, not tracebacks.

    A 404 or a refused connection is an ordinary outcome of using a command-line
    tool, not a bug worth a stack trace. The exit code still distinguishes
    failure so scripts can branch on it.
    """
    try:
        main()
    except httpx.HTTPStatusError as error:
        detail = ""
        try:
            detail = error.response.json().get("detail", "")
        except Exception:
            detail = error.response.text[:200]
        code = error.response.status_code
        hint = {
            401: "  check the API token file is present and readable",
            404: "  check the ID with `client list`",
            409: "  the lease or idempotency key conflicts with existing state",
            413: "  the file exceeds the configured size limit",
        }.get(code, "")
        print(f"{render.RED}error {code}{render.RESET}: {detail}", file=sys.stderr)
        if hint:
            print(f"{render.DIM}{hint}{render.RESET}", file=sys.stderr)
        raise SystemExit(1) from None
    except httpx.RequestError as error:
        print(
            f"{render.RED}cannot reach the control plane{render.RESET}: {error}",
            file=sys.stderr,
        )
        print(
            f"{render.DIM}  is --url correct? it defaults to localhost{render.RESET}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    run()
