from datetime import UTC, datetime

import pytest

from weibo_publisher.models import (
    Job,
    JobStatus,
    Visibility,
    assert_transition_allowed,
)


def test_job_returns_aware_publish_time() -> None:
    job = Job(
        id="job-1",
        text="可爱",
        images=[],
        publish_at_utc=datetime(2026, 11, 7, 9, 30, tzinfo=UTC),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.DRAFT,
        fingerprint="",
        created_at=datetime.now(UTC),
    )

    assert job.publish_at_utc.tzinfo is UTC


def test_disallowed_transition_raises() -> None:
    with pytest.raises(ValueError, match="draft -> published"):
        assert_transition_allowed(JobStatus.DRAFT, JobStatus.PUBLISHED)


def test_unknown_cannot_return_to_approved() -> None:
    with pytest.raises(ValueError):
        assert_transition_allowed(JobStatus.UNKNOWN, JobStatus.APPROVED)


def test_private_visibility_has_stable_cli_value() -> None:
    assert Visibility.PRIVATE == "private"


def test_preparing_can_finish_as_rehearsed() -> None:
    assert_transition_allowed(JobStatus.PREPARING, JobStatus.REHEARSED)
