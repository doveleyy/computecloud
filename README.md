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
app/         control plane: HTTP API, persistence, migrations, web interfaces
worker/      worker agent: claiming, telemetry, dataset cache, container launcher
containers/  pinned container image definition for batch execution
examples/    end-to-end training example
tests/       test suite
```

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
pixi run client --url <control-plane-url> workers
pixi run client --url <control-plane-url> worker-enable <worker-name>
pixi run client --url <control-plane-url> submit-python-batch script.py data.csv \
  --name "Training run"
```

## Status and scope

A working personal system, not a product. It runs a single coordinator with a
single database writer and is deliberately not highly available.

The most significant known gap: job results stay on the worker that produced
them, and there is no artifact retrieval path yet. Placement is also a race
between eligible workers rather than a real scheduler — see
[Architecture](docs/architecture.md) for why that is currently adequate and when
it stops being so.

Deployment specifics — hosts, addresses, accounts, machine inventory, and
operational runbooks — are intentionally kept out of this repository.
