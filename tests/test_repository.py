import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from app.database import Database
from app.repository import JobRepository
from app.service import JobService
from contracts.models import JobCreate, JobType, SleepParameters, WorkerClaim


def enable_worker(
    repository: JobRepository,
    worker_id: str,
    now: datetime,
    supported_types: list[JobType] | None = None,
) -> None:
    """Register a worker and turn its scheduling on.

    Workers register with scheduling disabled, so any test that expects a
    claim to succeed has to enable the worker first.
    """
    repository.heartbeat(
        worker_id,
        supported_types or [JobType.SLEEP],
        now,
        now,
        None,
        None,
    )
    assert repository.set_worker_enabled(worker_id, True, now) is not None


@pytest.mark.parametrize("_attempt", range(10))
def test_only_one_worker_can_claim_one_job_under_contention(
    tmp_path: Path,
    _attempt: int,
) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    JobService(JobRepository(database)).create(
        JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1))
    )
    registry = JobRepository(database)
    registration_time = datetime.now(UTC)
    enable_worker(registry, "mac-one", registration_time)
    enable_worker(registry, "mac-two", registration_time)
    barrier = Barrier(2)

    def claim(worker_id: str):
        service = JobService(JobRepository(database))
        barrier.wait()
        return service.claim(WorkerClaim(worker_id=worker_id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ["mac-one", "mac-two"]))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0].status.value == "RUNNING"


def test_existing_database_is_migrated_without_losing_job(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.db"
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    timestamp = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(job_id),
                "sleep",
                json.dumps({"seconds": 1}),
                "QUEUED",
                timestamp,
                timestamp,
            ),
        )

    database = Database(database_path)
    database.initialize()
    database.initialize()

    preserved = JobRepository(database).get(job_id)
    with sqlite3.connect(database_path) as connection:
        versions = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(jobs)").fetchall()
        }

    assert preserved is not None
    assert preserved.status.value == "QUEUED"
    assert preserved.name is None
    assert preserved.attempt == 0
    assert preserved.max_attempts == 3
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8}
    assert "jobs_queue_order" in indexes


def test_worker_enabled_state_survives_database_reinitialization(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    repository.claim(
        "mac-one",
        [JobType.SLEEP],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )

    enabled = repository.set_worker_enabled("mac-one", True, now)
    database.initialize()
    repository.heartbeat("mac-one", [JobType.SLEEP], now, now, None, None)
    workers = repository.list_workers(now)

    assert enabled is not None
    assert enabled.enabled is True
    assert workers[0].enabled is True


def test_new_worker_registers_with_scheduling_disabled(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    JobService(repository).create(
        JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1))
    )
    now = datetime.now(UTC)

    first_claim = repository.claim(
        "mac-brand-new",
        [JobType.SLEEP],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )
    workers = repository.list_workers(now)

    assert first_claim is None
    assert [worker.id for worker in workers] == ["mac-brand-new"]
    assert workers[0].enabled is False


def test_expired_leases_requeue_then_fail_at_attempt_limit(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    service = JobService(repository, max_attempts=2)
    created = service.create(
        JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1))
    )
    first_time = datetime(2026, 1, 1, tzinfo=UTC)
    first_token = uuid4()
    enable_worker(repository, "mac-one", first_time)
    enable_worker(repository, "mac-two", first_time)
    first = repository.claim(
        "mac-one",
        [JobType.SLEEP],
        first_time,
        first_token,
        first_time + timedelta(seconds=1),
    )
    assert first is not None

    recovered = repository.recover_expired(first_time + timedelta(seconds=2))
    queued = repository.get(created.id)

    assert recovered == 1
    assert queued is not None
    assert queued.status.value == "QUEUED"
    assert queued.attempt == 1
    assert queued.lease_token is None

    second_time = first_time + timedelta(seconds=3)
    second = repository.claim(
        "mac-two",
        [JobType.SLEEP],
        second_time,
        uuid4(),
        second_time + timedelta(seconds=1),
    )
    assert second is not None
    assert second.attempt == 2
    assert second.lease_token != first_token

    repository.recover_expired(second_time + timedelta(seconds=2))
    failed = repository.get(created.id)

    assert failed is not None
    assert failed.status.value == "FAILED"
    assert failed.attempt == 2
    assert "maximum attempts" in (failed.error or "")
    assert (
        repository.complete(
            created.id,
            "mac-one",
            first_token,
            {"slept_seconds": 1},
            second_time,
        )
        is None
    )


