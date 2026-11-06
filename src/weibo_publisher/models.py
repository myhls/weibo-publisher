from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class JobStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    PREPARING = "preparing"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    UNKNOWN = "unknown"
    MISSED = "missed"
    CANCELLED = "cancelled"


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    FOLLOWERS = "followers"


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class Job:
    id: str
    text: str
    images: list[Path]
    publish_at_utc: datetime
    timezone: str
    visibility: Visibility
    status: JobStatus
    fingerprint: str
    created_at: datetime
    confirmed_at: datetime | None = None
    last_attempt_at: datetime | None = None
    published_at: datetime | None = None
    weibo_mid: str | None = None
    weibo_url: str | None = None
    last_error: str | None = None
    artifact_dir: Path | None = None


@dataclass(slots=True)
class PublishResult:
    outcome: AttemptOutcome
    weibo_mid: str | None = None
    weibo_url: str | None = None
    error: str | None = None


_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.DRAFT: {JobStatus.APPROVED, JobStatus.CANCELLED},
    JobStatus.APPROVED: {
        JobStatus.PREPARING,
        JobStatus.CANCELLED,
        JobStatus.MISSED,
    },
    JobStatus.PREPARING: {
        JobStatus.READY,
        JobStatus.NEEDS_ATTENTION,
        JobStatus.FAILED,
    },
    JobStatus.READY: {
        JobStatus.PUBLISHING,
        JobStatus.NEEDS_ATTENTION,
        JobStatus.MISSED,
    },
    JobStatus.PUBLISHING: {JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.UNKNOWN},
    JobStatus.UNKNOWN: {JobStatus.NEEDS_ATTENTION},
    JobStatus.NEEDS_ATTENTION: set(),
    JobStatus.PUBLISHED: set(),
    JobStatus.FAILED: set(),
    JobStatus.MISSED: set(),
    JobStatus.CANCELLED: set(),
}


def assert_transition_allowed(current: JobStatus, target: JobStatus) -> None:
    if target not in _TRANSITIONS[current]:
        raise ValueError(f"Invalid state transition: {current} -> {target}")
