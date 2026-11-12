from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weibo_publisher.batch import (
    BatchAnalysis,
    BatchFormatError,
    analyze_workbook,
    build_draft_jobs,
    parse_local_date,
)
from weibo_publisher.config import ensure_runtime_dirs, load_config
from weibo_publisher.logging_setup import configure_logging
from weibo_publisher.models import (
    Batch,
    BatchStatus,
    Job,
    JobStatus,
    Visibility,
)
from weibo_publisher.repository import ConfirmationError, JobRepository
from weibo_publisher.scheduler import Runner, RunnerLockError
from weibo_publisher.validation import (
    build_fingerprint,
    normalize_text,
    parse_publish_time,
    validate_images,
)

MINIMUM_SCHEDULE_LEAD_SECONDS = 300


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
    text_source = add.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text", help="Post body text.")
    text_source.add_argument(
        "--text-file",
        type=Path,
        help="Read post body text from a UTF-8 file.",
    )
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
    add.add_argument(
        "--rehearsal",
        action="store_true",
        help="Prepare at the scheduled time without publishing.",
    )

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

    batch_import = subparsers.add_parser(
        "batch-import",
        help="Validate a batch workbook and create draft jobs without approval.",
    )
    batch_import.add_argument("--file", type=Path, required=True)
    batch_import.add_argument("--name", default=None)
    batch_import.add_argument(
        "--date",
        required=True,
        help="Required Beijing local date in YYYY-MM-DD format.",
    )
    batch_import.add_argument("--timezone", default=None)
    batch_import.add_argument(
        "--replace",
        action="store_true",
        help="Replace the existing draft batch for the same workbook.",
    )

    batch_report = subparsers.add_parser(
        "batch-report",
        help="Re-read a batch workbook and show its full validation report.",
    )
    batch_report.add_argument("batch_id")

    batch_confirm = subparsers.add_parser(
        "batch-confirm",
        help="Revalidate and atomically approve one complete draft batch.",
    )
    batch_confirm.add_argument("batch_id")
    batch_confirm.add_argument(
        "--yes",
        action="store_true",
        help="Confirm approval of every valid job in the batch.",
    )

    batch_list = subparsers.add_parser("batch-list", help="List imported batches.")
    batch_list.add_argument(
        "--status",
        choices=[item.value for item in BatchStatus],
        default=None,
    )

    batch_resume = subparsers.add_parser(
        "batch-resume",
        help="Resume a paused batch after its source still validates.",
    )
    batch_resume.add_argument("batch_id")
    batch_resume.add_argument(
        "--yes",
        action="store_true",
        help="Confirm resumption of the paused batch.",
    )

    batch_cancel = subparsers.add_parser(
        "batch-cancel",
        help="Cancel all not-yet-published jobs in a batch.",
    )
    batch_cancel.add_argument("batch_id")
    batch_cancel.add_argument(
        "--yes",
        action="store_true",
        help="Confirm cancellation of the batch.",
    )

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

    try:
        raw_text = (
            args.text_file.expanduser().resolve().read_text(encoding="utf-8-sig")
            if args.text_file is not None
            else args.text
        )
    except (OSError, UnicodeError) as exc:
        return _fail(f"could not read text file: {exc}")

    text = normalize_text(raw_text)
    if not text:
        return _fail("text must not be empty")

    minimum_lead = max(MINIMUM_SCHEDULE_LEAD_SECONDS, config.prepare_lead_seconds)
    earliest = datetime.now(UTC) + timedelta(seconds=minimum_lead)
    if publish_at_utc < earliest:
        return _fail(f"publish time must be at least {minimum_lead} seconds away")

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
        rehearsal=args.rehearsal,
    )
    repo.create_job(job)

    print(f"Created draft {job.id}")
    print(f"Text: {job.text}")
    print(f"Time: {_local_time(job)}")
    print(f"Images: {len(job.images)}")
    print(f"Mode: {'rehearsal (prepare only)' if job.rehearsal else 'publish'}")
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
    if job.batch_id:
        return _fail(
            "batch jobs must be approved with batch-confirm, not confirm"
        )
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
    if job.batch_id:
        return _fail(
            "batch jobs must be managed through the batch import and confirm flow"
        )
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
    print(f"Rehearsal: {'yes' if job.rehearsal else 'no'}")
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


