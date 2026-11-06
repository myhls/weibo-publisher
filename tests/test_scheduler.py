import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from weibo_publisher.config import AppConfig
from weibo_publisher.models import (
    AttemptOutcome,
    Job,
    JobStatus,
    PublishResult,
    Visibility,
)
from weibo_publisher.repository import JobRepository
from weibo_publisher.scheduler import JobExecutor, Runner, RunnerLockError
from weibo_publisher.validation import build_fingerprint


class FakePublisher:
    def __init__(
        self,
        result: PublishResult | None = None,
        *,
        prepare_error: Exception | None = None,
        submit_error: Exception | None = None,
    ) -> None:
        self.result = result or PublishResult(outcome=AttemptOutcome.SUCCESS)
        self.prepare_error = prepare_error
        self.submit_error = submit_error
        self.prepare_calls = 0
        self.submit_calls = 0
        self.close_calls = 0

    async def prepare(self, job: Job, artifact_dir: Path) -> None:
        del job, artifact_dir
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error

    async def submit(self, job: Job, artifact_dir: Path) -> PublishResult:
        del job, artifact_dir
        self.submit_calls += 1
        if self.submit_error is not None:
            raise self.submit_error
        return self.result

    async def close(self) -> None:
        self.close_calls += 1


def approved_job(
    *,
    seconds_from_now: float = 60,
    image: Path | None = None,
    fingerprint: str | None = None,
    timezone: str = "Asia/Shanghai",
) -> Job:
    target = datetime.now(UTC) + timedelta(seconds=seconds_from_now)
    return approved_job_at(
        target,
        image=image,
        fingerprint=fingerprint,
        timezone=timezone,
    )


def approved_job_at(
    target: datetime,
    *,
    image: Path | None = None,
    fingerprint: str | None = None,
    timezone: str = "Asia/Shanghai",
) -> Job:
    images = [image] if image is not None else []
    job = Job(
        id="job-1",
        text="可爱",
        images=images,
        publish_at_utc=target,
        timezone=timezone,
        visibility=Visibility.PUBLIC,
        status=JobStatus.APPROVED,
        fingerprint="",
        created_at=datetime.now(UTC),
        confirmed_at=datetime.now(UTC),
    )
    job.fingerprint = fingerprint or build_fingerprint(
        job.text,
        job.images,
        job.publish_at_utc,
        job.visibility,
    )
    return job


def repository_with(config: AppConfig, *jobs: Job) -> JobRepository:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    repo = JobRepository(config.db_path)
    repo.initialize()
    for job in jobs:
        repo.create_job(job)
    return repo


@pytest.mark.asyncio
async def test_success_records_attempt_and_publishes(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=5))
    fake = FakePublisher(
        PublishResult(
            outcome=AttemptOutcome.SUCCESS,
            weibo_mid="123",
            weibo_url="https://weibo.com/detail/123",
        )
    )
    sleep_delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr("weibo_publisher.scheduler.asyncio.sleep", record_sleep)

    await JobExecutor(app_config, repo, lambda: fake).execute("job-1")

    job = repo.get_job("job-1")
    attempts = repo.list_attempts("job-1")
    assert job is not None
    assert job.status is JobStatus.PUBLISHED
    assert job.weibo_mid == "123"
    assert job.weibo_url == "https://weibo.com/detail/123"
    assert job.published_at is not None
    assert job.artifact_dir is not None and job.artifact_dir.is_dir()
    assert fake.prepare_calls == 1
    assert fake.submit_calls == 1
    assert fake.close_calls == 1
    assert sleep_delays and sleep_delays[0] > 0
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == AttemptOutcome.SUCCESS.value


@pytest.mark.asyncio
async def test_definite_failure_records_failed_outcome(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=5))
    fake = FakePublisher(
        PublishResult(outcome=AttemptOutcome.FAILED, error="network rejected")
    )

    async def skip_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("weibo_publisher.scheduler.asyncio.sleep", skip_sleep)

    await JobExecutor(app_config, repo, lambda: fake).execute("job-1")

    job = repo.get_job("job-1")
    attempts = repo.list_attempts("job-1")
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.last_error == "network rejected"
    assert fake.close_calls == 1
    assert attempts[0]["outcome"] == AttemptOutcome.FAILED.value
    assert attempts[0]["error"] == "network rejected"


