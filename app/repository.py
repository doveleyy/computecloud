from __future__ import annotations

import json
import sqlite3
from builtins import list as list_type
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from app.database import Database
from contracts.models import (
    DatasetScriptParameters,
    JobParameters,
    JobRead,
    JobStatus,
    JobType,
    PythonBatchParameters,
    SleepParameters,
    WorkerMetrics,
    WorkerRead,
    WorkerState,
)


class JobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, job: JobRead, idempotency_key: str | None = None) -> JobRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    id, type, parameters_json, status, created_at, updated_at,
                    attempt, max_attempts, idempotency_key, name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    str(job.id),
                    job.type.value,
                    json.dumps(job.parameters.model_dump(mode="json")),
                    job.status.value,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.attempt,
                    job.max_attempts,
                    idempotency_key,
                    job.name,
                ),
            )
            if cursor.rowcount == 1:
                return job
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        if row is None:
            raise RuntimeError("idempotent insert did not return a job")
        return self._row_to_job(row)

    def get(self, job_id: UUID) -> JobRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list(self) -> list[JobRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def ping(self) -> None:
        self.database.ping()

    def claim(
        self,
        worker_id: str,
        supported_types: Sequence[JobType],
        claimed_at: datetime,
        lease_token: UUID,
        lease_expires_at: datetime,
        metrics: WorkerMetrics | None = None,
    ) -> JobRead | None:
        supported_json = json.dumps([job_type.value for job_type in supported_types])
        metrics_json = json.dumps(metrics.model_dump(mode="json")) if metrics else None
        with self.database.connect() as connection:
            self._recover_expired(connection, claimed_at)
            self._upsert_worker(
                connection,
                worker_id,
                supported_json,
                claimed_at,
                current_job_id=None,
                metrics_json=metrics_json,
            )

            worker = connection.execute(
                "SELECT enabled FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if worker is None or not bool(worker["enabled"]):
                return None

            active = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ? AND worker_id = ? AND lease_expires_at > ?
                ORDER BY started_at ASC LIMIT 1
                """,
                (JobStatus.RUNNING.value, worker_id, claimed_at.isoformat()),
            ).fetchone()
            if active is not None:
                connection.execute(
                    "UPDATE workers SET current_job_id = ? WHERE id = ?",
                    (active["id"], worker_id),
                )
                return self._row_to_job(active)

            placeholders = ", ".join("?" for _ in supported_types)
            query = f"""
                UPDATE jobs
                SET status = ?, worker_id = ?, started_at = ?, updated_at = ?,
                    attempt = attempt + 1, lease_token = ?, lease_expires_at = ?,
                    finished_at = NULL, result_json = NULL, error = NULL
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = ? AND type IN ({placeholders})
                    ORDER BY created_at ASC LIMIT 1
                )
                AND status = ?
                RETURNING *
            """
            values = (
                JobStatus.RUNNING.value,
                worker_id,
                claimed_at.isoformat(),
                claimed_at.isoformat(),
                str(lease_token),
                lease_expires_at.isoformat(),
                JobStatus.QUEUED.value,
                *(job_type.value for job_type in supported_types),
                JobStatus.QUEUED.value,
            )
            row = connection.execute(query, values).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE workers SET current_job_id = ? WHERE id = ?",
                    (row["id"], worker_id),
                )
        return self._row_to_job(row) if row is not None else None

    def heartbeat(
        self,
        worker_id: str,
        supported_types: Sequence[JobType],
        seen_at: datetime,
        lease_expires_at: datetime,
        current_job_id: UUID | None,
        lease_token: UUID | None,
        metrics: WorkerMetrics | None = None,
    ) -> bool:
        supported_json = json.dumps([job_type.value for job_type in supported_types])
        metrics_json = json.dumps(metrics.model_dump(mode="json")) if metrics else None
        with self.database.connect() as connection:
            if current_job_id is not None and lease_token is not None:
                renewed = connection.execute(
                    """
                    UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND worker_id = ?
                        AND lease_token = ? AND lease_expires_at > ?
                    """,
                    (
                        lease_expires_at.isoformat(),
                        seen_at.isoformat(),
                        str(current_job_id),
                        JobStatus.RUNNING.value,
                        worker_id,
                        str(lease_token),
                        seen_at.isoformat(),
                    ),
                )
                if renewed.rowcount != 1:
                    return False
                current = str(current_job_id)
            else:
                current = None
            self._upsert_worker(
                connection,
                worker_id,
                supported_json,
                seen_at,
                current,
                metrics_json,
            )
        return True

    def complete(
        self,
        job_id: UUID,
        worker_id: str,
        lease_token: UUID,
        result: dict[str, object],
        finished_at: datetime,
    ) -> JobRead | None:
        return self._finish(
            job_id,
            worker_id,
            lease_token,
            finished_at,
            JobStatus.COMPLETED,
            result=result,
        )

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
        finished_at: datetime,
    ) -> JobRead | None:
        return self._finish(
            job_id,
            worker_id,
            lease_token,
            finished_at,
            JobStatus.FAILED,
            error=error,
        )

    def _finish(
        self,
        job_id: UUID,
        worker_id: str,
        lease_token: UUID,
        finished_at: datetime,
        status: JobStatus,
        *,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> JobRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_json = ?, error = ?, finished_at = ?,
                    updated_at = ?, lease_token = NULL, lease_expires_at = NULL
                WHERE id = ? AND status = ? AND worker_id = ?
                    AND lease_token = ? AND lease_expires_at > ?
                RETURNING *
                """,
                (
                    status.value,
                    json.dumps(result) if result is not None else None,
                    error,
                    finished_at.isoformat(),
                    finished_at.isoformat(),
                    str(job_id),
                    JobStatus.RUNNING.value,
                    worker_id,
                    str(lease_token),
                    finished_at.isoformat(),
                ),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE workers SET current_job_id = NULL WHERE id = ?",
                    (worker_id,),
                )
        return self._row_to_job(row) if row is not None else None

    def recover_expired(self, recovered_at: datetime) -> int:
        with self.database.connect() as connection:
            return self._recover_expired(connection, recovered_at)

    def list_workers(self, stale_before: datetime) -> list_type[WorkerRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workers ORDER BY last_seen DESC"
            ).fetchall()
        return [self._row_to_worker(row, stale_before) for row in rows]

    def set_worker_enabled(
        self, worker_id: str, enabled: bool, stale_before: datetime
    ) -> WorkerRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE workers SET enabled = ? WHERE id = ? RETURNING *",
                (int(enabled), worker_id),
            ).fetchone()
        return self._row_to_worker(row, stale_before) if row is not None else None

    @staticmethod
    def _upsert_worker(
        connection: sqlite3.Connection,
        worker_id: str,
        supported_types_json: str,
        seen_at: datetime,
        current_job_id: str | None,
        metrics_json: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workers (
                id, supported_types_json, registered_at, last_seen, current_job_id,
                metrics_json, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                supported_types_json = excluded.supported_types_json,
                last_seen = excluded.last_seen,
                current_job_id = excluded.current_job_id,
                metrics_json = COALESCE(excluded.metrics_json, workers.metrics_json)
            """,
            (
                worker_id,
                supported_types_json,
                seen_at.isoformat(),
                seen_at.isoformat(),
                current_job_id,
                metrics_json,
            ),
        )

    @staticmethod
    def _row_to_worker(row: sqlite3.Row, stale_before: datetime) -> WorkerRead:
        last_seen = datetime.fromisoformat(row["last_seen"])
        if last_seen < stale_before:
            state = WorkerState.STALE
        elif row["current_job_id"] is not None:
            state = WorkerState.BUSY
        else:
            state = WorkerState.ONLINE
        return WorkerRead(
            id=row["id"],
            enabled=bool(row["enabled"]),
            supported_types=[
                JobType(value) for value in json.loads(row["supported_types_json"])
            ],
            registered_at=datetime.fromisoformat(row["registered_at"]),
            last_seen=last_seen,
            current_job_id=(
                UUID(row["current_job_id"])
                if row["current_job_id"] is not None
                else None
            ),
            metrics=(
                WorkerMetrics.model_validate(json.loads(row["metrics_json"]))
                if row["metrics_json"] is not None
                else None
            ),
            state=state,
        )

    @staticmethod
    def _recover_expired(connection: sqlite3.Connection, recovered_at: datetime) -> int:
        now = recovered_at.isoformat()
        expired_workers = connection.execute(
            """
            SELECT DISTINCT worker_id FROM jobs
            WHERE status = ? AND lease_expires_at <= ? AND worker_id IS NOT NULL
            """,
            (JobStatus.RUNNING.value, now),
        ).fetchall()
        failed = connection.execute(
            """
            UPDATE jobs SET status = ?, finished_at = ?, updated_at = ?,
                error = 'worker lease expired; maximum attempts reached',
                lease_token = NULL, lease_expires_at = NULL
            WHERE status = ? AND lease_expires_at <= ? AND attempt >= max_attempts
            """,
            (JobStatus.FAILED.value, now, now, JobStatus.RUNNING.value, now),
        ).rowcount
        requeued = connection.execute(
            """
            UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL,
                updated_at = ?, error = 'worker lease expired; job requeued',
                lease_token = NULL, lease_expires_at = NULL
            WHERE status = ? AND lease_expires_at <= ?
            """,
            (JobStatus.QUEUED.value, now, JobStatus.RUNNING.value, now),
        ).rowcount
        for row in expired_workers:
            connection.execute(
                "UPDATE workers SET current_job_id = NULL WHERE id = ?",
                (row["worker_id"],),
            )
        return failed + requeued

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRead:
        result_json = row["result_json"]
        job_type = JobType(row["type"])
        parameters_json = json.loads(row["parameters_json"])
        parameters: JobParameters
        if job_type is JobType.SLEEP:
            parameters = SleepParameters.model_validate(parameters_json)
        elif job_type is JobType.DATASET_SCRIPT:
            parameters = DatasetScriptParameters.model_validate(parameters_json)
        else:
            parameters = PythonBatchParameters.model_validate(parameters_json)
        return JobRead(
            id=UUID(row["id"]),
            name=row["name"],
            type=job_type,
            parameters=parameters,
            status=JobStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            worker_id=row["worker_id"],
            started_at=(
                datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
            ),
            finished_at=(
                datetime.fromisoformat(row["finished_at"])
                if row["finished_at"]
                else None
            ),
            result=json.loads(result_json) if result_json else None,
            error=row["error"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            lease_token=UUID(row["lease_token"]) if row["lease_token"] else None,
            lease_expires_at=(
                datetime.fromisoformat(row["lease_expires_at"])
                if row["lease_expires_at"]
                else None
            ),
        )
