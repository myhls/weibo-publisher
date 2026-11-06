from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError

from weibo_publisher.config import AppConfig
from weibo_publisher.models import (
    AttemptOutcome,
    Job,
    JobStatus,
    PublishResult,
    Visibility,
)
from weibo_publisher.publisher import Publisher
from weibo_publisher.repository import JobRepository
from weibo_publisher.validation import build_fingerprint, validate_images

SCAN_INTERVAL_SECONDS = 5
LOCK_BYTE_COUNT = 1


class RunnerLockError(RuntimeError):
    pass


class JobExecutor:
    def __init__(
        self,
        config: AppConfig,
        repo: JobRepository,
        publisher_factory: Callable[[], Publisher],
    ) -> None:
        self.config = config
        self.repo = repo
        self.publisher_factory = publisher_factory

    async def execute(self, job_id: str) -> None:
        job = self.repo.get_job(job_id)
        if job is None:
            return

        cutoff = job.publish_at_utc + timedelta(
            seconds=self.config.late_cutoff_seconds
        )
        now = datetime.now(UTC)
        if now >= job.publish_at_utc:
            self.repo.transition(
                job_id,
                {JobStatus.APPROVED},
                JobStatus.MISSED,
                last_error=(
                    "Job was too late: target time passed before preparation started"
                ),
            )
            return

        started_at = datetime.now(UTC)
        attempt_id = uuid4().hex
        artifact_dir = self.config.artifacts_dir / job_id / attempt_id
        if not self.repo.transition(
            job_id,
            {JobStatus.APPROVED},
            JobStatus.PREPARING,
            last_attempt_at=started_at,
            last_error=None,
            artifact_dir=artifact_dir,
        ):
            return

        try:
            self._validate_preparation(job)
        except (OSError, ValueError) as exc:
            self.repo.transition(
                job_id,
                {JobStatus.PREPARING},
                JobStatus.NEEDS_ATTENTION,
                last_error=str(exc),
            )
            return

        publisher: Publisher | None = None
        attempt_recorded = False
        submit_started = False

        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            self.repo.record_attempt(job_id, attempt_id)
            attempt_recorded = True
            publisher = self.publisher_factory()

            await publisher.prepare(job, artifact_dir)
            if not self.repo.transition(
                job_id,
                {JobStatus.PREPARING},
                JobStatus.READY,
                artifact_dir=artifact_dir,
            ):
                raise RuntimeError("Job was not preparing after publisher.prepare()")

            delay = (
                job.publish_at_utc - datetime.now(UTC)
            ).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

            if datetime.now(UTC) > cutoff:
                self.repo.transition(
                    job_id,
                    {JobStatus.READY},
                    JobStatus.MISSED,
                    last_error="Job became too late after publisher preparation",
                )
                self.repo.finish_attempt(
                    attempt_id,
                    AttemptOutcome.INTERRUPTED.value,
                    "Job became too late after publisher preparation",
                )
                return

            if not self.repo.transition(
                job_id,
                {JobStatus.READY},
                JobStatus.PUBLISHING,
            ):
                self.repo.finish_attempt(
                    attempt_id,
                    AttemptOutcome.INTERRUPTED.value,
                    "Job was not ready to publish",
                )
                return

            submit_started = True
            result = await publisher.submit(job, artifact_dir)
            self._record_result(job_id, attempt_id, result)
        except asyncio.CancelledError:
            if attempt_recorded:
                if submit_started:
                    self.repo.transition(
                        job_id,
                        {JobStatus.PUBLISHING},
                        JobStatus.UNKNOWN,
                        last_error="Execution was cancelled after submission started",
                    )
                    self.repo.finish_attempt(
                        attempt_id,
                        AttemptOutcome.UNKNOWN.value,
                        "Execution was cancelled after submission started",
                    )
                else:
                    self.repo.transition(
                        job_id,
                        {JobStatus.PREPARING, JobStatus.READY},
                        JobStatus.NEEDS_ATTENTION,
                        last_error="Execution was cancelled before submission started",
                    )
                    self.repo.finish_attempt(
                        attempt_id,
                        AttemptOutcome.INTERRUPTED.value,
                        "Execution was cancelled before submission started",
                    )
            raise
        except Exception as exc:  # noqa: BLE001 - submit failures must stop retries
            if submit_started:
                self.repo.transition(
                    job_id,
                    {JobStatus.PUBLISHING},
                    JobStatus.UNKNOWN,
                    last_error=str(exc),
                )
                outcome = AttemptOutcome.UNKNOWN
            else:
                if not self.repo.transition(
                    job_id,
                    {JobStatus.PREPARING},
                    JobStatus.FAILED,
                    last_error=str(exc),
                ):
                    self.repo.transition(
                        job_id,
                        {JobStatus.READY},
                        JobStatus.NEEDS_ATTENTION,
                        last_error=str(exc),
                    )
                outcome = AttemptOutcome.FAILED
            if attempt_recorded:
                self.repo.finish_attempt(attempt_id, outcome.value, str(exc))
        finally:
            if publisher is not None:
                await publisher.close()

    def _validate_preparation(self, job: Job) -> None:
        if job.visibility not in {Visibility.PUBLIC, Visibility.PRIVATE}:
            raise ValueError("followers visibility is not supported yet")

        images = validate_images(job.images)
        fingerprint = build_fingerprint(
            job.text,
            images,
            job.publish_at_utc,
            job.visibility,
        )
        if fingerprint != job.fingerprint:
            raise ValueError(
                "Job fingerprint does not match its confirmed content"
            )

        try:
            local_date = job.publish_at_utc.astimezone(
                ZoneInfo(job.timezone)
            ).date()
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Invalid timezone for job scheduling: {job.timezone}"
            ) from exc

        if not self.config.allow_over_limit:
            count = self.repo.count_for_local_date(
                local_date,
                job.timezone,
                exclude_job_id=job.id,
            )
            if count >= self.config.daily_limit:
                raise ValueError(
                    f"Daily limit of {self.config.daily_limit} reached"
                )

    def _record_result(
        self,
        job_id: str,
        attempt_id: str,
        result: PublishResult,
    ) -> None:
        if result.outcome is AttemptOutcome.SUCCESS:
            self.repo.transition(
                job_id,
                {JobStatus.PUBLISHING},
                JobStatus.PUBLISHED,
                published_at=datetime.now(UTC),
                weibo_mid=result.weibo_mid,
                weibo_url=result.weibo_url,
                last_error=None,
            )
            self.repo.finish_attempt(
                attempt_id,
                AttemptOutcome.SUCCESS.value,
                None,
            )
        elif result.outcome is AttemptOutcome.UNKNOWN:
            self.repo.transition(
                job_id,
                {JobStatus.PUBLISHING},
                JobStatus.UNKNOWN,
                last_error=result.error,
            )
            self.repo.finish_attempt(
                attempt_id,
                AttemptOutcome.UNKNOWN.value,
                result.error,
            )
        else:
            self.repo.transition(
                job_id,
                {JobStatus.PUBLISHING},
                JobStatus.FAILED,
                last_error=result.error,
            )
            self.repo.finish_attempt(
                attempt_id,
                AttemptOutcome.FAILED.value,
                result.error,
            )