@pytest.mark.asyncio
async def test_unknown_result_is_not_retried(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=5))
    fake = FakePublisher(
        PublishResult(outcome=AttemptOutcome.UNKNOWN, error="timeout")
    )
    factory_calls = 0

    def factory() -> FakePublisher:
        nonlocal factory_calls
        factory_calls += 1
        return fake

    async def skip_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("weibo_publisher.scheduler.asyncio.sleep", skip_sleep)

    executor = JobExecutor(app_config, repo, factory)
    await executor.execute("job-1")
    await executor.execute("job-1")

    job = repo.get_job("job-1")
    attempts = repo.list_attempts("job-1")
    assert job is not None
    assert job.status is JobStatus.UNKNOWN
    assert job.last_error == "timeout"
    assert fake.submit_calls == 1
    assert factory_calls == 1
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == AttemptOutcome.UNKNOWN.value


@pytest.mark.asyncio
async def test_late_job_is_missed_without_creating_publisher(
    app_config: AppConfig,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=-31))
    factory_calls = 0

    def factory() -> FakePublisher:
        nonlocal factory_calls
        factory_calls += 1
        return FakePublisher()

    await JobExecutor(app_config, repo, factory).execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.MISSED
    assert job.last_error is not None
    assert "late" in job.last_error
    assert factory_calls == 0
    assert repo.list_attempts("job-1") == []


@pytest.mark.asyncio
async def test_fingerprint_mismatch_needs_attention(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"first")
    repo = repository_with(
        app_config,
        approved_job(seconds_from_now=60, image=image, fingerprint="stale"),
    )
    factory_calls = 0

    def factory() -> FakePublisher:
        nonlocal factory_calls
        factory_calls += 1
        return FakePublisher()

    await JobExecutor(app_config, repo, factory).execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.NEEDS_ATTENTION
    assert job.last_error is not None
    assert "fingerprint" in job.last_error.lower()
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_missing_image_needs_attention(
    app_config: AppConfig,
    tmp_path: Path,
) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"first")
    repo = repository_with(
        app_config,
        approved_job(seconds_from_now=60, image=image),
    )
    image.unlink()

    await JobExecutor(app_config, repo, lambda: FakePublisher()).execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.NEEDS_ATTENTION
    assert job.last_error is not None
    assert "does not exist" in job.last_error


