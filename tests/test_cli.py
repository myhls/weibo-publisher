from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from weibo_publisher.cli import main
from weibo_publisher.config import ensure_runtime_dirs, load_config
from weibo_publisher.models import Job, JobStatus, Visibility
from weibo_publisher.repository import JobRepository
from weibo_publisher.scheduler import RunnerLockError


def _write_config(root: Path, **overrides: str) -> None:
    defaults = {
        "default_timezone": "Asia/Shanghai",
        "prepare_lead_seconds": "180",
        "late_cutoff_seconds": "30",
        "daily_limit": "100",
        "minimum_gap_seconds": "240",
        "allow_over_limit": "false",
    }
    defaults.update(overrides)
    text = (
        '[app]\n'
        f'default_timezone = "{defaults["default_timezone"]}"\n\n'
        "[publishing]\n"
        f'prepare_lead_seconds = {defaults["prepare_lead_seconds"]}\n'
        f'late_cutoff_seconds = {defaults["late_cutoff_seconds"]}\n'
        f'daily_limit = {defaults["daily_limit"]}\n'
        f'minimum_gap_seconds = {defaults["minimum_gap_seconds"]}\n'
        f'allow_over_limit = {defaults["allow_over_limit"]}\n'
    )
    (root / "config.toml").write_text(text, encoding="utf-8")


def _add_draft(root: Path, image: Path, at: datetime) -> int:
    return main(
        [
            "add",
            "--text",
            "可爱",
            "--image",
            str(image),
            "--at",
            at.isoformat(),
        ]
    )


def _only_draft(root: Path) -> Job:
    config = load_config(root=root)
    repo = JobRepository(config.db_path)
    return repo.list_jobs(JobStatus.DRAFT)[-1]


def _next_shanghai_noon() -> datetime:
    zone = ZoneInfo("Asia/Shanghai")
    now_local = datetime.now(UTC).astimezone(zone)
    noon = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
    if noon <= now_local:
        noon += timedelta(days=1)
    return noon.astimezone(UTC)


def test_add_creates_draft_and_confirm_approves(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)

    assert _add_draft(tmp_path, image, future) == 0

    draft = _only_draft(tmp_path)
    assert draft.fingerprint == ""
    assert draft.status is JobStatus.DRAFT

    assert main(["confirm", draft.id]) == 0
    approved = JobRepository(load_config(root=tmp_path).db_path).get_job(draft.id)
    assert approved is not None
    assert approved.status is JobStatus.APPROVED
    assert approved.fingerprint


def test_confirm_rejects_missing_image(tmp_path: Path, monkeypatch, capsys) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)
    _add_draft(tmp_path, image, future)
    image.unlink()

    draft = _only_draft(tmp_path)
    code = main(["confirm", draft.id])

    assert code == 2
    assert "Image does not exist" in capsys.readouterr().out


def test_confirm_rejects_time_within_prepare_lead(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_config(tmp_path, prepare_lead_seconds="86400")
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(hours=1)

    assert _add_draft(tmp_path, image, future) == 0
    draft = _only_draft(tmp_path)
    code = main(["confirm", draft.id])

    assert code == 2
    assert "too soon" in capsys.readouterr().out


def test_confirm_rejects_past_time(tmp_path: Path, monkeypatch, capsys) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    past = datetime.now(UTC) - timedelta(hours=1)

    assert _add_draft(tmp_path, image, past) == 0
    draft = _only_draft(tmp_path)
    code = main(["confirm", draft.id])

    assert code == 2
    assert "too soon" in capsys.readouterr().out


def test_confirm_enforces_daily_limit(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_config(tmp_path, daily_limit="1")
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    noon = _next_shanghai_noon()
    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)
    repo = JobRepository(config.db_path)
    repo.initialize()

    existing = Job(
        id="existing",
        text="已有",
        images=[],
        publish_at_utc=noon,
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.APPROVED,
        fingerprint="existing",
        created_at=datetime.now(UTC),
    )
    repo.create_job(existing)

    future = noon + timedelta(hours=2)
    assert _add_draft(tmp_path, image, future) == 0
    draft = _only_draft(tmp_path)
    code = main(["confirm", draft.id])

    assert code == 2
    assert "daily limit" in capsys.readouterr().out


def test_add_rejects_unknown_timezone(tmp_path: Path, monkeypatch, capsys) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)

    code = main(
        [
            "add",
            "--text",
            "可爱",
            "--image",
            str(image),
            "--at",
            future.isoformat(),
            "--timezone",
            "Mars/Olympus",
        ]
    )

    assert code == 2
    assert "Unknown timezone" in capsys.readouterr().out


