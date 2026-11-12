from __future__ import annotations

import itertools
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from weibo_publisher.models import (
    Batch,
    BatchStatus,
    Job,
    JobStatus,
    Visibility,
)

ACTIVE_STATUSES = {
    JobStatus.APPROVED,
    JobStatus.PREPARING,
    JobStatus.READY,
    JobStatus.PUBLISHING,
    JobStatus.PUBLISHED,
}
SQLITE_BUSY_TIMEOUT_MS = 5_000


class ConfirmationError(ValueError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    images_json TEXT NOT NULL,
    publish_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    visibility TEXT NOT NULL,
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    last_attempt_at TEXT,
    published_at TEXT,
    weibo_mid TEXT,
    weibo_url TEXT,
    last_error TEXT,
    artifact_dir TEXT,
    rehearsal INTEGER NOT NULL DEFAULT 0,
    batch_id TEXT REFERENCES batches(id),
    source_row INTEGER,
    source_sequence TEXT,
    source_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    error TEXT
);
"""


class JobRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self._db_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(jobs)"
                ).fetchall()
            }
            if "rehearsal" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN "
                    "rehearsal INTEGER NOT NULL DEFAULT 0"
                )
            for column_name, definition in (
                ("batch_id", "TEXT"),
                ("source_row", "INTEGER"),
                ("source_sequence", "TEXT"),
                ("source_fingerprint", "TEXT"),
            ):
                if column_name not in columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN "
                        f"{column_name} {definition}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_batch_id ON jobs(batch_id)"
            )

    def create_job(self, job: Job) -> None:
        with self._connect() as connection:
            self._insert_job(connection, job)

    def _insert_job(
        self,
        connection: sqlite3.Connection,
        job: Job,
    ) -> None:
        connection.execute(
            """
            INSERT INTO jobs (
                id,
                text,
                images_json,
                publish_at_utc,
                timezone,
                visibility,
                status,
                fingerprint,
                created_at,
                confirmed_at,
                last_attempt_at,
                published_at,
                weibo_mid,
                weibo_url,
                last_error,
                artifact_dir,
                rehearsal,
                batch_id,
                source_row,
                source_sequence,
                source_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.text,
                json.dumps([str(image) for image in job.images]),
                self._to_iso(job.publish_at_utc),
                job.timezone,
                job.visibility.value,
                job.status.value,
                job.fingerprint,
                self._to_iso(job.created_at),
                self._to_iso(job.confirmed_at),
                self._to_iso(job.last_attempt_at),
                self._to_iso(job.published_at),
                job.weibo_mid,
                job.weibo_url,
                job.last_error,
                str(job.artifact_dir) if job.artifact_dir else None,
                int(job.rehearsal),
                job.batch_id,
                job.source_row,
                job.source_sequence,
                job.source_fingerprint,
            ),
        )

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY publish_at_utc",
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY publish_at_utc",
                    (status.value,),
                ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def create_batch(self, batch: Batch, jobs: list[Job]) -> None:
        if not jobs:
            raise ConfirmationError("batch must contain at least one job")
        if any(job.batch_id != batch.id for job in jobs):
            raise ConfirmationError("every batch job must reference the batch")
        if any(job.status is not JobStatus.DRAFT for job in jobs):
            raise ConfirmationError("new batch jobs must be drafts")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_batch(connection, batch)
            for job in jobs:
                self._insert_job(connection, job)

    def replace_draft_batch(
        self,
        batch: Batch,
        jobs: list[Job],
    ) -> None:
        if not jobs:
            raise ConfirmationError("batch must contain at least one job")
        if any(job.batch_id != batch.id for job in jobs):
            raise ConfirmationError("every batch job must reference the batch")
        if any(job.status is not JobStatus.DRAFT for job in jobs):
            raise ConfirmationError("replacement batch jobs must be drafts")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch.id,),
            ).fetchone()
            if existing is None:
                raise ConfirmationError(f"batch not found: {batch.id}")
            if BatchStatus(existing["status"]) is not BatchStatus.DRAFT:
                raise ConfirmationError(
                    f"batch is not a draft: {existing['status']}"
                )
            non_drafts = connection.execute(
                """
                SELECT COUNT(*)
                FROM jobs
                WHERE batch_id = ? AND status != ?
                """,
                (batch.id, JobStatus.DRAFT.value),
            ).fetchone()[0]
            if non_drafts:
                raise ConfirmationError(
                    "batch has jobs that are no longer drafts"
                )
            connection.execute(
                """
                DELETE FROM attempts
                WHERE job_id IN (
                    SELECT id FROM jobs WHERE batch_id = ?
                )
                """,
                (batch.id,),
            )
            connection.execute(
                "DELETE FROM jobs WHERE batch_id = ?",
                (batch.id,),
            )
            cursor = connection.execute(
                """
                UPDATE batches
                SET name = ?,
                    source_path = ?,
                    source_fingerprint = ?,
                    local_date = ?,
                    timezone = ?,
                    status = ?,
                    created_at = ?,
                    approved_at = NULL,
                    last_error = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    batch.name,
                    str(batch.source_path),
                    batch.source_fingerprint,
                    batch.local_date.isoformat(),
                    batch.timezone,
                    batch.status.value,
                    self._to_iso(batch.created_at),
                    batch.id,
                    BatchStatus.DRAFT.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfirmationError("batch changed while replacing")
            for job in jobs:
                self._insert_job(connection, job)

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        return self._row_to_batch(row) if row else None

    def list_batches(
        self,
        status: BatchStatus | None = None,
    ) -> list[Batch]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT *
                    FROM batches
                    WHERE status = ?
                    ORDER BY created_at DESC
                    """,
                    (status.value,),
                ).fetchall()
        return [self._row_to_batch(row) for row in rows]

    def list_batch_jobs(self, batch_id: str) -> list[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE batch_id = ?
                ORDER BY source_row, publish_at_utc
                """,
                (batch_id,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def find_draft_batch_for_source(
        self,
        source_path: Path,
    ) -> Batch | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM batches
                WHERE source_path = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(source_path.expanduser().resolve()), BatchStatus.DRAFT.value),
            ).fetchone()
        return self._row_to_batch(row) if row else None

    def batch_is_runnable(self, batch_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        return bool(
            row is not None
            and BatchStatus(row["status"]) is BatchStatus.APPROVED
        )

    def pause_batch(self, batch_id: str, error: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE batches
                SET status = ?, last_error = ?
                WHERE id = ? AND status = ?
                """,
                (
                    BatchStatus.PAUSED.value,
                    error,
                    batch_id,
                    BatchStatus.APPROVED.value,
                ),
            )
            return cursor.rowcount == 1

    def resume_batch(self, batch_id: str) -> Batch:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise ConfirmationError(f"batch not found: {batch_id}")
            batch = self._row_to_batch(row)
            if batch.status is not BatchStatus.PAUSED:
                raise ConfirmationError(
                    f"batch is not paused: {batch.status}"
                )
            active = connection.execute(
                """
                SELECT COUNT(*)
                FROM jobs
                WHERE batch_id = ?
                  AND status IN (?, ?, ?)
                """,
                (
                    batch_id,
                    JobStatus.PREPARING.value,
                    JobStatus.READY.value,
                    JobStatus.PUBLISHING.value,
                ),
            ).fetchone()[0]
            if active:
                raise ConfirmationError(
                    "batch has a job still in an active publish state"
                )
            cursor = connection.execute(
                """
                UPDATE batches
                SET status = ?, last_error = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    BatchStatus.APPROVED.value,
                    batch_id,
                    BatchStatus.PAUSED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfirmationError("batch changed while resuming")
            resumed = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if resumed is None:
                raise ConfirmationError("batch disappeared while resuming")
            return self._row_to_batch(resumed)

    def cancel_batch(self, batch_id: str) -> Batch:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                raise ConfirmationError(f"batch not found: {batch_id}")
            batch = self._row_to_batch(row)
            if batch.status is BatchStatus.CANCELLED:
                return batch
            blocked_statuses = {
                JobStatus.PREPARING,
                JobStatus.READY,
                JobStatus.PUBLISHING,
                JobStatus.PUBLISHED,
            }
            placeholders = ",".join("?" for _ in blocked_statuses)
            blocked = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM jobs
                WHERE batch_id = ? AND status IN ({placeholders})
                """,
                (batch_id, *(status.value for status in blocked_statuses)),
            ).fetchone()[0]
            if blocked:
                raise ConfirmationError(
                    "batch cannot be cancelled after preparation or publication"
                )
            connection.execute(
                """
                UPDATE jobs
                SET status = ?
                WHERE batch_id = ? AND status IN (?, ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    batch_id,
                    JobStatus.DRAFT.value,
                    JobStatus.APPROVED.value,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE batches
                SET status = ?, last_error = NULL
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    BatchStatus.CANCELLED.value,
                    batch_id,
                    BatchStatus.DRAFT.value,
                    BatchStatus.APPROVED.value,
                    BatchStatus.PAUSED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfirmationError(
                    f"batch cannot be cancelled from {batch.status.value}"
                )
            cancelled = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if cancelled is None:
                raise ConfirmationError("batch disappeared while cancelling")
            return self._row_to_batch(cancelled)

    def complete_batch_if_terminal(self, batch_id: str) -> bool:
        active_statuses = {
            JobStatus.APPROVED,
            JobStatus.PREPARING,
            JobStatus.READY,
            JobStatus.PUBLISHING,
        }
        placeholders = ",".join("?" for _ in active_statuses)
        with self._connect() as connection:
            active = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM jobs
                WHERE batch_id = ? AND status IN ({placeholders})
                """,
                (batch_id, *(status.value for status in active_statuses)),
            ).fetchone()[0]
            if active:
                return False
            cursor = connection.execute(
                """
                UPDATE batches
                SET status = ?
                WHERE id = ? AND status = ?
                """,
                (
                    BatchStatus.COMPLETED.value,
                    batch_id,
                    BatchStatus.APPROVED.value,
                ),
            )
            return cursor.rowcount == 1

    def confirm_batch(
        self,
        batch_id: str,
        fingerprints: dict[str, str],
        confirmed_at: datetime,
        *,
        daily_limit: int,
        minimum_gap_seconds: int,
        allow_over_limit: bool,
    ) -> list[Job]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch_row = connection.execute(
                "SELECT * FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch_row is None:
                raise ConfirmationError(f"batch not found: {batch_id}")
            batch = self._row_to_batch(batch_row)
            if batch.status is not BatchStatus.DRAFT:
                raise ConfirmationError(
                    f"batch is not a draft: {batch.status}"
                )
            job_rows = connection.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY source_row",
                (batch_id,),
            ).fetchall()
            jobs = [self._row_to_job(row) for row in job_rows]
            if not jobs:
                raise ConfirmationError("batch has no jobs")
            if any(job.status is not JobStatus.DRAFT for job in jobs):
                raise ConfirmationError("all batch jobs must be drafts")
            if any(job.batch_id != batch_id for job in jobs):
                raise ConfirmationError("batch job reference mismatch")
            if any(not fingerprints.get(job.id) for job in jobs):
                raise ConfirmationError(
                    "every batch job must have a confirmation fingerprint"
                )
            if any(
                job.visibility not in {
                    Visibility.PUBLIC,
                    Visibility.PRIVATE,
                }
                for job in jobs
            ):
                raise ConfirmationError(
                    "followers visibility is not supported yet"
                )

            if not allow_over_limit:
                local_date = batch.local_date
                count = self._count_for_local_date(
                    connection,
                    local_date,
                    batch.timezone,
                )
                if count + len(jobs) > daily_limit:
                    raise ConfirmationError("daily limit reached")

            ordered = sorted(jobs, key=lambda job: job.publish_at_utc)
            for previous, current in itertools.pairwise(ordered):
                gap = (
                    current.publish_at_utc - previous.publish_at_utc
                ).total_seconds()
                if gap <= 0:
                    raise ConfirmationError(
                        "batch publish times must be unique and ordered"
                    )
                if gap < minimum_gap_seconds:
                    raise ConfirmationError(
                        f"publish time conflicts within minimum gap: "
                        f"{previous.id} -> {current.id}"
                    )

            for job in jobs:
                conflict = self._find_gap_conflict(
                    connection,
                    job.publish_at_utc,
                    minimum_gap_seconds,
                )
                if conflict is not None and conflict.batch_id != batch_id:
                    raise ConfirmationError(
                        f"publish time conflicts with job {conflict.id} "
                        "within minimum gap"
                    )

            for job in jobs:
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        fingerprint = ?,
                        confirmed_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        JobStatus.APPROVED.value,
                        fingerprints[job.id],
                        self._to_iso(confirmed_at),
                        job.id,
                        JobStatus.DRAFT.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ConfirmationError(
                        "job state changed while confirming batch"
                    )
            cursor = connection.execute(
                """
                UPDATE batches
                SET status = ?, approved_at = ?, last_error = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    BatchStatus.APPROVED.value,
                    self._to_iso(confirmed_at),
                    batch_id,
                    BatchStatus.DRAFT.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfirmationError("batch changed while confirming")
            confirmed_rows = connection.execute(
                "SELECT * FROM jobs WHERE batch_id = ? ORDER BY publish_at_utc",
                (batch_id,),
            ).fetchall()
            return [self._row_to_job(row) for row in confirmed_rows]

    def _sql_value(self, value: object) -> object:
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        if isinstance(value, Path):
            return str(value)
        return value

    def transition(
        self,
        job_id: str,
        from_statuses: set[JobStatus],
        to_status: JobStatus,
        **fields: object,
    ) -> bool:
        placeholders = ",".join("?" for _ in from_statuses)
        assignments = ["status = ?"]
        values: list[object] = [to_status.value]
        for key, value in fields.items():
            if key not in {
                "confirmed_at",
                "last_attempt_at",
                "published_at",
                "weibo_mid",
                "weibo_url",
                "last_error",
                "artifact_dir",
                "fingerprint",
            }:
                raise ValueError(f"Unsupported transition field: {key}")
            assignments.append(f"{key} = ?")
            values.append(self._sql_value(value))
        values.extend([job_id, *(status.value for status in from_statuses)])
        sql = (
            f"UPDATE jobs SET {', '.join(assignments)} "
            f"WHERE id = ? AND status IN ({placeholders})"
        )
        with self._connect() as connection:
            cursor = connection.execute(sql, values)
            return cursor.rowcount == 1

    def count_for_local_date(
        self,
        local_date: date,
        timezone_name: str,
        exclude_job_id: str | None = None,
    ) -> int:
        with self._connect() as connection:
            return self._count_for_local_date(
                connection,
                local_date,
                timezone_name,
                exclude_job_id,
            )

    def _count_for_local_date(
        self,
        connection: sqlite3.Connection,
        local_date: date,
        timezone_name: str,
        exclude_job_id: str | None = None,
    ) -> int:
        start_local = datetime.combine(
            local_date,
            datetime.min.time(),
            tzinfo=ZoneInfo(timezone_name),
        )
        start_utc = start_local.astimezone(UTC)
        end_utc = (start_local + timedelta(days=1)).astimezone(UTC)
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        exclusion = " AND id != ?" if exclude_job_id is not None else ""
        parameters: list[object] = [
            *(status.value for status in ACTIVE_STATUSES),
            start_utc.isoformat(),
            end_utc.isoformat(),
        ]
        if exclude_job_id is not None:
            parameters.append(exclude_job_id)
        row = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM jobs
            WHERE status IN ({placeholders})
              AND rehearsal = 0
              AND publish_at_utc >= ?
              AND publish_at_utc < ?
              {exclusion}
            """,
            parameters,
        ).fetchone()
        return int(row[0])

    def find_gap_conflict(
        self,
        publish_at_utc: datetime,
        gap_seconds: int,
    ) -> Job | None:
        with self._connect() as connection:
            return self._find_gap_conflict(
                connection,
                publish_at_utc,
                gap_seconds,
            )

    def _find_gap_conflict(
        self,
        connection: sqlite3.Connection,
        publish_at_utc: datetime,
        gap_seconds: int,
    ) -> Job | None:
        start = publish_at_utc - timedelta(seconds=gap_seconds)
        end = publish_at_utc + timedelta(seconds=gap_seconds)
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = connection.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE status IN ({placeholders})
              AND rehearsal = 0
              AND publish_at_utc > ?
              AND publish_at_utc < ?
            ORDER BY publish_at_utc
            LIMIT 1
            """,
            [
                *(status.value for status in ACTIVE_STATUSES),
                start.isoformat(),
                end.isoformat(),
            ],
        ).fetchone()
        return self._row_to_job(row) if row else None

    def confirm_job(
        self,
        job_id: str,
        fingerprint: str,
        confirmed_at: datetime,
        *,
        daily_limit: int,
        minimum_gap_seconds: int,
        allow_over_limit: bool,
    ) -> Job:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ConfirmationError(f"job not found: {job_id}")

            job = self._row_to_job(row)
            if job.status is not JobStatus.DRAFT:
                raise ConfirmationError(f"job is not a draft: {job.status}")
            if job.visibility not in {Visibility.PUBLIC, Visibility.PRIVATE}:
                raise ConfirmationError(
                    "followers visibility is not supported yet"
                )

            if not allow_over_limit:
                local_date = job.publish_at_utc.astimezone(
                    ZoneInfo(job.timezone)
                ).date()
                count = self._count_for_local_date(
                    connection,
                    local_date,
                    job.timezone,
                    exclude_job_id=job.id,
                )
                if count >= daily_limit:
                    raise ConfirmationError("daily limit reached")

            conflict = self._find_gap_conflict(
                connection,
                job.publish_at_utc,
                minimum_gap_seconds,
            )
            if conflict is not None and conflict.id != job.id:
                raise ConfirmationError(
                    f"publish time conflicts with job {conflict.id} "
                    "within minimum gap"
                )

            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?,
                    fingerprint = ?,
                    confirmed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.APPROVED.value,
                    fingerprint,
                    self._to_iso(confirmed_at),
                    job_id,
                    JobStatus.DRAFT.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConfirmationError("job state changed while confirming")

            confirmed = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if confirmed is None:
                raise ConfirmationError("job disappeared while confirming")
            return self._row_to_job(confirmed)

    def record_attempt(self, job_id: str, attempt_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO attempts (attempt_id, job_id, started_at)
                VALUES (?, ?, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def finish_attempt(
        self,
        attempt_id: str,
        outcome: str,
        error: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = ?, error = ?
                WHERE attempt_id = ?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    outcome,
                    error,
                    attempt_id,
                ),
            )

    def finish_open_attempts(
        self,
        job_id: str,
        outcome: str,
        error: str | None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE attempts
                SET finished_at = ?, outcome = ?, error = ?
                WHERE job_id = ? AND finished_at IS NULL
                """,
                (
                    datetime.now(UTC).isoformat(),
                    outcome,
                    error,
                    job_id,
                ),
            )
            return cursor.rowcount

    def list_attempts(self, job_id: str) -> list[dict[str, str | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT attempt_id, started_at, finished_at, outcome, error
                FROM attempts
                WHERE job_id = ?
                ORDER BY started_at
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _insert_batch(
        self,
        connection: sqlite3.Connection,
        batch: Batch,
    ) -> None:
        connection.execute(
            """
            INSERT INTO batches (
                id,
                name,
                source_path,
                source_fingerprint,
                local_date,
                timezone,
                status,
                created_at,
                approved_at,
                last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch.id,
                batch.name,
                str(batch.source_path),
                batch.source_fingerprint,
                batch.local_date.isoformat(),
                batch.timezone,
                batch.status.value,
                self._to_iso(batch.created_at),
                self._to_iso(batch.approved_at),
                batch.last_error,
            ),
        )

    def _row_to_batch(self, row: sqlite3.Row) -> Batch:
        return Batch(
            id=row["id"],
            name=row["name"],
            source_path=Path(row["source_path"]),
            source_fingerprint=row["source_fingerprint"],
            local_date=date.fromisoformat(row["local_date"]),
            timezone=row["timezone"],
            status=BatchStatus(row["status"]),
            created_at=self._parse_iso(row["created_at"]),
            approved_at=self._parse_iso(row["approved_at"]),
            last_error=row["last_error"],
        )

    def _to_iso(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat()

    def _parse_iso(self, value: str | None) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            text=row["text"],
            images=[Path(image) for image in json.loads(row["images_json"])],
            publish_at_utc=self._parse_iso(row["publish_at_utc"]),
            timezone=row["timezone"],
            visibility=Visibility(row["visibility"]),
            status=JobStatus(row["status"]),
            fingerprint=row["fingerprint"],
            created_at=self._parse_iso(row["created_at"]),
            confirmed_at=self._parse_iso(row["confirmed_at"]),
            last_attempt_at=self._parse_iso(row["last_attempt_at"]),
            published_at=self._parse_iso(row["published_at"]),
            weibo_mid=row["weibo_mid"],
            weibo_url=row["weibo_url"],
            last_error=row["last_error"],
            artifact_dir=Path(row["artifact_dir"]) if row["artifact_dir"] else None,
            rehearsal=bool(row["rehearsal"]),
            batch_id=row["batch_id"],
            source_row=row["source_row"],
            source_sequence=row["source_sequence"],
            source_fingerprint=row["source_fingerprint"],
        )
