# Architecture

A small distributed job system for a home network: one always-on coordinator
that owns state, and laptops that supply compute only while they happen to be
available.

This document describes design and reasoning. It deliberately contains no
hostnames, addresses, accounts, or filesystem paths — those belong to a
particular deployment, not to the design.

## Shape of the system

```text
        clients (CLI, web)
                |
                v
    +-----------------------+
    |    control plane      |   always on, low power
    |  HTTP API + SQLite    |   owns job truth
    +-----------------------+
                ^
                | workers poll: claim, heartbeat, report
                |
    +-----------+-----------+
    |                       |
 worker A                worker B      on-demand, may disappear
    |                       |
 per-job container      per-job container
```

The coordinator is deliberately not a compute node. It stays responsive because
it only ever handles small messages: job contracts, heartbeats, leases, status,
and result metadata. Queued work simply waits when no eligible worker is online.

## Ownership boundaries

| Concern | Owner | Why |
|---|---|---|
| Job truth | SQLite on the coordinator | One writer, durable, trivially backed up |
| Job schemas | Shared contract package | Both sides must agree or nothing works |
| State transitions and leases | Control-plane service layer | Workers must not invent transitions |
| Execution | Worker host process | It observes real host resources |
| Untrusted code execution | Per-job container on the worker | The host agent must never import user code |
| Heavy compute | Workers only | The coordinator must not silently become one |

The database is the source of truth. Worker memory, dashboard state, HTTP
responses, and logs are views or transports — never competing authorities.

## Job lifecycle

```text
client submits
      |
      v
   QUEUED
      |
      | atomic claim by an eligible worker
      v
   RUNNING  + renewable lease
      |
      +--> success ------> COMPLETED
      |
      +--> handler error -> FAILED
      |
      +--> lease expires -> QUEUED, bounded by max_attempts, then FAILED
```

**Leases** are what make worker loss survivable. A claimed job carries a lease
token and an expiry. The worker renews it by heartbeat. If the worker sleeps,
loses network, or dies, the lease expires and the coordinator requeues the job —
up to a bounded attempt limit, so a job that reliably kills its worker fails
instead of looping forever.

Only the worker holding the current lease token may complete or fail a job. A
stale token is rejected, so a worker that comes back from the dead cannot
overwrite a result produced by its replacement.

**Idempotency.** An optional client-supplied key makes repeated identical
submissions return the same job. Reusing a key with a different payload is a
conflict, not a silent overwrite.

## Scheduling: there isn't one

Placement is worker-pull, not coordinator-push. Each enabled worker polls on a
short interval and self-selects the oldest queued job whose type it supports:

```sql
SELECT id FROM jobs
WHERE status = 'QUEUED' AND type IN (<caller's supported types>)
ORDER BY created_at ASC LIMIT 1
```

The consequences are worth stating plainly, because they are easy to
misread as intelligence:

- With two eligible workers, **whichever poll timer fires first wins.** The
  outcome is arbitrary and not reproducible.
- Workers report CPU, memory, storage and GPU metrics, and those are displayed —
  but they are **never consulted** when choosing a job. A saturated worker is as
  likely to win as an idle one.
- The claim is a single atomic conditional update, so exactly one worker can win
  a given job even under contention.
- A worker already holding a live lease is handed back its existing job rather
  than a new one, so each node runs at most one job at a time. This produces
  crude but real load spreading: whoever is free takes the next job.

This is adequate while workers advertise disjoint capabilities, because job type
then determines placement. It becomes a coin-flip the moment two workers can run
the same type. Explicit per-job targeting is the intended next step;
resource-aware and data-locality scheduling are further out.

## Scheduling eligibility is separate from liveness

Each registered worker has a durable enable/disable switch that lives in the
control plane, not the worker.

Disabling a worker does not stop its process, and does not change whether it is
online, busy, or stale. It atomically blocks *future* claims while heartbeats
and metrics continue. A job already holding a valid lease may finish normally —
this is graceful draining, not remote cancellation.

Workers register with that switch **off**. A newly seen worker is inert until
someone deliberately enables it, so an accidentally-started agent cannot quietly
begin consuming work.

## Data plane

Large files must not travel through the coordinator or sit in job rows. Two
paths exist:

**Linked datasets.** The job carries a URL, an exact byte count, and a SHA-256.
The selected worker downloads directly from the source, verifies size and digest
before use, and keeps a content-addressed local cache keyed by digest. The
coordinator never sees the bytes.

**Uploaded datasets.** Small files submitted through the web interface are staged
by the coordinator, which records an upload ID, digest, and size. The worker
retrieves them over its existing authenticated connection and verifies them
again. Here the coordinator *is* in the byte path, which is why uploads are size
capped and linked datasets remain the route for anything large.

Verification happens on the consuming side in both cases. A declared digest that
does not match what arrived is a hard failure, not a warning.

### Publishing results

Inputs travel to the compute; results travel back. A worker that finishes a job
uploads its output files to the coordinator, which stores them under the job's
identifier on durable storage. The job record keeps only metadata — exit code,
truncated output streams, and file names.

Three details make this work rather than merely function:

**The upload is authorised by the same lease that authorises completion.**
Publishing results mutates a job's output, so it demands the same proof as
finishing it. A worker whose lease has expired — because it froze and the job
was requeued — cannot overwrite the results of whichever worker took over.

**The upload happens while the lease is still being renewed.** This is easy to
get wrong. If results are uploaded after the heartbeat loop stops, a transfer
slower than the lease interval causes the coordinator to declare the worker dead
and requeue the job *while it is succeeding*. Large jobs would then silently run
twice while small ones behaved perfectly.