class Runner:
    def __init__(
        self,
        config: AppConfig,
        repo: JobRepository,
        publisher_factory: Callable[[], Publisher],
        executor_factory: Callable[[str], object] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.publisher_factory = publisher_factory
        self.executor_factory = executor_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or asyncio.sleep
        self.scheduler = AsyncIOScheduler(timezone=config.default_timezone)
        self._scheduled_job_ids: set[str] = set()
        self.scheduled = self._scheduled_job_ids
        self._lock_path = config.runtime_dir / "runner.lock"
        self._lock_file: BinaryIO | None = None

    def acquire_lock(self) -> None:
        if self._lock_file is not None:
            raise RunnerLockError(
                f"Runner lock is already held by this process for {self.config.root}"
            )
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        lock_file: BinaryIO | None = None
        try:
            lock_file = self._lock_path.open("a+b")
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            self._acquire_os_lock(lock_file)
        except BaseException:
            if lock_file is not None:
                try:
                    lock_file.close()
                except OSError:
                    pass
            raise
        self._lock_file = lock_file

    def _acquire_os_lock(self, lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(
                    lock_file.fileno(),
                    msvcrt.LK_NBLCK,
                    LOCK_BYTE_COUNT,
                )
            except OSError as exc:
                raise RunnerLockError(
                    f"Another runner is already running for {self.config.root}"
                ) from exc
            return

        import fcntl

        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            raise RunnerLockError(
                f"Another runner is already running for {self.config.root}"
            ) from exc

    def release_lock(self) -> None:
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(
                    lock_file.fileno(),
                    msvcrt.LK_UNLCK,
                    LOCK_BYTE_COUNT,
                )
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                lock_file.close()
            except OSError:
                pass

    async def scan_once(self) -> None:
        approved_jobs = {
            job.id: job for job in self.repo.list_jobs(JobStatus.APPROVED)
        }
        self._remove_inactive_jobs(approved_jobs)

        now = self._now()
        for job in approved_jobs.values():
            if job.id in self._scheduled_job_ids:
                continue

            if now >= job.publish_at_utc:
                self.repo.transition(
                    job.id,
                    {JobStatus.APPROVED},
                    JobStatus.MISSED,
                    last_error=(
                        "Job was too late: target time passed before preparation started"
                    ),
                )
                continue

            prepare_at = job.publish_at_utc - timedelta(
                seconds=self.config.prepare_lead_seconds
            )
            run_at = max(
                now + timedelta(seconds=SCAN_INTERVAL_SECONDS),
                prepare_at,
            )
            self.scheduler.add_job(
                self._executor(job.id),
                "date",
                run_date=run_at,
                id=self._scheduler_job_id(job.id),
                replace_existing=True,
            )
            self._scheduled_job_ids.add(job.id)

    async def recover_interrupted(self) -> None:
        publishing_error = "Runner restarted during submission"
        for job in self.repo.list_jobs(JobStatus.PUBLISHING):
            self.repo.transition(
                job.id,
                {JobStatus.PUBLISHING},
                JobStatus.UNKNOWN,
                last_error=publishing_error,
            )
            self.repo.finish_open_attempts(
                job.id,
                AttemptOutcome.UNKNOWN.value,
                publishing_error,
            )

        message = (
            "Runner restarted after browser preparation; "
            "the in-memory browser context was lost"
        )
        for status in (JobStatus.PREPARING, JobStatus.READY):
            for job in self.repo.list_jobs(status):
                self.repo.transition(
                    job.id,
                    {status},
                    JobStatus.NEEDS_ATTENTION,
                    last_error=message,
                )
                self.repo.finish_open_attempts(
                    job.id,
                    AttemptOutcome.INTERRUPTED.value,
                    message,
                )

    async def run_forever(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        on_started: Callable[[], object] | None = None,
    ) -> None:
        self.acquire_lock()
        try:
            await self.recover_interrupted()
            self.scheduler.start()
            if on_started is not None:
                started = on_started()
                if inspect.isawaitable(started):
                    await started
            while stop_event is None or not stop_event.is_set():
                await self.scan_once()
                await self.sleep(SCAN_INTERVAL_SECONDS)
        finally:
            try:
                try:
                    self.scheduler.shutdown(wait=False)
                except SchedulerNotRunningError:
                    pass
                else:
                    await asyncio.sleep(0)
            finally:
                self.release_lock()

    def _remove_inactive_jobs(self, approved_jobs: dict[str, Job]) -> None:
        for job_id in list(self._scheduled_job_ids):
            if job_id in approved_jobs:
                continue
            try:
                self.scheduler.remove_job(self._scheduler_job_id(job_id))
            except JobLookupError:
                pass
            self._scheduled_job_ids.discard(job_id)

    def _executor(self, job_id: str) -> Callable[[], Awaitable[None]]:
        if self.executor_factory is not None:

            async def injected() -> None:
                try:
                    callback = self.executor_factory(job_id)
                    if inspect.isawaitable(callback):
                        await callback
                finally:
                    self._scheduled_job_ids.discard(job_id)

            return injected

        executor = JobExecutor(
            self.config,
            self.repo,
            self.publisher_factory,
        )

        async def execute_job() -> None:
            try:
                await executor.execute(job_id)
            finally:
                self._scheduled_job_ids.discard(job_id)

        return execute_job

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    def _scheduler_job_id(self, job_id: str) -> str:
        return f"job:{job_id}"