@pytest.mark.asyncio
async def test_invalid_timezone_needs_attention(
    app_config: AppConfig,
) -> None:
    repo = repository_with(
        app_config,
        approved_job(seconds_from_now=60, timezone="Mars/Olympus"),
    )
    factory_calls = 0

    def factory() -> FakePublisher:
        nonlocal factory_calls
        factory_calls += 1
        return FakePublisher()

    await JobExecutor(app_config, repo, factory).execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.NEEDS_ATTENTION
    assert job.last_error is not None
    assert "timezone" in job.last_error.lower()
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_daily_limit_needs_attention_but_job_does_not_count_itself(
    app_config: AppConfig,
) -> None:
    config = replace(app_config, daily_limit=1)
    zone = ZoneInfo("Asia/Shanghai")
    local_noon = (datetime.now(zone) + timedelta(days=1)).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    current = approved_job_at(local_noon.astimezone(UTC))
    other = approved_job_at((local_noon + timedelta(hours=1)).astimezone(UTC))
    other.id = "job-2"
    repo = repository_with(config, current, other)
    factory_calls = 0

    def factory() -> FakePublisher:
        nonlocal factory_calls
        factory_calls += 1
        return FakePublisher()

    await JobExecutor(config, repo, factory).execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.NEEDS_ATTENTION
    assert job.last_error is not None
    assert "daily limit" in job.last_error.lower()
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_daily_limit_ignored_when_allow_over_limit(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(app_config, daily_limit=1, allow_over_limit=True)
    current = approved_job(seconds_from_now=5)
    other = approved_job(seconds_from_now=120)
    other.id = "job-2"
    repo = repository_with(config, current, other)

    async def skip_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("weibo_publisher.scheduler.asyncio.sleep", skip_sleep)

    await JobExecutor(config, repo, lambda: FakePublisher()).execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.PUBLISHED


@pytest.mark.asyncio
async def test_invalid_timezone_still_rejected_when_over_limit_allowed(
    app_config: AppConfig,
) -> None:
    config = replace(app_config, allow_over_limit=True)
    repo = repository_with(
        config,
        approved_job(seconds_from_now=60, timezone="Mars/Olympus"),
    )

    await JobExecutor(config, repo, lambda: FakePublisher()).execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.NEEDS_ATTENTION
    assert job.last_error is not None
    assert "timezone" in job.last_error.lower()


@pytest.mark.asyncio
async def test_prepare_exception_is_failed_and_closes_once(
    app_config: AppConfig,
) -> None:
    repo = repository_with(app_config, approved_job())
    fake = FakePublisher(prepare_error=RuntimeError("composer unavailable"))

    await JobExecutor(app_config, repo, lambda: fake).execute("job-1")

    job = repo.get_job("job-1")
    attempts = repo.list_attempts("job-1")
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.last_error == "composer unavailable"
    assert fake.submit_calls == 0
    assert fake.close_calls == 1
    assert attempts[0]["outcome"] == AttemptOutcome.FAILED.value


@pytest.mark.asyncio
async def test_submit_exception_is_unknown_and_closes_once(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=5))
    fake = FakePublisher(submit_error=RuntimeError("connection lost"))

    async def skip_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("weibo_publisher.scheduler.asyncio.sleep", skip_sleep)

    await JobExecutor(app_config, repo, lambda: fake).execute("job-1")

    job = repo.get_job("job-1")
    attempts = repo.list_attempts("job-1")
    assert job is not None
    assert job.status is JobStatus.UNKNOWN
    assert job.last_error == "connection lost"
    assert fake.close_calls == 1
    assert attempts[0]["outcome"] == AttemptOutcome.UNKNOWN.value


@pytest.mark.asyncio
async def test_attempt_exists_before_publisher_prepare(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=5))

    class InspectingPublisher(FakePublisher):
        async def prepare(self, job: Job, artifact_dir: Path) -> None:
            attempts = repo.list_attempts(job.id)
            assert len(attempts) == 1
            assert attempts[0]["attempt_id"] == artifact_dir.name
            assert attempts[0]["finished_at"] is None
            assert attempts[0]["outcome"] is None
            await super().prepare(job, artifact_dir)

    async def skip_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("weibo_publisher.scheduler.asyncio.sleep", skip_sleep)

    await JobExecutor(
        app_config,
        repo,
        lambda: InspectingPublisher(),
    ).execute("job-1")


@pytest.mark.asyncio
async def test_second_execution_cannot_publish_again(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=5))
    fake = FakePublisher()
    factory_calls = 0

    def factory() -> FakePublisher:
        nonlocal factory_calls
        factory_calls += 1
        return fake

    async def skip_sleep(delay: float) -> None:
        del delay

    monkeypatch.setattr("weibo_publisher.scheduler.asyncio.sleep", skip_sleep)

    executor = JobExecutor(app_config, repo, factory)
    await executor.execute("job-1")
    await executor.execute("job-1")

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.PUBLISHED
    assert factory_calls == 1
    assert fake.submit_calls == 1
    assert len(repo.list_attempts("job-1")) == 1


