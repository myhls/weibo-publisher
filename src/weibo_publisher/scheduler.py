from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
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
)
from weibo_publisher.publisher import Publisher
from weibo_publisher.repository import JobRepository
from weibo_publisher.validation import build_fingerprint, validate_images

SCAN_INTERVAL_SECONDS = 5
LOCK_ACQUIRE_ATTEMPTS = 3


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
        if now > cutoff:
            self.repo.transition(
                job_id,
                {JobStatus.APPROVED},
                JobStatus.MISSED,
                last_error="Job was too late to publish safely",
            )
            return

        started_at = datetime.now(UTC)
        if not self.repo.transition(
            job_id,
            {JobStatus.APPROVED},
            JobStatus.PREPARING,
            last_attempt_at=started_at,
            last_error=None,
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

        attempt_id = uuid4().hex
        artifact_dir = self.config.artifacts_dir / job_id / attempt_id
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
                self.repo.transition(
                    job_id,
                    {JobStatus.PREPARING, JobStatus.READY},
                    JobStatus.FAILED,
                    last_error=str(exc),
                )
                outcome = AttemptOutcome.FAILED
            if attempt_recorded:
                self.repo.finish_attempt(attempt_id, outcome.value, str(exc))
        finally:
            if publisher is not None:
                await publisher.close()

    def _validate_preparation(self, job: Job) -> None:
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
        self._lock_fd: int | None = None

    def acquire_lock(self) -> None:
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(LOCK_ACQUIRE_ATTEMPTS):
            try:
                lock_fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if self._remove_stale_lock():
                    continue
                raise RunnerLockError(
                    f"Another runner is already running for {self.config.root}"
                )

            self._lock_fd = lock_fd
            try:
                os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
            except BaseException:
                self.release_lock()
                raise
            return

        raise RunnerLockError(
            f"Another runner is already running for {self.config.root}"
        )

    def release_lock(self) -> None:
        lock_fd = self._lock_fd
        if lock_fd is None:
            return
        self._lock_fd = None
        try:
            os.close(lock_fd)
        finally:
            self._lock_path.unlink(missing_ok=True)

    async def scan_once(self) -> None:
        approved_jobs = {
            job.id: job for job in self.repo.list_jobs(JobStatus.APPROVED)
        }
        self._remove_inactive_jobs(approved_jobs)

        now = self._now()
        for job in approved_jobs.values():
            if job.id in self._scheduled_job_ids:
                continue

            cutoff = job.publish_at_utc + timedelta(
                seconds=self.config.late_cutoff_seconds
            )
            if now > cutoff:
                self.repo.transition(
                    job.id,
                    {JobStatus.APPROVED},
                    JobStatus.MISSED,
                    last_error="Job was too late to prepare safely",
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
        for job in self.repo.list_jobs(JobStatus.PUBLISHING):
            self.repo.transition(
                job.id,
                {JobStatus.PUBLISHING},
                JobStatus.UNKNOWN,
                last_error="Runner restarted during submission",
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
                callback = self.executor_factory(job_id)
                if inspect.isawaitable(callback):
                    await callback

            return injected

        executor = JobExecutor(
            self.config,
            self.repo,
            self.publisher_factory,
        )

        async def execute_job() -> None:
            await executor.execute(job_id)

        return execute_job

    def _remove_stale_lock(self) -> bool:
        try:
            lock_pid = self._read_lock_pid()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if lock_pid is None or lock_pid <= 0 or self._pid_is_alive(lock_pid):
            return False

        try:
            current_pid = self._read_lock_pid()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if current_pid != lock_pid:
            return False

        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True

    def _read_lock_pid(self) -> int | None:
        content = self._lock_path.read_text(encoding="ascii").strip()
        value = content.removeprefix("pid=")
        try:
            return int(value)
        except ValueError:
            return None

    def _pid_is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            return self._windows_pid_is_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    def _windows_pid_is_alive(self, pid: int) -> bool:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            return now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    def _scheduler_job_id(self, job_id: str) -> str:
        return f"job:{job_id}"
