from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from weibo_publisher.models import Job, JobStatus, Visibility

ACTIVE_STATUSES = {
    JobStatus.APPROVED,
    JobStatus.PREPARING,
    JobStatus.READY,
    JobStatus.PUBLISHING,
    JobStatus.PUBLISHED,
}


_SCHEMA = """
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
    artifact_dir TEXT
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
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def create_job(self, job: Job) -> None:
        with self._connect() as connection:
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
                    artifact_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def count_for_local_date(self, local_date: date, timezone_name: str) -> int:
        start_local = datetime.combine(
            local_date,
            datetime.min.time(),
            tzinfo=ZoneInfo(timezone_name),
        )
        start_utc = start_local.astimezone(UTC)
        end_utc = (start_local + timedelta(days=1)).astimezone(UTC)
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM jobs
                WHERE status IN ({placeholders})
                  AND publish_at_utc >= ?
                  AND publish_at_utc < ?
                """,
                [
                    *(status.value for status in ACTIVE_STATUSES),
                    start_utc.isoformat(),
                    end_utc.isoformat(),
                ],
            ).fetchone()
        return int(row[0])

    def find_gap_conflict(
        self,
        publish_at_utc: datetime,
        gap_seconds: int,
    ) -> Job | None:
        start = publish_at_utc - timedelta(seconds=gap_seconds)
        end = publish_at_utc + timedelta(seconds=gap_seconds)
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT *
                FROM jobs
                WHERE status IN ({placeholders})
                  AND publish_at_utc BETWEEN ? AND ?
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
        )