def _analyze_batch(
    config,
    batch: Batch,
) -> BatchAnalysis:
    return analyze_workbook(
        batch.source_path,
        name=batch.name,
        local_date=batch.local_date,
        timezone=batch.timezone,
        minimum_gap_seconds=config.minimum_gap_seconds,
        prepare_lead_seconds=config.prepare_lead_seconds,
        daily_limit=config.daily_limit,
    )


def _batch_source_matches_jobs(
    analysis: BatchAnalysis,
    batch: Batch,
    jobs: list[Job],
) -> bool:
    if analysis.source_fingerprint != batch.source_fingerprint:
        return False
    if len(analysis.items) != len(jobs):
        return False
    by_row = {job.source_row: job for job in jobs}
    return all(
        by_row.get(item.source_row) is not None
        and by_row[item.source_row].source_fingerprint
        == item.source_fingerprint
        and by_row[item.source_row].source_sequence == item.sequence
        for item in analysis.items
    )


def _print_batch_report(
    analysis: BatchAnalysis,
    batch: Batch | None,
    jobs: list[Job] | None = None,
    *,
    minimum_gap_seconds: int,
) -> None:
    zone = ZoneInfo(analysis.timezone)
    scheduled = [
        item.publish_at_utc.astimezone(zone)
        for item in analysis.items
        if item.publish_at_utc is not None
    ]
    gaps = [
        int((later - earlier).total_seconds())
        for earlier, later in itertools.pairwise(scheduled)
    ]
    blocking_rows = analysis.blocking_items
    print(f"Batch: {batch.id if batch else '(not created)'}")
    print(f"Name: {analysis.name}")
    print(f"Status: {batch.status.value if batch else 'draft validation'}")
    print(f"Source: {analysis.source_path}")
    print(f"Local date: {analysis.local_date.isoformat()} {analysis.timezone}")
    print(f"Records: {len(analysis.items)}")
    print(f"Valid records: {len(analysis.valid_items)}")
    print(f"Records with blocking issues: {len(blocking_rows)}")
    if scheduled:
        print(
            "Schedule: "
            f"{scheduled[0].strftime('%H:%M')} -> "
            f"{scheduled[-1].strftime('%H:%M')}"
        )
    else:
        print("Schedule: unavailable")
    print(
        "Minimum gap: "
        f"{min(gaps) if gaps else 'n/a'}s "
        f"(required: {minimum_gap_seconds}s)"
    )

    if not _batch_source_matches_jobs(analysis, batch, jobs or []) and batch:
        print("BLOCKED source: workbook no longer matches the imported draft")

    if analysis.issues:
        print("Batch anomalies:")
        for issue in analysis.issues:
            level = "BLOCK" if issue.blocking else "WARN"
            print(f"  [{level}] {issue.code}: {issue.message}")

    if blocking_rows:
        print("Row anomalies:")
        for item in blocking_rows:
            print(
                f"  {item.sequence or '(no sequence)'} "
                f"row {item.source_row}"
            )
            for issue in item.blocking_issues:
                print(f"    {issue.code}: {issue.message}")

    print("Schedule:")
    if jobs:
        jobs_by_row = {job.source_row: job for job in jobs}
    else:
        jobs_by_row = {}
    ordered_items = sorted(
        analysis.items,
        key=lambda item: (
            item.publish_at_utc is None,
            item.publish_at_utc or datetime.max.replace(tzinfo=UTC),
            item.source_row,
        ),
    )
    for item in ordered_items:
        if item.publish_at_utc is None:
            local_time = "INVALID"
        else:
            local_time = item.publish_at_utc.astimezone(zone).strftime("%H:%M")
        job = jobs_by_row.get(item.source_row)
        job_status = job.status.value if job is not None else "draft"
        validation = "OK" if item.valid else "BLOCKED"
        detail = ""
        if job is not None and job.weibo_url:
            detail = f" url={job.weibo_url}"
        elif job is not None and job.last_error:
            detail = f" error={job.last_error}"
        print(
            f"  {item.sequence or '(none)':>4} "
            f"row {item.source_row:>3} {local_time} "
            f"{item.visibility.value if item.visibility else 'invalid':<7} "
            f"images={len(item.images):>2} "
            f"{validation:<7} {job_status}{detail}"
        )
