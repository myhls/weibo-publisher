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
    assert draft.visibility is Visibility.PUBLIC

    assert main(["confirm", draft.id]) == 0
    approved = JobRepository(load_config(root=tmp_path).db_path).get_job(draft.id)
    assert approved is not None
    assert approved.status is JobStatus.APPROVED
    assert approved.fingerprint


def test_add_private_visibility_and_confirm_approves(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)

    assert main(
        [
            "add",
            "--text",
            "仅自己可见",
            "--image",
            str(image),
            "--at",
            future.isoformat(),
            "--visibility",
            "private",
        ]
    ) == 0

    draft = _only_draft(tmp_path)
    assert draft.visibility is Visibility.PRIVATE
    assert main(["confirm", draft.id]) == 0

    approved = JobRepository(load_config(root=tmp_path).db_path).get_job(draft.id)
    assert approved is not None
    assert approved.status is JobStatus.APPROVED
    assert approved.visibility is Visibility.PRIVATE
    assert approved.fingerprint


def test_add_rehearsal_creates_scheduled_prepare_only_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)

    assert main(
        [
            "add",
            "--text",
            "调度演练",
            "--image",
            str(image),
            "--at",
            future.isoformat(),
            "--visibility",
            "private",
            "--rehearsal",
        ]
    ) == 0

    draft = _only_draft(tmp_path)
    assert draft.rehearsal is True
    assert main(["confirm", draft.id]) == 0
    approved = JobRepository(load_config(root=tmp_path).db_path).get_job(draft.id)
    assert approved is not None
    assert approved.status is JobStatus.APPROVED
    assert approved.rehearsal is True


def test_add_rejects_followers_visibility_clearly(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "add",
                "--text",
                "仅粉丝可见",
                "--image",
                str(image),
                "--at",
                future.isoformat(),
                "--visibility",
                "followers",
            ]
        )

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "invalid choice: 'followers'" in error
    assert "public" in error
    assert "private" in error


def test_confirm_rejects_followers_visibility(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    future = datetime.now(UTC) + timedelta(days=1)
    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)
    repo = JobRepository(config.db_path)
    repo.initialize()
    draft = Job(
        id="followers-draft",
        text="仅粉丝可见",
        images=[image],
        publish_at_utc=future,
        timezone="Asia/Shanghai",
        visibility=Visibility.FOLLOWERS,
        status=JobStatus.DRAFT,
        fingerprint="",
        created_at=datetime.now(UTC),
    )
    repo.create_job(draft)

    code = main(["confirm", draft.id])

    assert code == 2
    assert "followers visibility is not supported yet" in capsys.readouterr().out
    loaded = JobRepository(load_config(root=tmp_path).db_path).get_job(draft.id)
    assert loaded is not None
    assert loaded.status is JobStatus.DRAFT
    assert loaded.fingerprint == ""


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


def test_status_shows_artifact_last_error_and_weibo_url(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)
    repo = JobRepository(config.db_path)
    repo.initialize()
    artifact_dir = config.artifacts_dir / "failed-job" / "attempt-1"
    job = Job(
        id="failed-job",
        text="失败任务",
        images=[],
        publish_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.NEEDS_ATTENTION,
        fingerprint="fp",
        created_at=datetime.now(UTC),
        last_error="composer unavailable",
        artifact_dir=artifact_dir,
        weibo_url="https://weibo.com/detail/123",
    )
    repo.create_job(job)

    assert main(["status", job.id]) == 0

    output = capsys.readouterr().out
    assert f"Artifact: {artifact_dir}" in output
    assert "Last error: composer unavailable" in output
    assert "Weibo URL: https://weibo.com/detail/123" in output


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

        async def run_forever(self, *, on_started=None) -> None:
            calls.append("run")
            assert on_started is not None
            on_started()

    class BlockedRunner(FakeRunner):
        async def run_forever(self, *, on_started=None) -> None:
            del on_started
            raise RunnerLockError("runner is already running")

    monkeypatch.setattr("weibo_publisher.cli.Runner", FakeRunner)
    assert main(["run"]) == 0
    assert calls == ["run"]
    assert "Runner started" in capsys.readouterr().out

    monkeypatch.setattr("weibo_publisher.cli.Runner", BlockedRunner)
    assert main(["run"]) == 1
    blocked_out = capsys.readouterr().out
    assert "already running" in blocked_out
    assert "Runner started" not in blocked_out


def test_run_reports_runner_construction_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    class BrokenRunner:
        def __init__(self, config, repo, publisher_factory) -> None:
            del config, repo, publisher_factory
            raise RuntimeError("construction failed")

    monkeypatch.setattr("weibo_publisher.cli.Runner", BrokenRunner)

    assert main(["run"]) == 1
    output = capsys.readouterr().out
    assert "runner failed to start" in output
    assert "Runner started" not in output


def test_run_controls_repository_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    def fail_repository(args):
        del args
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "weibo_publisher.cli._repository_from_args",
        fail_repository,
    )

    assert main(["run"]) == 1
    captured = capsys.readouterr()
    assert "runner failed to start" in captured.out
    assert "Runner started" not in captured.out
    assert "Traceback" not in captured.err


def test_run_controls_logging_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "weibo_publisher.cli._repository_from_args",
        lambda args: (object(), object()),
    )

    def fail_logging(config) -> None:
        del config
        raise RuntimeError("logging unavailable")

    monkeypatch.setattr(
        "weibo_publisher.cli.configure_logging",
        fail_logging,
    )

    assert main(["run"]) == 1
    captured = capsys.readouterr()
    assert "runner failed to start" in captured.out
    assert "Runner started" not in captured.out
    assert "Traceback" not in captured.err
