# Configuration

Every setting is an environment variable. There is no configuration file format
to learn, and no setting that can only be changed in code.

Values below are defaults. Paths are relative to the process working directory
unless absolute.

## Control plane

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_DB_PATH` | `data/home-platform.db` | SQLite database — the source of job truth |
| `HOME_PLATFORM_API_TOKEN` | unset | Token value. Prefer the file form below |
| `HOME_PLATFORM_API_TOKEN_FILE` | unset | Path to a file containing the token. Preferred: a value in the environment is visible in the process list |
| `HOME_PLATFORM_LEASE_SECONDS` | `15` | How long a claimed job's lease lasts before it must be renewed |
| `HOME_PLATFORM_WORKER_STALE_SECONDS` | `20` | Silence after which a worker is reported `STALE` |
| `HOME_PLATFORM_RECOVERY_INTERVAL_SECONDS` | `2` | How often expired leases are swept and requeued |
| `HOME_PLATFORM_MAX_ATTEMPTS` | `3` | Requeues before a job is failed permanently |

If no token is configured the API is unauthenticated. That is only appropriate
for local development.

`LEASE_SECONDS` is the interesting one: too short and a briefly-paused worker
loses its job; too long and a dead worker's job sits idle before recovery. It
must comfortably exceed the worker's heartbeat interval.

## Uploads (staged job inputs)

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_UPLOAD_DIR` | `data/uploads` | Where submitted scripts and datasets are staged |
| `HOME_PLATFORM_MAX_UPLOAD_BYTES` | `10485760` (10 MiB) | Per-dataset upload ceiling |
| `HOME_PLATFORM_MAX_SCRIPT_UPLOAD_BYTES` | `262144` (256 KiB) | Per-script upload ceiling |

Uploads are deleted automatically once the job referencing them reaches a
terminal state, unless another unfinished job still references the same upload.

Keep the dataset ceiling low. Uploads pass *through* the coordinator, so this is
the one data path where it sits in the byte stream; anything large should use a
linked URL instead, which goes directly to the worker.

## Artifacts (published job results)

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_ARTIFACT_DIR` | `data/artifacts` | Where published results are stored |
| `HOME_PLATFORM_MAX_ARTIFACT_BYTES` | `104857600` (100 MiB) | Per-file ceiling |
| `HOME_PLATFORM_MAX_JOB_ARTIFACT_BYTES` | `536870912` (512 MiB) | Per-job total ceiling |
| `HOME_PLATFORM_MAX_ARTIFACT_STORE_BYTES` | `53687091200` (50 GiB) | Whole-store ceiling — a backstop, not a policy |
| `HOME_PLATFORM_ARTIFACT_REQUIRE_MOUNT` | unset (false) | Refuse to write unless the artifact directory is on a different device from `/` |

Results never expire by age. The store ceiling only evicts least-recently-touched
jobs if a runaway threatens the disk, and logs each eviction at `WARNING`.

These limits also define the practical download system today. Each artifact is
served as a streamed file response and may be downloaded through the API, CLI,
or authenticated Job Desk. Job Desk only previews text-like files up to 256 KiB;
that preview threshold is a browser-interface safety limit, not an artifact
storage limit. Transfers are not yet resumable and have no progress contract.

**Set `ARTIFACT_REQUIRE_MOUNT` whenever the artifact directory lives on removable
storage.** If that disk is absent, its mount point is still a perfectly writable
directory on the system disk, so writes would succeed and quietly fill it. The
check compares device identity against the root filesystem.

## Service health reporting

The dashboard reports on a file-sharing service if one is present. These only
affect what it displays; nothing functional depends on them.

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_NAS_MOUNT` | `/srv/home-platform/storage` | Mount point checked for presence and capacity |
| `HOME_PLATFORM_NAS_SERVICE` | `home-platform-nas` | systemd unit whose state is reported |

The health check combines four signals — the mount point being a real mount, its
capacity, the service unit's state, and TCP reachability of the share port. It is
a liveness indication, not proof that an authenticated read or write would
succeed.

## Worker

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_API_URL` | `http://raspberrypi.local:8000` | Control plane to poll |
| `HOME_PLATFORM_WORKER_ID` | `worker-<hostname>` | Identity in the registry. Stable across restarts |
| `HOME_PLATFORM_API_TOKEN_FILE` | `~/.config/home-platform/api-token` | Token used for every call |
| `HOME_PLATFORM_WORKER_DATA_DIR` | `~/.local/share/home-platform-worker` | Cache, staged inputs, and outputs |
| `HOME_PLATFORM_POLL_SECONDS` | `2` | How often to ask for work |
| `HOME_PLATFORM_HEARTBEAT_SECONDS` | `5` | Lease renewal interval. Must be well under `LEASE_SECONDS` |
| `HOME_PLATFORM_DATASET_ALLOWED_HOSTS` | unset | Comma-separated hosts this worker may download from. **Empty means it will not advertise `dataset_script` at all** |
| `HOME_PLATFORM_MAX_DATASET_BYTES` | `10737418240` (10 GiB) | Largest dataset this worker accepts |
| `HOME_PLATFORM_CONTAINER_IMAGE` | `home-platform-ml:0.1` | Image used for `python_batch`. The worker advertises that type only while this image exists locally |

A worker registers with scheduling **disabled**; it claims nothing until enabled
through the API, CLI, or dashboard.

## Inside a job container

These are set *by* the worker and read *by* your script.

| Variable | Meaning |
|---|---|
| `HOME_PLATFORM_DATASET` | Absolute path to the input CSV, read-only |
| `HOME_PLATFORM_OUTPUT_DIR` | Write results here — everything left behind is published as an artifact |
| `HOME_PLATFORM_JOB_ID` | The job's UUID |
| `HOME_PLATFORM_CPU_LIMIT` | The CPU quota this job was given, as a float |

Thread-pool variables (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`) are also set,
pinned to the CPU quota. See [Job contract](job-contract.md#resource-limits) for
why that matters.

## Per-job resource limits

Set at submission rather than by environment:

| Parameter | Range | Default |
|---|---|---|
| `cpu_limit` | 0.1 – 8.0 | 2.0 |
| `memory_mb` | 256 – 16384 | 2048 |
| `timeout_seconds` | 1 – 86400 (24 h) | 1800 |

`cpu_limit` is a hard quota, not a priority. A fraction runs the job slowly and
coolly rather than merely deprioritising it, which makes long overnight training
runs practical on a machine you are also using.
