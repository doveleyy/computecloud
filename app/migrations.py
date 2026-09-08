import sqlite3
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        row["version"]
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    for version, migration in MIGRATIONS:
        if version not in applied:
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )


def create_jobs_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def add_execution_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    new_columns = {
        "worker_id": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "result_json": "TEXT",
        "error": "TEXT",
    }
    for name, column_type in new_columns.items():
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}")


def add_queue_index(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS jobs_queue_order
        ON jobs (status, type, created_at)
        """
    )


def add_leases_and_workers(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    new_columns = {
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "INTEGER NOT NULL DEFAULT 3",
        "lease_token": "TEXT",
        "lease_expires_at": "TEXT",
    }
    for name, column_type in new_columns.items():
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            supported_types_json TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            current_job_id TEXT
        )
        """
    )


def add_submission_idempotency(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "idempotency_key" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_key
        ON jobs (idempotency_key) WHERE idempotency_key IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS jobs_expired_leases
        ON jobs (status, lease_expires_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS workers_last_seen
        ON workers (last_seen)
        """
    )


def add_worker_metrics(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(workers)").fetchall()
    }
    if "metrics_json" not in existing_columns:
        connection.execute("ALTER TABLE workers ADD COLUMN metrics_json TEXT")


def add_worker_scheduling_control(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(workers)").fetchall()
    }
    if "enabled" not in existing_columns:
        connection.execute(
            "ALTER TABLE workers ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )


def add_job_names(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "name" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN name TEXT")


def add_scheduling_and_failure_details(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "target_worker_id" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN target_worker_id TEXT")
    if "failure_kind" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN failure_kind TEXT")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS jobs_scheduling_order
        ON jobs (status, target_worker_id, type, created_at, id)
        """
    )


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, create_jobs_table),
    (2, add_execution_columns),
    (3, add_queue_index),
    (4, add_leases_and_workers),
    (5, add_submission_idempotency),
    (6, add_worker_metrics),
    (7, add_worker_scheduling_control),
    (8, add_job_names),
    (9, add_scheduling_and_failure_details),
)