def test_repeated_claim_returns_workers_existing_active_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    service = JobService(repository)
    service.create(JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1)))
    service.create(JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=2)))
    enable_worker(repository, "mac-one", datetime.now(UTC))

    first = service.claim(WorkerClaim(worker_id="mac-one"))
    repeated = service.claim(WorkerClaim(worker_id="mac-one"))

    assert first is not None
    assert repeated is not None
    assert repeated.id == first.id
    assert repeated.lease_token == first.lease_token
    assert len([job for job in repository.list() if job.status.value == "QUEUED"]) == 1


def test_connections_are_reused_within_a_thread(tmp_path: Path) -> None:
    """Reuse is the point of the change, so assert it directly.

    Opening a connection per operation made SQLite checkpoint the WAL on every
    close, which is what turned a tiny database into gigabytes of daily writes.
    """
    database = Database(tmp_path / "jobs.db")
    database.initialize()

    with database.connect() as first:
        first_id = id(first)
    with database.connect() as second:
        assert id(second) == first_id, "a new connection was opened per operation"

    database.close()
    with database.connect() as after_close:
        assert id(after_close) != first_id, "close() should force a reconnect"


def test_each_thread_gets_its_own_connection(tmp_path: Path) -> None:
    """SQLite connections are not safe to share across threads by default,
    and FastAPI runs synchronous endpoints in a worker threadpool."""
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    seen: list[int] = []
    barrier = Barrier(2)

    def record() -> None:
        barrier.wait()
        with database.connect() as connection:
            seen.append(id(connection))

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: record(), range(2)))

    assert len(set(seen)) == 2, "threads shared a connection"


def test_a_failed_statement_does_not_poison_the_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)

    with pytest.raises(sqlite3.Error), database.connect() as connection:
        connection.execute("SELECT * FROM a_table_that_does_not_exist")

    # The thread must still be usable afterwards.
    assert repository.list() == []


def test_disabled_worker_polling_does_not_write(tmp_path: Path) -> None:
    """A disabled worker polls every couple of seconds and can claim nothing.

    Recording that on every poll is a database write that changes no state. On
    a microSD that is wear spent to note that nothing happened, so `claim`
    must not touch the row once the worker is known and disabled. Heartbeat
    still refreshes liveness.
    """
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    registered_at = datetime(2026, 1, 1, tzinfo=UTC)

    # First contact registers the worker even though it cannot claim.
    assert (
        repository.claim(
            "mac-one",
            [JobType.SLEEP],
            registered_at,
            uuid4(),
            registered_at + timedelta(seconds=15),
        )
        is None
    )
    first = repository.list_workers(registered_at)
    assert [w.id for w in first] == ["mac-one"]
    assert first[0].enabled is False

    # A later poll must not move last_seen, because it must not write at all.
    much_later = registered_at + timedelta(hours=1)
    assert (
        repository.claim(
            "mac-one",
            [JobType.SLEEP],
            much_later,
            uuid4(),
            much_later + timedelta(seconds=15),
        )
        is None
    )
    assert repository.list_workers(registered_at)[0].last_seen == first[0].last_seen

    # Heartbeat is what keeps liveness fresh, and still does.
    repository.heartbeat("mac-one", [JobType.SLEEP], much_later, much_later, None, None)
    assert repository.list_workers(registered_at)[0].last_seen > first[0].last_seen
