import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier

import pytest

import weibo_publisher.repository as repository_module
from weibo_publisher.models import Job, JobStatus, Visibility
from weibo_publisher.repository import JobRepository


def make_job() -> Job:
    return Job(
        id="job-1",
        text="可爱",
        images=[],
        publish_at_utc=datetime(2026, 11, 7, 9, 30, tzinfo=UTC),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.DRAFT,
        fingerprint="",
        created_at=datetime(2026, 11, 6, 8, 0, tzinfo=UTC),
    )


def test_create_and_read_job(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.text == "可爱"
    assert loaded.publish_at_utc == datetime(2026, 11, 7, 9, 30, tzinfo=UTC)


def test_conditional_transition_succeeds_once(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    first = repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)
    second = repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)

    assert first is True
    assert second is False
    assert repo.get_job("job-1").status is JobStatus.APPROVED


def test_list_jobs_filters_by_status(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())
    repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)

    assert [job.id for job in repo.list_jobs(JobStatus.APPROVED)] == ["job-1"]
    assert repo.list_jobs(JobStatus.DRAFT) == []


def test_count_for_local_date_excludes_cancelled(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())
    repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.CANCELLED)

    count = repo.count_for_local_date(
        date(2026, 11, 7),
        "Asia/Shanghai",
    )
    assert count == 0


def test_find_gap_conflict_returns_nearby_approved_job(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())
    repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)

    conflict = repo.find_gap_conflict(
        datetime(2026, 11, 7, 9, 31, tzinfo=UTC),
        gap_seconds=240,
    )

    assert conflict is not None
    assert conflict.id == "job-1"


