from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weibo_publisher.config import ensure_runtime_dirs, load_config
from weibo_publisher.logging_setup import configure_logging
from weibo_publisher.models import Job, JobStatus, Visibility
from weibo_publisher.repository import ConfirmationError, JobRepository
from weibo_publisher.scheduler import Runner, RunnerLockError
from weibo_publisher.validation import (
    build_fingerprint,
    normalize_text,
    parse_publish_time,
    validate_images,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weibo-publisher",
        description="Create and confirm scheduled Weibo posts.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root directory (defaults to the current directory).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "login",
        help="Open the dedicated browser profile for Weibo QR login.",
    )

    subparsers.add_parser(
        "doctor",
        help="Check packages, browser, storage, and Weibo session state.",
    )

    add = subparsers.add_parser("add", help="Create a draft post.")
    add.add_argument("--text", required=True, help="Post body text.")
    add.add_argument("--image", action="append", default=[], help="Image path.")
    add.add_argument(
        "--at",
        required=True,
        help="Publish time as an ISO 8601 timestamp with a timezone.",
    )
    add.add_argument(
        "--visibility",
        choices=[Visibility.PUBLIC.value, Visibility.PRIVATE.value],
        default=Visibility.PUBLIC.value,
        help="Post visibility: public or private (仅自己可见).",
    )
    add.add_argument("--timezone", default=None, help="IANA timezone name.")

    confirm = subparsers.add_parser("confirm", help="Approve a draft for publishing.")
    confirm.add_argument("job_id")

    show = subparsers.add_parser("show", help="Show one job.")
    show.add_argument("job_id")

    list_parser = subparsers.add_parser("list", help="List jobs.")
    list_parser.add_argument(
        "--status",
        choices=[item.value for item in JobStatus],
        default=None,
    )

    cancel = subparsers.add_parser("cancel", help="Cancel a draft or approved job.")
    cancel.add_argument("job_id")

    status = subparsers.add_parser("status", help="Show job attempts and artifacts.")
    status.add_argument("job_id")

    preview = subparsers.add_parser(
        "preview",
        help="Fill a draft or approved job in Weibo without publishing.",
    )
    preview.add_argument("job_id")

    subparsers.add_parser(
        "run",
        help="Start the single-runner publishing scheduler.",
    )

    return parser


def _fail(message: str) -> int:
    print(f"error: {message}")
    return 2


def _config_from_args(args: argparse.Namespace):
    root = (args.root or Path.cwd()).resolve()
    return load_config(root=root)


def _repository_from_args(args: argparse.Namespace) -> tuple[JobRepository, object]:
    config = _config_from_args(args)
    ensure_runtime_dirs(config)
    repo = JobRepository(config.db_path)
    repo.initialize()
    return repo, config


def _local_time(job: Job) -> str:
    zone = ZoneInfo(job.timezone)
    return job.publish_at_utc.astimezone(zone).isoformat()