@pytest.mark.asyncio
async def test_late_cutoff_is_rechecked_after_wait(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(app_config, late_cutoff_seconds=0)
    clock_now = datetime.now(UTC)
    repo = repository_with(
        config,
        approved_job_at(clock_now + timedelta(seconds=10)),
    )
    fake = FakePublisher()

    class FakeClock(datetime):
        current = clock_now

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current.astimezone(tz)

    async def advance_clock(delay: float) -> None:
        FakeClock.current += timedelta(seconds=delay + 1)

    monkeypatch.setattr("weibo_publisher.scheduler.datetime", FakeClock)
    monkeypatch.setattr(
        "weibo_publisher.scheduler.asyncio.sleep",
        advance_clock,
    )

    await JobExecutor(config, repo, lambda: fake).execute("job-1")

    job = repo.get_job("job-1")
    attempts = repo.list_attempts("job-1")
    assert job is not None
    assert job.status is JobStatus.MISSED
    assert fake.submit_calls == 0
    assert fake.close_calls == 1
    assert attempts[0]["outcome"] == AttemptOutcome.INTERRUPTED.value


@pytest.mark.asyncio
async def test_non_approved_job_is_ignored(
    app_config: AppConfig,
) -> None:
    job = approved_job()
    job.status = JobStatus.DRAFT
    repo = repository_with(app_config, job)
    factory_calls = 0

    def factory() -> FakePublisher:
        nonlocal factory_calls
        factory_calls += 1
        return FakePublisher()

    await JobExecutor(app_config, repo, factory).execute("job-1")

    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.status is JobStatus.DRAFT
    assert loaded.last_attempt_at is None
    assert factory_calls == 0
    assert repo.list_attempts("job-1") == []


@pytest.mark.asyncio
async def test_scan_ignores_draft_jobs(app_config: AppConfig) -> None:
    job = approved_job()
    job.status = JobStatus.DRAFT
    repo = repository_with(app_config, job)
    calls: list[str] = []

    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(),
        executor_factory=lambda job_id: calls.append(job_id),
    )

    await runner.scan_once()

    assert calls == []
    assert runner.scheduled == set()
    assert runner.scheduler.timezone.key == app_config.default_timezone


@pytest.mark.asyncio
async def test_scan_schedules_approved_job_once(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=10))
    added: list[dict[str, object]] = []

    def record_add_job(callback, trigger, **kwargs) -> None:
        added.append(
            {
                "callback": callback,
                "trigger": trigger,
                **kwargs,
            }
        )

    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(),
        executor_factory=lambda job_id: None,
    )
    monkeypatch.setattr(runner.scheduler, "add_job", record_add_job)

    await runner.scan_once()
    await runner.scan_once()

    assert runner.scheduled == {"job-1"}
    assert len(added) == 1
    assert added[0]["id"] == "job:job-1"
    assert added[0]["trigger"] == "date"
    run_date = added[0]["run_date"]
    assert isinstance(run_date, datetime)
    assert timedelta(seconds=4) <= run_date - datetime.now(UTC) <= timedelta(seconds=6)


@pytest.mark.asyncio
async def test_scan_uses_prepare_lead_for_future_job(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_now = datetime.now(UTC)
    job = approved_job_at(clock_now + timedelta(seconds=600))
    repo = repository_with(app_config, job)
    added: dict[str, object] = {}

    def record_add_job(callback, trigger, **kwargs) -> None:
        added.update({"callback": callback, "trigger": trigger, **kwargs})

    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(),
        executor_factory=lambda job_id: None,
        clock=lambda: clock_now,
    )
    monkeypatch.setattr(runner.scheduler, "add_job", record_add_job)

    await runner.scan_once()

    assert added["run_date"] == job.publish_at_utc - timedelta(
        seconds=app_config.prepare_lead_seconds
    )


@pytest.mark.asyncio
async def test_scan_marks_late_approved_job_missed(app_config: AppConfig) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=-31))
    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(),
        executor_factory=lambda job_id: None,
    )

    await runner.scan_once()

    job = repo.get_job("job-1")
    assert job is not None
    assert job.status is JobStatus.MISSED
    assert job.last_error is not None
    assert "late" in job.last_error.lower()
    assert runner.scheduled == set()