def test_get_job_returns_none_for_missing_id(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()

    assert repo.get_job("missing") is None


def test_images_round_trip_as_path_objects(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()

    job = make_job()
    job.images = [Path("a.jpg"), Path("images") / "b.png"]
    repo.create_job(job)

    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.images == [Path("a.jpg"), Path("images") / "b.png"]
    assert all(isinstance(image, Path) for image in loaded.images)


def test_enum_columns_round_trip(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()

    job = make_job()
    job.visibility = Visibility.FOLLOWERS
    repo.create_job(job)

    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.status is JobStatus.DRAFT
    assert loaded.visibility is Visibility.FOLLOWERS


def test_timestamps_remain_utc_aware(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.publish_at_utc.tzinfo is UTC
    assert loaded.created_at.tzinfo is UTC
    assert loaded.confirmed_at is None


def test_transition_serializes_datetime_and_path_fields(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    published_at = datetime(2026, 11, 7, 9, 35, tzinfo=UTC)
    artifact_dir = tmp_path / "artifacts" / "job-1"

    changed = repo.transition(
        "job-1",
        {JobStatus.DRAFT},
        JobStatus.APPROVED,
        published_at=published_at,
        artifact_dir=artifact_dir,
        last_error=None,
    )

    assert changed is True
    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.published_at == published_at
    assert loaded.artifact_dir == artifact_dir
    assert loaded.last_error is None


def test_transition_rejects_unsupported_field(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    with pytest.raises(ValueError, match="Unsupported transition field"):
        repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED, bogus=1)


def test_count_for_local_date_excludes_non_active_statuses(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()

    excluded = [
        JobStatus.DRAFT,
        JobStatus.CANCELLED,
        JobStatus.MISSED,
        JobStatus.FAILED,
        JobStatus.UNKNOWN,
        JobStatus.NEEDS_ATTENTION,
    ]
    for index, status in enumerate(excluded):
        job = make_job()
        job.id = f"excluded-{index}"
        repo.create_job(job)
        if status != JobStatus.DRAFT:
            repo.transition(job.id, {JobStatus.DRAFT}, status)

    active = make_job()
    active.id = "active"
    repo.create_job(active)
    repo.transition("active", {JobStatus.DRAFT}, JobStatus.APPROVED)

    assert repo.count_for_local_date(date(2026, 11, 7), "Asia/Shanghai") == 1


def test_find_gap_conflict_ignores_non_active_statuses(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()

    failed = make_job()
    repo.create_job(failed)
    repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.FAILED)

    nearby = datetime(2026, 11, 7, 9, 31, tzinfo=UTC)
    assert repo.find_gap_conflict(nearby, gap_seconds=240) is None

    active = make_job()
    active.id = "active"
    repo.create_job(active)
    repo.transition("active", {JobStatus.DRAFT}, JobStatus.APPROVED)

    conflict = repo.find_gap_conflict(nearby, gap_seconds=240)
    assert conflict is not None
    assert conflict.id == "active"


def test_record_and_finish_attempt(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    repo.record_attempt("job-1", "attempt-1")
    repo.finish_attempt("attempt-1", "success", None)

    with sqlite3.connect(tmp_path / "state.db") as connection:
        row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?",
            ("attempt-1",),
        ).fetchone()

    assert row is not None
    attempt_id, job_id, started_at, finished_at, outcome, error = row
    assert attempt_id == "attempt-1"
    assert job_id == "job-1"
    assert datetime.fromisoformat(started_at).tzinfo is UTC
    assert datetime.fromisoformat(finished_at).tzinfo is UTC
    assert outcome == "success"
    assert error is None


def test_finish_open_attempts_only_updates_pending_rows(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())
    repo.record_attempt("job-1", "pending")
    repo.record_attempt("job-1", "finished")
    repo.finish_attempt("finished", "success", None)

    changed = repo.finish_open_attempts(
        "job-1",
        "interrupted",
        "Runner restarted",
    )

    attempts = {attempt["attempt_id"]: attempt for attempt in repo.list_attempts("job-1")}
    assert changed == 1
    assert attempts["pending"]["outcome"] == "interrupted"
    assert attempts["pending"]["error"] == "Runner restarted"
    assert attempts["pending"]["finished_at"] is not None
    assert attempts["finished"]["outcome"] == "success"
    assert attempts["finished"]["error"] is None


def test_confirmation_rejects_followers_visibility_atomically(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    job = make_job()
    job.visibility = Visibility.FOLLOWERS
    repo.create_job(job)

    with pytest.raises(
        repository_module.ConfirmationError,
        match="followers visibility is not supported yet",
    ):
        repo.confirm_job(
            "job-1",
            "confirmed-fingerprint",
            datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
            daily_limit=100,
            minimum_gap_seconds=240,
            allow_over_limit=False,
        )

    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.status is JobStatus.DRAFT
    assert loaded.fingerprint == ""


def test_confirmation_accepts_private_visibility_atomically(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    job = make_job()
    job.visibility = Visibility.PRIVATE
    repo.create_job(job)

    confirmed = repo.confirm_job(
        "job-1",
        "confirmed-fingerprint",
        datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
        daily_limit=100,
        minimum_gap_seconds=240,
        allow_over_limit=False,
    )

    assert confirmed.status is JobStatus.APPROVED
    assert confirmed.visibility is Visibility.PRIVATE
    assert confirmed.fingerprint == "confirmed-fingerprint"


def test_confirmation_is_atomic_for_daily_limit(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    first = make_job()
    second = make_job()
    second.id = "job-2"
    second.publish_at_utc = datetime(2026, 11, 7, 10, 30, tzinfo=UTC)
    repo.create_job(first)
    repo.create_job(second)

    results = _confirm_concurrently(
        repo,
        ("job-1", "job-2"),
        daily_limit=1,
        minimum_gap_seconds=240,
    )

    assert sorted(error for _, error in results if error is not None) == [
        "daily limit reached"
    ]
    approved_ids = _approved_ids(repo, "job-1", "job-2")
    assert len(approved_ids) == 1


def test_confirmation_is_atomic_for_minimum_gap(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    first = make_job()
    second = make_job()
    second.id = "job-2"
    repo.create_job(first)
    repo.create_job(second)

    results = _confirm_concurrently(
        repo,
        ("job-1", "job-2"),
        daily_limit=100,
        minimum_gap_seconds=240,
    )

    failures = [error for _, error in results if error is not None]
    assert len(failures) == 1
    assert failures[0] is not None and "within minimum gap" in failures[0]
    assert len(_approved_ids(repo, "job-1", "job-2")) == 1


def _confirm_concurrently(
    repo: JobRepository,
    job_ids: tuple[str, ...],
    *,
    daily_limit: int,
    minimum_gap_seconds: int,
) -> list[tuple[str, str | None]]:
    barrier = Barrier(len(job_ids))

    def confirm(job_id: str) -> tuple[str, str | None]:
        barrier.wait()
        try:
            repo.confirm_job(
                job_id,
                f"fingerprint-{job_id}",
                datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
                daily_limit=daily_limit,
                minimum_gap_seconds=minimum_gap_seconds,
                allow_over_limit=False,
            )
        except repository_module.ConfirmationError as exc:
            return job_id, str(exc)
        return job_id, None

    with ThreadPoolExecutor(max_workers=len(job_ids)) as pool:
        return list(pool.map(confirm, job_ids))


def _approved_ids(repo: JobRepository, *job_ids: str) -> list[str]:
    approved: list[str] = []
    for job_id in job_ids:
        job = repo.get_job(job_id)
        if job is not None and job.status is JobStatus.APPROVED:
            approved.append(job.id)
    return approved
