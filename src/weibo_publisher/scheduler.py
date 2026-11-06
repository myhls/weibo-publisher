from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