def _cmd_batch_import(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    timezone = args.timezone or config.default_timezone
    try:
        ZoneInfo(timezone)
        local_date = parse_local_date(args.date)
        analysis = analyze_workbook(
            args.file,
            name=args.name,
            local_date=local_date,
            timezone=timezone,
            minimum_gap_seconds=config.minimum_gap_seconds,
            prepare_lead_seconds=config.prepare_lead_seconds,
            daily_limit=config.daily_limit,
        )
    except (BatchFormatError, ZoneInfoNotFoundError) as exc:
        return _fail(str(exc))

    existing = repo.find_draft_batch_for_source(analysis.source_path)
    if existing is not None and not args.replace:
        _print_batch_report(
            analysis,
            existing,
            repo.list_batch_jobs(existing.id),
            minimum_gap_seconds=config.minimum_gap_seconds,
        )
        return _fail(
            "a draft batch already exists for this workbook; "
            "rerun with --replace after reviewing changes"
        )

    if not analysis.can_import:
        _print_batch_report(
            analysis,
            None,
            minimum_gap_seconds=config.minimum_gap_seconds,
        )
        return _fail(
            "no batch was created because one or more rows lack a valid "
            "publish time or visibility"
        )

    batch_id = existing.id if existing is not None else str(uuid.uuid4())
    batch = Batch(
        id=batch_id,
        name=analysis.name,
        source_path=analysis.source_path,
        source_fingerprint=analysis.source_fingerprint,
        local_date=analysis.local_date,
        timezone=analysis.timezone,
        status=BatchStatus.DRAFT,
        created_at=datetime.now(UTC),
    )
    jobs = build_draft_jobs(analysis, batch_id)
    try:
        if existing is not None:
            repo.replace_draft_batch(batch, jobs)
        else:
            repo.create_batch(batch, jobs)
    except ConfirmationError as exc:
        return _fail(str(exc))

    _print_batch_report(
        analysis,
        batch,
        jobs,
        minimum_gap_seconds=config.minimum_gap_seconds,
    )
    print()
    if analysis.can_confirm:
        print("Review complete. No jobs were approved.")
        print(f"Approve the whole batch with: weibo-publisher batch-confirm {batch.id} --yes")
    else:
        print("No jobs were approved because blocking anomalies remain.")
        print("Fix the workbook, rerun batch-import --replace, then report again.")
    return 0


def _cmd_batch_report(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    batch = repo.get_batch(args.batch_id)
    if batch is None:
        return _fail(f"batch not found: {args.batch_id}")
    jobs = repo.list_batch_jobs(batch.id)
    try:
        analysis = _analyze_batch(config, batch)
    except (BatchFormatError, ZoneInfoNotFoundError) as exc:
        return _fail(str(exc))
    _print_batch_report(
        analysis,
        batch,
        jobs,
        minimum_gap_seconds=config.minimum_gap_seconds,
    )
    return 0


def _cmd_batch_confirm(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    batch = repo.get_batch(args.batch_id)
    if batch is None:
        return _fail(f"batch not found: {args.batch_id}")
    jobs = repo.list_batch_jobs(batch.id)
    try:
        analysis = _analyze_batch(config, batch)
    except (BatchFormatError, ZoneInfoNotFoundError) as exc:
        return _fail(str(exc))
    _print_batch_report(
        analysis,
        batch,
        jobs,
        minimum_gap_seconds=config.minimum_gap_seconds,
    )
    if batch.status is not BatchStatus.DRAFT:
        return _fail(f"batch is not a draft: {batch.status.value}")
    if not _batch_source_matches_jobs(analysis, batch, jobs):
        return _fail(
            "workbook content changed after import; rerun batch-import "
            "--replace before approving"
        )
    if not analysis.can_confirm:
        return _fail("batch has blocking validation issues")
    if not args.yes:
        return _fail(
            "explicit whole-batch confirmation was not provided; "
            "rerun with --yes after reviewing the report"
        )

    try:
        fingerprints = {
            job.id: build_fingerprint(
                job.text,
                job.images,
                job.publish_at_utc,
                job.visibility,
            )
            for job in jobs
        }
        repo.confirm_batch(
            batch.id,
            fingerprints,
            datetime.now(UTC),
            daily_limit=config.daily_limit,
            minimum_gap_seconds=config.minimum_gap_seconds,
            allow_over_limit=config.allow_over_limit,
        )
    except (ConfirmationError, OSError, ValueError) as exc:
        return _fail(str(exc))

    print(f"Approved {len(jobs)} jobs in batch {batch.id}")
    print("Start the runner with: weibo-publisher run")
    return 0


def _cmd_batch_list(args: argparse.Namespace) -> int:
    repo, _ = _repository_from_args(args)
    status = BatchStatus(args.status) if args.status else None
    batches = repo.list_batches(status)
    print(
        f"{'ID':36}  {'STATUS':10}  {'DATE':10}  "
        f"{'JOBS':>4}  {'NAME'}"
    )
    for batch in batches:
        jobs = repo.list_batch_jobs(batch.id)
        print(
            f"{batch.id:36}  {batch.status.value:10}  "
            f"{batch.local_date.isoformat():10}  {len(jobs):4d}  "
            f"{batch.name}"
        )
    return 0


def _cmd_batch_resume(args: argparse.Namespace) -> int:
    repo, config = _repository_from_args(args)
    batch = repo.get_batch(args.batch_id)
    if batch is None:
        return _fail(f"batch not found: {args.batch_id}")
    jobs = repo.list_batch_jobs(batch.id)
    try:
        analysis = _analyze_batch(config, batch)
    except (BatchFormatError, ZoneInfoNotFoundError) as exc:
        return _fail(str(exc))
    _print_batch_report(
        analysis,
        batch,
        jobs,
        minimum_gap_seconds=config.minimum_gap_seconds,
    )
    if batch.status is not BatchStatus.PAUSED:
        return _fail(f"batch is not paused: {batch.status.value}")
    if not args.yes:
        return _fail(
            "explicit resume confirmation was not provided; rerun with --yes"
        )
    if not _batch_source_matches_jobs(analysis, batch, jobs):
        return _fail(
            "workbook content changed after import; replace the draft batch "
            "before resuming"
        )
    if not analysis.can_confirm:
        return _fail("batch has blocking validation issues")
    try:
        resumed = repo.resume_batch(batch.id)
    except ConfirmationError as exc:
        return _fail(str(exc))
    print(f"Resumed {len(jobs)} jobs in batch {resumed.id}")
    return 0


def _cmd_batch_cancel(args: argparse.Namespace) -> int:
    repo, _ = _repository_from_args(args)
    batch = repo.get_batch(args.batch_id)
    if batch is None:
        return _fail(f"batch not found: {args.batch_id}")
    jobs = repo.list_batch_jobs(batch.id)
    print(f"Batch: {batch.id}")
    print(f"Status: {batch.status.value}")
    print(f"Jobs: {len(jobs)}")
    if not args.yes:
        return _fail(
            "explicit cancellation confirmation was not provided; "
            "rerun with --yes"
        )
    try:
        cancelled = repo.cancel_batch(batch.id)
    except ConfirmationError as exc:
        return _fail(str(exc))
    print(f"Cancelled batch {cancelled.id}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    started = False
    runner: Runner | None = None

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

    halted_reason = getattr(runner, "halted_reason", None)
    if halted_reason:
        print(f"error: publishing halted: {halted_reason}")
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
        "batch-import": _cmd_batch_import,
        "batch-report": _cmd_batch_report,
        "batch-confirm": _cmd_batch_confirm,
        "batch-list": _cmd_batch_list,
        "batch-resume": _cmd_batch_resume,
        "batch-cancel": _cmd_batch_cancel,
        "run": _cmd_run,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