def _cmd_add(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    try:
        ZoneInfo(args.timezone or config.default_timezone)
        publish_at_utc = parse_publish_time(
            args.at,
            args.timezone or config.default_timezone,
        )
        images = [Path(value) for value in args.image]
    except ZoneInfoNotFoundError:
        return _fail(f"Unknown timezone: {args.timezone}")
    except ValueError as exc:
        return _fail(str(exc))

    text = normalize_text(args.text)
    if not text:
        return _fail("text must not be empty")

    resolved_images: list[Path] = []
    for image in images:
        resolved = image.expanduser().resolve()
        if resolved.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            return _fail(f"Unsupported image: {resolved}")
        resolved_images.append(resolved)

    job = Job(
        id=str(uuid.uuid4()),
        text=text,
        images=resolved_images,
        publish_at_utc=publish_at_utc,
        timezone=args.timezone or config.default_timezone,
        visibility=Visibility(args.visibility),
        status=JobStatus.DRAFT,
        fingerprint="",
        created_at=datetime.now(UTC),
    )
    repo.create_job(job)

    print(f"Created draft {job.id}")
    print(f"Text: {job.text}")
    print(f"Time: {_local_time(job)}")
    print(f"Images: {len(job.images)}")
    print(f"Confirm with: weibo-publisher confirm {job.id}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    ensure_runtime_dirs(config)
    configure_logging(config)
    try:
        from weibo_publisher.browser import login

        completed = asyncio.run(login(config))
    except Exception:
        logging.getLogger(__name__).exception("Weibo login command failed")
        return _fail("Weibo login could not be opened")
    if not completed:
        print("Login not completed. Run the command again when ready.")
        return 1
    print("Login successful. The dedicated profile is ready.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    ensure_runtime_dirs(config)
    configure_logging(config)
    try:
        from weibo_publisher.browser import run_doctor

        checks = asyncio.run(run_doctor(config))
    except Exception:
        logging.getLogger(__name__).exception("Doctor command failed")
        return _fail("doctor could not complete")

    for check in checks:
        state = "OK" if check.ok else "FAIL"
        print(f"{check.name}: {state} - {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def _cmd_confirm(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    job = repo.get_job(args.job_id)
    if job is None:
        return _fail(f"job not found: {args.job_id}")
    if job.status is not JobStatus.DRAFT:
        return _fail(f"job is not a draft: {job.status}")
    if job.visibility not in {Visibility.PUBLIC, Visibility.PRIVATE}:
        return _fail("followers visibility is not supported yet")
    if not normalize_text(job.text):
        return _fail("text must not be empty")

    try:
        validate_images(job.images)
    except ValueError as exc:
        return _fail(str(exc))

    now = datetime.now(UTC)
    earliest = now + timedelta(seconds=config.prepare_lead_seconds)
    if job.publish_at_utc < earliest:
        return _fail("publish time is too soon")

    fingerprint = build_fingerprint(
        job.text,
        job.images,
        job.publish_at_utc,
        job.visibility,
    )
    try:
        repo.confirm_job(
            job.id,
            fingerprint,
            now,
            daily_limit=config.daily_limit,
            minimum_gap_seconds=config.minimum_gap_seconds,
            allow_over_limit=config.allow_over_limit,
        )
    except ConfirmationError as exc:
        return _fail(str(exc))

    print(f"Confirmed {job.id}")
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    repo, _ = _repository_from_args(args)
    job = repo.get_job(args.job_id)
    if job is None:
        return _fail(f"job not found: {args.job_id}")
    if job.status not in {JobStatus.DRAFT, JobStatus.APPROVED}:
        return _fail(f"cannot cancel a {job.status} job")

    changed = repo.transition(
        job.id,
        {job.status},
        JobStatus.CANCELLED,
    )
    if not changed:
        return _fail("job state changed while cancelling")
    print(f"Cancelled {job.id}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    repo, _ = _repository_from_args(args)
    job = repo.get_job(args.job_id)
    if job is None:
        return _fail(f"job not found: {args.job_id}")

    print(f"ID: {job.id}")
    print(f"Status: {job.status.value}")
    print(f"Text: {job.text}")
    print(f"Publish at: {_local_time(job)}")
    print(f"Timezone: {job.timezone}")
    print(f"Visibility: {job.visibility.value}")
    print(f"Images: {', '.join(str(image) for image in job.images)}")
    print(f"Fingerprint: {job.fingerprint or '(none)'}")
    print(f"Created at: {job.created_at.isoformat()}")
    print(f"Confirmed at: {job.confirmed_at.isoformat() if job.confirmed_at else '(none)'}")
    print(f"Result URL: {job.weibo_url or '(none)'}")
    print(f"Last error: {job.last_error or '(none)'}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    repo, _ = _repository_from_args(args)
    status = JobStatus(args.status) if args.status else None
    jobs = repo.list_jobs(status)

    print(
        f"{'ID':36}  {'STATUS':16}  {'PUBLISH AT':25}  "
        f"{'TEXT':24}  {'RESULT'}"
    )
    for job in jobs:
        text = job.text.replace("\n", " ")
        if len(text) > 24:
            text = text[:23] + "..."
        print(
            f"{job.id:36}  {job.status.value:16}  {_local_time(job):25}  "
            f"{text:24}  {job.weibo_url or ''}"
        )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    repo, _ = _repository_from_args(args)
    job = repo.get_job(args.job_id)
    if job is None:
        return _fail(f"job not found: {args.job_id}")

    print(f"Job: {job.id}")
    print(f"Status: {job.status.value}")
    print(f"Artifact: {job.artifact_dir or '(none)'}")
    print(f"Last error: {job.last_error or '(none)'}")
    if job.weibo_url:
        print(f"Weibo URL: {job.weibo_url}")
    attempts = repo.list_attempts(job.id)
    if not attempts:
        print("Attempts: none")
    else:
        print("Attempts:")
        for attempt in attempts:
            print(
                f"  {attempt['attempt_id']} {attempt['started_at']} "
                f"-> {attempt['outcome'] or 'pending'}"
            )
            if attempt["error"]:
                print(f"    error: {attempt['error']}")
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    job = repo.get_job(args.job_id)
    if job is None:
        return _fail(f"job not found: {args.job_id}")
    if job.status not in {JobStatus.DRAFT, JobStatus.APPROVED}:
        return _fail(f"cannot preview a {job.status.value} job")

    try:
        validate_images(job.images)
    except ValueError as exc:
        return _fail(str(exc))

    artifact_dir = config.artifacts_dir / job.id
    try:
        from weibo_publisher.weibo import preview_job

        asyncio.run(preview_job(config, job, artifact_dir))
    except RuntimeError as exc:
        logging.getLogger(__name__).exception("Weibo preview failed")
        return _fail(f"preview failed: {exc}")
    except Exception:
        logging.getLogger(__name__).exception("Weibo preview failed")
        return _fail("preview failed")

    print(f"Preview saved: {artifact_dir / 'preview.png'}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    started = False

    def report_started() -> None:
        nonlocal started
        started = True
        print("Runner started. Press Ctrl+C to stop.")

    try:
        repo, config = _repository_from_args(args)
        configure_logging(config)
        from weibo_publisher.weibo import WeiboPublisher

        runner = Runner(
            config,
            repo,
            lambda: WeiboPublisher(config),
        )
        asyncio.run(runner.run_forever(on_started=report_started))
    except RunnerLockError as exc:
        print(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("Runner stopped.")
        return 0
    except Exception:
        if started:
            logging.getLogger(__name__).exception("Runner stopped unexpectedly")
            print("error: runner stopped unexpectedly")
        else:
            print("error: runner failed to start")
        return 1

    print("Runner stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "login": _cmd_login,
        "doctor": _cmd_doctor,
        "add": _cmd_add,
        "show": _cmd_show,
        "confirm": _cmd_confirm,
        "list": _cmd_list,
        "cancel": _cmd_cancel,
        "status": _cmd_status,
        "preview": _cmd_preview,
        "run": _cmd_run,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
