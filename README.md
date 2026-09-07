# Home Platform

A small distributed job system for a home network. One always-on coordinator
owns job state; laptops join as compute workers when they happen to be
available, and queued work waits when none are.

Built to explore the parts of distributed systems that are easy to describe and
hard to get right: atomic work claiming, lease-based failure recovery, verifying
data you did not produce, and running untrusted code without trusting it.

## What it does

- **Durable job queue** — SQLite is the single source of truth. Workers mutate
  state only through the API.
- **Lease-based recovery** — a claimed job carries a renewable lease. If a
  worker sleeps or dies, the job is requeued automatically, bounded by an
  attempt limit so a poisonous job fails instead of looping forever.
- **Atomic claiming** — a single conditional update means exactly one worker
  wins a job, even under contention.
- **Content-addressed data plane** — large datasets go straight to the worker
  that needs them, verified by size and SHA-256 and cached by digest. They never
  travel through the coordinator or sit in job rows.
- **Isolated execution** — user-supplied Python runs in a fixed, pre-built
  container with no network, a read-only root, dropped capabilities, a non-root
  user, and CPU/memory/PID/time limits. The host agent never imports it.
- **Result publishing** — a worker uploads its output files to the coordinator
  under the same lease that authorises completion, so a revived worker cannot
  overwrite its replacement's results. Files are then downloadable and can be
  exposed read-only to a file share.
- **Deliberate throttling** — `cpu_limit` is a hard quota, not a priority, and
  accepts fractions. A long search at 0.5 CPU runs slowly and coolly on a laptop
  you are still using. Library thread pools are pinned to the quota, without
  which a throttled job oversubscribes its own limit and runs several times
  slower for the same CPU budget.
- **Operator control** — workers register scheduling-disabled and are enabled
  deliberately, from a web dashboard or the CLI. Disabling drains gracefully
  rather than cancelling running work.

## Documentation

- [Architecture](docs/architecture.md) — design, job lifecycle, leases,
  scheduling behaviour, isolation model, trust boundaries, failure behaviour.
- [Job and API contract](docs/job-contract.md) — job types, state machine,
  worker protocol, endpoints, authentication.

## Layout

```text
contracts/   the shared wire contract — the only code both sides import
app/         control plane: HTTP API, persistence, migrations, web interfaces
worker/      worker agent: claiming, telemetry, dataset cache, container launcher
cli/         operator client
containers/  pinned container image definition for batch execution
examples/    end-to-end training examples, including a throttled grid search
tests/       test suite
```

`contracts/` exists so a worker deployment does not drag control-plane code
along with it. Nothing in `app/` imports `worker/`, and neither `worker/` nor
`cli/` imports `app/`.

## Development

```bash
pixi run dev      # API on localhost, interactive docs at /docs
pixi run check    # lint, format, strict type check, tests
```

`/health` reports process liveness; `/ready` also verifies the database is
reachable.

## Running a worker

```bash
HOME_PLATFORM_API_URL=<control-plane-url> \
HOME_PLATFORM_WORKER_ID=<worker-name> \
pixi run -e worker worker
```

A worker advertises the batch capability only once it can see the pre-built
container image, re-checking periodically — so capability appears and
disappears on its own, without restarts.

## Client

```bash
export HOME_PLATFORM_API_URL=<control-plane-url>

pixi run client workers                       # readable table; --json to pipe
pixi run client worker-enable <worker-name>
pixi run client submit-python-batch script.py data.csv --name "Training run" \
  --cpus 0.5 --timeout-seconds 86400        # slow, cool, overnight
pixi run client list
pixi run client artifacts <job-id>            # what the job produced
pixi run client download <job-id> model.joblib
pixi run client delete <job-id>               # remove its files; job record stays
```

Output is human-readable by default and raw JSON behind `--json`.
`pixi run client --help` lists every command with its arguments.

## Status and scope

A working personal system, not a product. It runs a single coordinator with a
single database writer and is deliberately not highly available.

Known gaps, in the order they will start to matter: placement is a race between
eligible workers rather than a real scheduler; the worker's content-addressed
caches still grow without bound; and results pass through the coordinator
instead of going directly to storage. See [Architecture](docs/architecture.md) for why
each is currently adequate and when it stops being so.

Deployment specifics — hosts, addresses, accounts, machine inventory, and
operational runbooks — are intentionally kept out of this repository.