**The coordinator refuses to write to the wrong disk.** Where durable storage is
a removable volume, an absent disk leaves an ordinary directory at the mount
point, and writes would quietly fill the system disk instead of failing. The
write path compares device identity against the root filesystem and refuses
rather than proceeding.

Results are exposed read-only to any file-sharing layer. The coordinator owns
that directory; letting a share client delete from it would create a second
writer and no way to reconcile the two.

### Keeping storage bounded

Every job leaves data in several places: staged inputs on the coordinator, a
content-addressed cache and a per-job output directory on the worker, and the
published results. Left alone, all of them grow forever.

Three rules keep that in check, and the distinction between them matters:

- **Inputs are released when a job reaches a terminal state.** Nothing will ask
  for them again. An input still referenced by an unfinished job is kept, since
  two jobs may legitimately share one upload.
- **The worker's copy is deleted once publishing succeeds.** At that point it is
  pure duplication. A *failed* publish leaves it alone — it is then the only
  remaining copy.
- **Published results are never deleted automatically by age.** They are what
  the job was for. A total-size ceiling exists purely as a backstop against a
  runaway, evicting least-recently-touched jobs and logging loudly; removal is
  otherwise an explicit operator action.

The asymmetry is deliberate. Inputs and intermediates are reconstructible or
redundant, so they expire on a rule. Results are not, so they expire on a
decision.

## Isolating untrusted code

The system accepts user-supplied Python. The host worker agent never imports or
executes it. Instead the worker stages the script and its input into a per-job
directory and launches a **fixed, pre-built container image**, with:

- no network
- read-only root filesystem
- read-only input mount; only the output directory is writable
- non-root user, all capabilities dropped, no-new-privileges
- CPU, memory, swap, PID, and wall-clock limits
- no credentials and no container-runtime socket inside

The image is pinned and built ahead of time, not assembled per job — so a job
cannot influence its own runtime. A worker advertises the batch capability only
when it can actually see that image, and re-checks periodically, so capability
appears and disappears on its own without restarts.

This is appropriate for trusted household workloads. It is not a claim of
hostile multi-tenant isolation.

### Resource limits are for pacing, not just safety

The CPU limit exists as much to make a job *considerate* as to contain it. A
hard quota below one core lets an expensive search run for hours on a laptop
that is also being used for something else — the fans stay off and the machine
stays responsive, at the cost of proportionally longer wall-clock time.

For that to work, the container's thread pools must match the quota. A quota
caps CPU *time* but not the core count the container observes, and libraries
that size their pools from the visible core count will start far more threads
than the quota can run. They then spend the difference on context switching, so
the throttle costs more than it should. The worker pins the thread-pool
environment to the quota and advertises the quota to the job, so a script can
size its own parallelism to it rather than to the host.

## Trust boundaries

```text
public internet
      |
      | no port forwarding
      v
private overlay network        reachability only
      |
      +-- HTTPS  -> API token, or a session cookie exchanged for it
      +-- SSH    -> key-based authentication
      +-- SMB    -> its own separate account
```

Network-level privacy and application authentication are separate concerns. The
overlay network supplies reachability; every service still authenticates
independently. Compromising one does not grant the other.

Secrets live in owner-only files outside the source tree and are never
committed. Uploaded files are stored under generated identifiers rather than
client-supplied names.

## Removable storage

A container bind-mounting a path on removable media will happily write to the
mount *point* when the media is absent — silently filling the system disk
instead of the intended volume.

The guard is to make the consumer prove the right filesystem is mounted: the
service unit verifies the expected filesystem UUID before starting, and the
mount itself is configured to let the machine boot without the disk. Absent
disk therefore means "service does not start," not "service writes to the wrong
place."

## Failure behaviour

| Failure | Result | Response |
|---|---|---|
| Worker sleeps or disconnects | Heartbeat goes stale; leased job requeued within its attempt limit | Restore the worker, or leave work queued |
| Worker administratively disabled | Keeps heartbeating, claims nothing; current job may finish | Re-enable when it should accept work |
| Coordinator stops | Submission and coordination stop; SQLite data remains durable | Inspect logs, restart |
| Overlay network down | Remote access stops; local network still works | Check the network daemon |
| Removable disk absent at boot | Machine boots; dependent service fails its mount check; API unaffected | Reconnect, mount, start the service |
| Disk pulled during a write | Processes may see I/O errors; recent data may be corrupt | Stop consumers, remount, check the filesystem |
| Database unavailable | Liveness may still pass while readiness fails | Restore the database before accepting work |

Liveness and readiness are deliberately distinct: a process can be alive and
still unable to serve.

## Decisions worth keeping

1. The coordinator coordinates; laptops compute.
2. The database owns job truth; workers mutate state only through the API.
3. Large files never travel in job rows or job payloads.
4. Network privacy is not authentication; each service authenticates anyway.
5. Host services own hardware and networking; containers isolate applications.
6. Consumers of removable storage must prove the intended filesystem is mounted.
7. Scheduling eligibility is durable control-plane state, independent of worker
   connectivity and execution state.
8. Untrusted code runs only inside a fixed image; the host agent never executes
   it directly.

## Known limits

- No real scheduler; placement is a race between eligible workers.
- The worker's content-addressed caches still grow without bound. Deduplication
  across jobs slows that rather than solving it.
- Published results are never evicted except by an explicit deletion or the
  size ceiling, which is intentional but means the store's growth is governed by
  operator discipline rather than by policy.
- Results pass through the coordinator rather than going directly to storage.
  Correct while storage is a disk attached to the coordinator; the natural fix
  at larger scale is object storage with pre-signed upload URLs, which takes the
  coordinator out of the byte path entirely.
- Telemetry is carried inside the claim and heartbeat messages rather than
  exposed separately, so monitoring a worker requires speaking the job protocol.
- Single coordinator, single database writer — this is not a highly available
  design, by choice.
