# Job and API Contract

The wire contract between clients, the control plane, and workers. Schemas live
in the shared models module and are enforced by Pydantic on both sides.

## Job identity and state

Every job has a server-generated UUID and an optional, **non-unique**
human-readable name. Repeated runs may deliberately share a name; identity,
leases, idempotency, and state transitions always use the UUID.

```text
QUEUED -> RUNNING -> COMPLETED
                  -> FAILED
RUNNING --lease expires--> QUEUED, until max_attempts, then FAILED
```

Supplying an `Idempotency-Key` header makes repeated identical submissions
return the same job. Reusing a key with a different payload returns `409`.

## Job types

| Type | Input | Execution | Result |
|---|---|---|---|
| `sleep` | `seconds`, 1–30 | Worker sleeps | `slept_seconds` |
| `dataset_script` | Reviewed script ID + verified dataset, timeout ≤ 1 h | Host subprocess runs an allow-listed script | Row/column summary + artifact reference |
| `python_batch` | Uploaded `.py` + verified dataset, timeout ≤ 6 h, 0.5–4 CPUs, 256–4096 MiB | Fixed container image, no network, read-only inputs | Exit code, truncated stdout/stderr, output filenames, artifact reference |

`dataset_script` is legacy: it runs a fixed, reviewed script on the host rather
than arbitrary code in a container. `python_batch` supersedes it.

## Dataset references

A job never carries dataset bytes. It carries one of:

**Linked** — an HTTPS URL, exact `size_bytes`, and `sha256`. The worker
downloads directly from the source. The host must be in that worker's allowlist,
redirects are refused, and both size and digest are verified before use.

**Uploaded** — an `upload_id`, `sha256`, and `size_bytes` returned by a prior
upload call. The worker fetches it from the control plane over its existing
authenticated connection and verifies it again.

In both cases the worker keeps a content-addressed cache keyed by digest, so a
repeated dataset is fetched once.

## Worker protocol

Workers poll; the control plane never pushes.

| Call | Purpose |
|---|---|
| `POST /workers/claim` | Report identity, supported types and metrics; receive a job or null |
| `POST /workers/heartbeat` | Renew a lease and refresh metrics |
| `POST /jobs/{id}/complete` | Submit a result — requires the current lease token |
| `POST /jobs/{id}/fail` | Report failure — requires the current lease token |
| `GET /workers` | List registered workers |
| `PATCH /workers/{id}` | Enable or disable scheduling |

A claim returns the oldest queued job whose type the caller supports, or null.
It is atomic: exactly one worker can win a given job. A worker that already
holds a live lease is handed back its existing job rather than a new one.

Completion and failure require the **current** lease token. A stale token is
rejected with `409`, so a revived worker cannot overwrite its replacement's
result.

Workers register with scheduling **disabled** and claim nothing until enabled.

## Job endpoints

| Call | Purpose |
|---|---|
| `POST /jobs` | Submit; honours `Idempotency-Key` |
| `GET /jobs` | List |
| `GET /jobs/{id}` | Retrieve one |
| `POST /uploads/datasets` | Stage a CSV, returns a verified reference |
| `POST /uploads/scripts` | Stage a Python script, returns a verified reference |

## Probes

| Endpoint | Meaning |
|---|---|
| `GET /health` | Process liveness only |
| `GET /ready` | Readiness, including database reachability |
| `GET /version` | Deployed application identity and version |

Liveness and readiness are distinct on purpose: the process can be alive while
the database is not reachable, and that must not read as healthy.

## Authentication

API calls use an `X-API-Token` header. The web interfaces exchange that token
once for an HttpOnly, `SameSite=Strict` session cookie, so the token itself is
never held in browser JavaScript.

Uploads are size-capped and stored under generated identifiers rather than
client-supplied filenames.

## Results and artifacts

The job record holds **metadata only** — exit codes, truncated stdout and
stderr, digests, and output file names. The files themselves are published
separately.

| Call | Purpose |
|---|---|
| `POST /jobs/{id}/artifacts` | Worker publishes one output file |
| `GET /jobs/{id}/artifacts` | List a job's files |
| `GET /jobs/{id}/artifacts/{filename}` | Download one |

Publishing is authorised by `worker_id` **plus the current lease token**, sent as
form fields alongside the file. It carries the same authority as completing the
job, because it changes the job's output: a worker whose lease has expired
receives `409` and cannot overwrite the results of its replacement. Once a job
reaches a terminal state its lease is gone, so publishing stops working too.

File names are validated against an allow-list — a plain name, no separators, no
traversal, no leading dot — because they originate from user-supplied code and
are used to build a path. Per-file and per-job size limits are enforced while
streaming rather than trusting a declared length.

The `worker://` URI in the result remains as a record of which worker produced
the files. Retrieval goes through the endpoints above.
