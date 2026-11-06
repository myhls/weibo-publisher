from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from weibo_publisher.config import ensure_runtime_dirs, load_config
from weibo_publisher.models import Job, JobStatus, Visibility
from weibo_publisher.repository import JobRepository
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
        choices=[item.value for item in Visibility],
        default=Visibility.PUBLIC.value,
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

    return parser


def _fail(message: str) -> int:
    print(f"error: {message}")
    return 2


def _repository_from_args(args: argparse.Namespace) -> tuple[JobRepository, object]:
    root = (args.root or Path.cwd()).resolve()
    config = load_config(root=root)
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
        publish_at_utc = parse_publish_time(
            args.at,
            args.timezone or config.default_timezone,
        )
        images = [Path(value) for value in args.image]
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


def _cmd_confirm(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    job = repo.get_job(args.job_id)
    if job is None:
        return _fail(f"job not found: {args.job_id}")
    if job.status is not JobStatus.DRAFT:
        return _fail(f"job is not a draft: {job.status}")
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

    if not config.allow_over_limit:
        local_date = job.publish_at_utc.astimezone(ZoneInfo(job.timezone)).date()
        count = repo.count_for_local_date(local_date, job.timezone)
        if count >= config.daily_limit:
            return _fail("daily limit reached")

    conflict = repo.find_gap_conflict(
        job.publish_at_utc,
        config.minimum_gap_seconds,
    )
    if conflict is not None and conflict.id != job.id:
        return _fail(f"publish time conflicts with job {conflict.id} within minimum gap")

    fingerprint = build_fingerprint(
        job.text,
        job.images,
        job.publish_at_utc,
        job.visibility,
    )
    changed = repo.transition(
        job.id,
        {JobStatus.DRAFT},
        JobStatus.APPROVED,
        fingerprint=fingerprint,
        confirmed_at=now,
    )
    if not changed:
        return _fail("job state changed while confirming")

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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "add": _cmd_add,
        "show": _cmd_show,
        "confirm": _cmd_confirm,
        "list": _cmd_list,
        "cancel": _cmd_cancel,
        "status": _cmd_status,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