def test_confirm_enforces_minimum_gap(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_config(tmp_path, minimum_gap_seconds="3600")
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)
    repo = JobRepository(config.db_path)
    repo.initialize()

    future = datetime.now(UTC) + timedelta(days=1)
    existing = Job(
        id="existing",
        text="已有",
        images=[],
        publish_at_utc=future,
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.APPROVED,
        fingerprint="existing",
        created_at=datetime.now(UTC),
    )
    repo.create_job(existing)

    assert _add_draft(tmp_path, image, future) == 0
    draft = _only_draft(tmp_path)
    code = main(["confirm", draft.id])

    assert code == 2
    assert "gap" in capsys.readouterr().out


def test_confirm_honors_allow_over_limit(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, daily_limit="1", allow_over_limit="true")
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)
    repo = JobRepository(config.db_path)
    repo.initialize()
    existing = Job(
        id="existing",
        text="已有",
        images=[],
        publish_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.APPROVED,
        fingerprint="existing",
        created_at=datetime.now(UTC),
    )
    repo.create_job(existing)

    future = datetime.now(UTC) + timedelta(days=1, hours=2)
    assert _add_draft(tmp_path, image, future) == 0
    draft = _only_draft(tmp_path)

    assert main(["confirm", draft.id]) == 0


def test_cancel_draft_and_approved(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)
    _add_draft(tmp_path, image, future)
    draft = _only_draft(tmp_path)

    assert main(["cancel", draft.id]) == 0
    assert JobRepository(load_config(root=tmp_path).db_path).get_job(draft.id).status is (
        JobStatus.CANCELLED
    )

    assert _add_draft(tmp_path, image, future) == 0
    second = _only_draft(tmp_path)
    assert main(["confirm", second.id]) == 0
    assert main(["cancel", second.id]) == 0


def test_cancel_rejects_published(tmp_path: Path, monkeypatch, capsys) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)
    repo = JobRepository(config.db_path)
    repo.initialize()
    job = Job(
        id="published",
        text="已发布",
        images=[],
        publish_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.PUBLISHED,
        fingerprint="fp",
        created_at=datetime.now(UTC),
    )
    repo.create_job(job)

    code = main(["cancel", job.id])

    assert code == 2
    assert "cannot cancel" in capsys.readouterr().out


def test_list_and_show_omit_session_data(tmp_path: Path, monkeypatch, capsys) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)
    _add_draft(tmp_path, image, future)
    draft = _only_draft(tmp_path)

    assert main(["list"]) == 0
    list_out = capsys.readouterr().out
    assert draft.id[:8] in list_out
    assert "draft" in list_out

    assert main(["show", draft.id]) == 0
    show_out = capsys.readouterr().out
    assert "cookie" not in show_out
    assert "session" not in show_out
    assert "profile" not in show_out


def test_show_missing_job_returns_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["show", "missing"]) == 2
    assert "not found" in capsys.readouterr().out


def test_status_shows_artifact_path(tmp_path: Path, monkeypatch, capsys) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)
    _add_draft(tmp_path, image, future)
    draft = _only_draft(tmp_path)

    assert main(["status", draft.id]) == 0
    assert "Artifact" in capsys.readouterr().out


def test_run_reports_startup_and_lock_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    class FakeRunner:
        def __init__(self, config, repo, publisher_factory) -> None:
            del config, repo, publisher_factory

        async def run_forever(self) -> None:
            calls.append("run")

    class BlockedRunner(FakeRunner):
        async def run_forever(self) -> None:
            raise RunnerLockError("runner is already running")

    monkeypatch.setattr("weibo_publisher.cli.Runner", FakeRunner)
    assert main(["run"]) == 0
    assert calls == ["run"]
    assert "Runner started" in capsys.readouterr().out

    monkeypatch.setattr("weibo_publisher.cli.Runner", BlockedRunner)
    assert main(["run"]) == 1
    assert "already running" in capsys.readouterr().out