@pytest.mark.asyncio
async def test_scan_removes_job_that_is_no_longer_approved(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repository_with(app_config, approved_job(seconds_from_now=600))
    removed: list[str] = []

    def record_add_job(callback, trigger, **kwargs) -> None:
        del callback, trigger, kwargs

    def record_remove_job(job_id: str) -> None:
        removed.append(job_id)

    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(),
        executor_factory=lambda job_id: None,
    )
    monkeypatch.setattr(runner.scheduler, "add_job", record_add_job)
    monkeypatch.setattr(runner.scheduler, "remove_job", record_remove_job)

    await runner.scan_once()
    assert repo.transition(
        "job-1",
        {JobStatus.APPROVED},
        JobStatus.CANCELLED,
    )

    await runner.scan_once()

    assert runner.scheduled == set()
    assert removed == ["job:job-1"]


@pytest.mark.asyncio
async def test_runner_lock_refuses_second_runner(app_config: AppConfig) -> None:
    repo = repository_with(app_config)
    first = Runner(app_config, repo, lambda: FakePublisher())
    second = Runner(app_config, repo, lambda: FakePublisher())
    first.acquire_lock()
    try:
        with pytest.raises(RunnerLockError, match="already running"):
            second.acquire_lock()
    finally:
        first.release_lock()

    assert not (app_config.runtime_dir / "runner.lock").exists()


@pytest.mark.asyncio
async def test_runner_releases_lock_on_normal_exit(app_config: AppConfig) -> None:
    repo = repository_with(app_config)
    runner = Runner(app_config, repo, lambda: FakePublisher())
    stop_event = asyncio.Event()
    stop_event.set()

    await runner.run_forever(stop_event=stop_event)

    assert not (app_config.runtime_dir / "runner.lock").exists()
    assert runner.scheduler.state == 0


@pytest.mark.asyncio
async def test_runner_releases_lock_when_loop_raises(app_config: AppConfig) -> None:
    repo = repository_with(app_config)

    async def fail_sleep(delay: float) -> None:
        assert delay == 5
        raise RuntimeError("stop after first scan")

    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(),
        sleep=fail_sleep,
    )

    with pytest.raises(RuntimeError, match="stop after first scan"):
        await runner.run_forever()

    assert not (app_config.runtime_dir / "runner.lock").exists()
    assert runner.scheduler.state == 0


@pytest.mark.asyncio
async def test_recovery_marks_interrupted_jobs_for_attention(
    app_config: AppConfig,
) -> None:
    publishing = approved_job()
    publishing.id = "publishing"
    publishing.status = JobStatus.PUBLISHING
    preparing = approved_job(seconds_from_now=600)
    preparing.id = "preparing"
    preparing.status = JobStatus.PREPARING
    ready = approved_job(seconds_from_now=600)
    ready.id = "ready"
    ready.status = JobStatus.READY
    repo = repository_with(app_config, publishing, preparing, ready)
    runner = Runner(app_config, repo, lambda: FakePublisher())

    await runner.recover_interrupted()

    recovered_publishing = repo.get_job("publishing")
    recovered_preparing = repo.get_job("preparing")
    recovered_ready = repo.get_job("ready")
    assert recovered_publishing is not None
    assert recovered_publishing.status is JobStatus.UNKNOWN
    assert recovered_publishing.last_error == "Runner restarted during submission"
    assert recovered_preparing is not None
    assert recovered_preparing.status is JobStatus.NEEDS_ATTENTION
    assert recovered_preparing.last_error is not None
    assert "browser context" in recovered_preparing.last_error.lower()
    assert recovered_ready is not None
    assert recovered_ready.status is JobStatus.NEEDS_ATTENTION
    assert recovered_ready.last_error is not None
    assert "browser context" in recovered_ready.last_error.lower()


@pytest.mark.asyncio
async def test_run_forever_recovers_before_exiting(app_config: AppConfig) -> None:
    publishing = approved_job()
    publishing.id = "publishing"
    publishing.status = JobStatus.PUBLISHING
    repo = repository_with(app_config, publishing)
    runner = Runner(app_config, repo, lambda: FakePublisher())
    stop_event = asyncio.Event()
    stop_event.set()

    await runner.run_forever(stop_event=stop_event)

    job = repo.get_job("publishing")
    assert job is not None
    assert job.status is JobStatus.UNKNOWN
