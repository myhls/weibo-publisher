from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import openpyxl

from weibo_publisher.models import Job, JobStatus, Visibility
from weibo_publisher.validation import SUPPORTED_IMAGES

SHEET_NAME = "发布清单"
HEADER_ROW = 8
FIRST_DATA_ROW = 9
IMAGE_FIRST_COLUMN = 3
IMAGE_LAST_COLUMN = 20
VISIBILITY_COLUMN = 22
PUBLISH_TIME_COLUMN = 23
MAX_IMAGES = 18
MAX_IMAGE_BYTES = 20 * 1024 * 1024
BEIJING_TIMEZONE = "Asia/Shanghai"
BODY_TIME_PATTERN = re.compile(r"║\s*(\d{2}):(\d{2})")
PATH_SEPARATOR_PATTERN = re.compile(r"[\r\n;；]")


class BatchFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BatchIssue:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class BatchItem:
    sequence: str
    source_row: int
    text: str
    images: tuple[Path, ...]
    raw_images: tuple[str, ...]
    publish_at_utc: datetime | None
    visibility: Visibility | None
    source_fingerprint: str
    issues: tuple[BatchIssue, ...]

    @property
    def blocking_issues(self) -> tuple[BatchIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def valid(self) -> bool:
        return bool(self.text) and not self.blocking_issues


@dataclass(frozen=True, slots=True)
class BatchAnalysis:
    name: str
    source_path: Path
    source_fingerprint: str
    local_date: date
    timezone: str
    items: tuple[BatchItem, ...]
    issues: tuple[BatchIssue, ...]

    @property
    def blocking_items(self) -> tuple[BatchItem, ...]:
        return tuple(item for item in self.items if item.blocking_issues)

    @property
    def valid_items(self) -> tuple[BatchItem, ...]:
        return tuple(item for item in self.items if item.valid)

    @property
    def blocking_issues(self) -> tuple[BatchIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def can_import(self) -> bool:
        return bool(self.items) and all(
            item.publish_at_utc is not None and item.visibility is not None
            for item in self.items
        )

    @property
    def can_confirm(self) -> bool:
        return (
            self.can_import
            and not self.blocking_items
            and not self.blocking_issues
        )


def parse_local_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BatchFormatError(
            f"Invalid local date {value!r}; expected YYYY-MM-DD"
        ) from exc


def analyze_workbook(
    path: Path,
    *,
    name: str | None = None,
    local_date: date,
    timezone: str = BEIJING_TIMEZONE,
    minimum_gap_seconds: int,
    prepare_lead_seconds: int,
    daily_limit: int,
    now: datetime | None = None,
) -> BatchAnalysis:
    source_path = path.expanduser().resolve()
    if not source_path.is_file():
        raise BatchFormatError(f"Workbook does not exist: {source_path}")
    if source_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise BatchFormatError("Batch workbook must be an .xlsx or .xlsm file")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise BatchFormatError(f"Unknown timezone: {timezone}") from exc

    workbook = openpyxl.load_workbook(
        source_path,
        read_only=True,
        data_only=True,
    )
    try:
        if SHEET_NAME not in workbook.sheetnames:
            raise BatchFormatError(
                f"Workbook must contain the {SHEET_NAME!r} sheet"
            )
        sheet = workbook[SHEET_NAME]
        _validate_headers(sheet)

        rows: list[
            tuple[
                int,
                object,
                object,
                tuple[object, ...],
                object,
                object,
                object,
            ]
        ] = []
        for row_number, values in enumerate(
            sheet.iter_rows(
                min_row=FIRST_DATA_ROW,
                max_col=PUBLISH_TIME_COLUMN,
                values_only=True,
            ),
            FIRST_DATA_ROW,
        ):
            text_value = values[1] if len(values) > 1 else None
            image_values = values[
                IMAGE_FIRST_COLUMN - 1 : IMAGE_LAST_COLUMN
            ]
            if not str(text_value or "").strip() and not any(
                str(value or "").strip() for value in image_values
            ):
                continue
            visibility_value = (
                values[VISIBILITY_COLUMN - 1]
                if len(values) >= VISIBILITY_COLUMN
                else None
            )
            image_count_value = values[20] if len(values) >= 21 else None
            publish_time_value = (
                values[PUBLISH_TIME_COLUMN - 1]
                if len(values) >= PUBLISH_TIME_COLUMN
                else None
            )
            rows.append(
                (
                    row_number,
                    values[0] if values else None,
                    text_value,
                    image_values + (None,) * (MAX_IMAGES - len(image_values)),
                    image_count_value,
                    visibility_value,
                    publish_time_value,
                )
            )
    finally:
        workbook.close()

    if not rows:
        raise BatchFormatError("Workbook contains no batch records")

    now_utc = _as_utc(now or datetime.now(UTC))
    items = [
        _analyze_row(
            row_number=row_number,
            sequence_value=sequence,
            text_value=text,
            image_values=image_values,
            image_count_value=image_count,
            visibility_value=visibility,
            publish_time_value=publish_time,
            local_date=local_date,
            timezone=timezone,
            now_utc=now_utc,
            prepare_lead_seconds=prepare_lead_seconds,
        )
        for (
            row_number,
            sequence,
            text,
            image_values,
            image_count,
            visibility,
            publish_time,
        ) in rows
    ]

    global_issues: list[BatchIssue] = []
    items = _apply_duplicate_checks(items)
    items = _apply_schedule_checks(
        items,
        minimum_gap_seconds=minimum_gap_seconds,
        prepare_lead_seconds=prepare_lead_seconds,
        daily_limit=daily_limit,
        global_issues=global_issues,
    )

    fingerprint_payload = {
        "local_date": local_date.isoformat(),
        "timezone": timezone,
        "items": [
            {
                "sequence": item.sequence,
                "source_row": item.source_row,
                "fingerprint": item.source_fingerprint,
            }
            for item in items
        ],
    }
    source_fingerprint = sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    return BatchAnalysis(
        name=(name or source_path.stem).strip(),
        source_path=source_path,
        source_fingerprint=source_fingerprint,
        local_date=local_date,
        timezone=timezone,
        items=tuple(items),
        issues=tuple(global_issues),
    )


def build_draft_jobs(
    analysis: BatchAnalysis,
    batch_id: str,
    *,
    created_at: datetime | None = None,
    id_factory=None,
) -> list[Job]:
    if not analysis.can_import:
        raise BatchFormatError(
            "Batch cannot be imported until every row has a valid time and "
            "visibility"
        )
    timestamp = _as_utc(created_at or datetime.now(UTC))
    factory = id_factory or (lambda: str(uuid4()))
    jobs: list[Job] = []
    for item in analysis.items:
        if item.publish_at_utc is None or item.visibility is None:
            raise BatchFormatError("Batch item is missing required fields")
        jobs.append(
            Job(
                id=factory(),
                text=item.text,
                images=list(item.images),
                publish_at_utc=item.publish_at_utc,
                timezone=analysis.timezone,
                visibility=item.visibility,
                status=JobStatus.DRAFT,
                fingerprint="",
                created_at=timestamp,
                batch_id=batch_id,
                source_row=item.source_row,
                source_sequence=item.sequence,
                source_fingerprint=item.source_fingerprint,
            )
        )
    return jobs


def _validate_headers(sheet) -> None:
    expected = {
        1: "序号",
        2: "正文（必填）",
        IMAGE_FIRST_COLUMN: "图片1",
        IMAGE_LAST_COLUMN: "图片18",
        21: "图片数（自动）",
        VISIBILITY_COLUMN: "可见范围（必填）",
        PUBLISH_TIME_COLUMN: "发布时间（北京时间，可留空）",
    }
    mismatches = []
    for column, expected_value in expected.items():
        actual = str(sheet.cell(HEADER_ROW, column).value or "").strip()
        if actual != expected_value:
            mismatches.append(
                f"column {column}: expected {expected_value!r}, got {actual!r}"
            )
    if mismatches:
        raise BatchFormatError(
            "Workbook headers do not match the batch template: "
            + "; ".join(mismatches)
        )


def _analyze_row(
    *,
    row_number: int,
    sequence_value: object,
    text_value: object,
    image_values: tuple[object, ...],
    image_count_value: object,
    visibility_value: object,
    publish_time_value: object,
    local_date: date,
    timezone: str,
    now_utc: datetime,
    prepare_lead_seconds: int,
) -> BatchItem:
    issues: list[BatchIssue] = []
    sequence = str(sequence_value or "").strip()
    if not sequence:
        issues.append(
            BatchIssue("missing_sequence", "Sequence is required")
        )

    text = str(text_value or "").strip()
    if not text:
        issues.append(BatchIssue("missing_text", "Post body is required"))

    raw_visibility = str(visibility_value or "").strip().lower()
    visibility = None
    if raw_visibility in {Visibility.PUBLIC.value, Visibility.PRIVATE.value}:
        visibility = Visibility(raw_visibility)
    else:
        issues.append(
            BatchIssue(
                "invalid_visibility",
                "Visibility must be public or private",
            )
        )

    publish_at_utc = None
    try:
        local_publish_at = _parse_publish_time(
            publish_time_value,
            timezone,
        )
        if local_publish_at.date() != local_date:
            issues.append(
                BatchIssue(
                    "wrong_local_date",
                    f"Expected {local_date.isoformat()}, got "
                    f"{local_publish_at.date().isoformat()}",
                )
            )
        else:
            publish_at_utc = local_publish_at.astimezone(UTC)
    except ValueError as exc:
        local_publish_at = None
        issues.append(BatchIssue("invalid_publish_time", str(exc)))

    raw_images = tuple(
        _clean_cell_path(value) for value in image_values[:MAX_IMAGES]
    )
    nonempty_indexes = [
        index for index, value in enumerate(raw_images) if value
    ]
    if not nonempty_indexes:
        issues.append(
            BatchIssue("missing_images", "At least one image is required")
        )
    elif nonempty_indexes != list(range(len(nonempty_indexes))):
        issues.append(
            BatchIssue(
                "non_contiguous_images",
                "Image columns must be filled continuously from image 1",
            )
        )
    try:
        declared_image_count = int(image_count_value)
    except (TypeError, ValueError):
        issues.append(
            BatchIssue(
                "invalid_image_count",
                "Image count column must contain an integer",
            )
        )
    else:
        if declared_image_count != len(nonempty_indexes):
            issues.append(
                BatchIssue(
                    "image_count_mismatch",
                    f"Image count column says {declared_image_count}, "
                    f"but {len(nonempty_indexes)} image columns are filled",
                )
            )

    images: list[Path] = []
    for image_number, raw_path in enumerate(raw_images, 1):
        if not raw_path:
            continue
        if PATH_SEPARATOR_PATTERN.search(raw_path):
            issues.append(
                BatchIssue(
                    f"image{image_number}_multiple_paths",
                    "Each image column must contain exactly one path",
                )
            )
            continue
        image = Path(raw_path).expanduser()
        images.append(image)
        if not image.is_absolute():
            issues.append(
                BatchIssue(
                    f"image{image_number}_relative_path",
                    f"Image path must be absolute: {image}",
                )
            )
        if image.suffix.lower() not in SUPPORTED_IMAGES:
            issues.append(
                BatchIssue(
                    f"image{image_number}_unsupported_extension",
                    f"Unsupported image extension: {image}",
                )
            )
        if not image.exists():
            issues.append(
                BatchIssue(
                    f"image{image_number}_missing",
                    f"Image does not exist: {image}",
                )
            )
            continue
        if not image.is_file():
            directory_detail = ""
            if image.is_dir():
                try:
                    image_count = sum(
                        child.is_file()
                        and child.suffix.lower() in SUPPORTED_IMAGES
                        for child in image.iterdir()
                    )
                except OSError:
                    image_count = 0
                directory_detail = (
                    f" (directory contains {image_count} supported image "
                    "file(s); fill one path per image column)"
                )
            issues.append(
                BatchIssue(
                    f"image{image_number}_not_file",
                    f"Image path is not a file: {image}{directory_detail}",
                )
            )
            continue
        try:
            size = image.stat().st_size
            if size <= 0:
                issues.append(
                    BatchIssue(
                        f"image{image_number}_empty",
                        f"Image is empty: {image}",
                    )
                )
            elif size > MAX_IMAGE_BYTES:
                issues.append(
                    BatchIssue(
                        f"image{image_number}_over_20mb",
                        f"Image exceeds 20 MB ({size} bytes): {image}",
                    )
                )
            signature = _image_signature(image)
            suffix = image.suffix.lower()
            if signature is None:
                issues.append(
                    BatchIssue(
                        f"image{image_number}_invalid_signature",
                        f"File content is not a supported image: {image}",
                    )
                )
            elif signature not in SUPPORTED_IMAGES:
                issues.append(
                    BatchIssue(
                        f"image{image_number}_unsupported_content",
                        f"Detected unsupported image format {signature}: {image}",
                    )
                )
            elif not _extensions_compatible(signature, suffix):
                issues.append(
                    BatchIssue(
                        f"image{image_number}_format_mismatch",
                        f"File extension {suffix} does not match detected "
                        f"format {signature}: {image}",
                    )
                )
        except OSError as exc:
            issues.append(
                BatchIssue(
                    f"image{image_number}_unreadable",
                    f"Image cannot be read: {image} ({exc})",
                )
            )

    path_counts = Counter(_normalized_path(path) for path in images)
    for path, count in path_counts.items():
        if count > 1:
            issues.append(
                BatchIssue(
                    "duplicate_image_in_row",
                    f"Image appears more than once in this post: {path}",
                )
            )

    body_time = BODY_TIME_PATTERN.search(text)
    if body_time and local_publish_at is not None:
        body_hour = int(body_time.group(1))
        body_minute = int(body_time.group(2))
        if (body_hour, body_minute) != (
            local_publish_at.hour,
            local_publish_at.minute,
        ):
            issues.append(
                BatchIssue(
                    "body_time_mismatch",
                    f"Body time {body_hour:02d}:{body_minute:02d} does not "
                    f"match scheduled time "
                    f"{local_publish_at.hour:02d}:{local_publish_at.minute:02d}",
                )
            )

    if (
        publish_at_utc is not None
        and publish_at_utc
        < now_utc + timedelta(seconds=prepare_lead_seconds)
    ):
        issues.append(
            BatchIssue(
                "too_soon",
                "Scheduled time is inside the preparation window",
            )
        )

    source_fingerprint = _item_fingerprint(
        sequence=sequence,
        source_row=row_number,
        text=text,
        raw_images=raw_images,
        publish_at_utc=publish_at_utc,
        visibility=visibility,
    )
    return BatchItem(
        sequence=sequence,
        source_row=row_number,
        text=text,
        images=tuple(images),
        raw_images=raw_images,
        publish_at_utc=publish_at_utc,
        visibility=visibility,
        source_fingerprint=source_fingerprint,
        issues=tuple(issues),
    )


def _apply_duplicate_checks(items: list[BatchItem]) -> list[BatchItem]:
    text_counts = Counter(item.text for item in items if item.text)
    sequence_counts = Counter(item.sequence for item in items if item.sequence)
    path_rows: dict[str, list[str]] = {}
    for item in items:
        for image in item.images:
            path_rows.setdefault(_normalized_path(image), []).append(item.sequence)

    updated: list[BatchItem] = []
    for item in items:
        extra: list[BatchIssue] = []
        if item.sequence and sequence_counts[item.sequence] > 1:
            extra.append(
                BatchIssue(
                    "duplicate_sequence",
                    "Sequence is duplicated in another row",
                )
            )
        if item.text and text_counts[item.text] > 1:
            extra.append(
                BatchIssue(
                    "duplicate_text",
                    "Post body is duplicated in another row",
                )
            )
        duplicates = [
            path
            for path in {_normalized_path(image) for image in item.images}
            if len(path_rows[path]) > 1
        ]
        if duplicates:
            extra.append(
                BatchIssue(
                    "duplicate_image_across_rows",
                    "One or more images are reused by another post: "
                    + ", ".join(sorted(duplicates)),
                )
            )
        updated.append(
            BatchItem(
                sequence=item.sequence,
                source_row=item.source_row,
                text=item.text,
                images=item.images,
                raw_images=item.raw_images,
                publish_at_utc=item.publish_at_utc,
                visibility=item.visibility,
                source_fingerprint=item.source_fingerprint,
                issues=item.issues + tuple(extra),
            )
        )
    return updated


def _apply_schedule_checks(
    items: list[BatchItem],
    *,
    minimum_gap_seconds: int,
    prepare_lead_seconds: int,
    daily_limit: int,
    global_issues: list[BatchIssue],
) -> list[BatchItem]:
    scheduled = sorted(
        (
            (item.publish_at_utc, index, item)
            for index, item in enumerate(items)
            if item.publish_at_utc is not None
        ),
        key=lambda entry: entry[0],
    )
    extra_by_index: dict[int, list[BatchIssue]] = {}
    previous: BatchItem | None = None
    for publish_at, index, item in scheduled:
        if previous is not None and previous.publish_at_utc is not None:
            gap = int(
                (publish_at - previous.publish_at_utc).total_seconds()
            )
            if gap == 0:
                extra_by_index.setdefault(index, []).append(
                    BatchIssue(
                        "duplicate_publish_time",
                        "Publish time duplicates another post",
                    )
                )
            elif gap < minimum_gap_seconds:
                extra_by_index.setdefault(index, []).append(
                    BatchIssue(
                        "minimum_gap_violation",
                        f"Only {gap}s after sequence "
                        f"{previous.sequence}; required at least "
                        f"{minimum_gap_seconds}s",
                    )
                )
        previous = item

    if len(scheduled) > daily_limit:
        global_issues.append(
            BatchIssue(
                "daily_limit_exceeded",
                f"Batch has {len(scheduled)} scheduled posts; "
                f"local daily limit is {daily_limit}",
            )
        )
    if minimum_gap_seconds < prepare_lead_seconds:
        global_issues.append(
            BatchIssue(
                "gap_smaller_than_prepare_window",
                f"Minimum gap {minimum_gap_seconds}s is shorter than the "
                f"{prepare_lead_seconds}s preparation window",
            )
        )

    source_order = [
        item.publish_at_utc
        for item in items
        if item.publish_at_utc is not None
    ]
    if source_order != sorted(source_order):
        global_issues.append(
            BatchIssue(
                "source_order_not_chronological",
                "Workbook rows are not in chronological order",
                blocking=False,
            )
        )

    updated: list[BatchItem] = []
    for index, item in enumerate(items):
        extra = tuple(extra_by_index.get(index, ()))
        updated.append(
            BatchItem(
                sequence=item.sequence,
                source_row=item.source_row,
                text=item.text,
                images=item.images,
                raw_images=item.raw_images,
                publish_at_utc=item.publish_at_utc,
                visibility=item.visibility,
                source_fingerprint=item.source_fingerprint,
                issues=item.issues + extra,
            )
        )
    return updated


def _parse_publish_time(value: object, timezone: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, time):
        raise ValueError("Publish time is missing its date")  # noqa: TRY004
    else:
        text = str(value or "").strip().replace("：", ":")
        if not text:
            raise ValueError("Publish time is required")
        parsed = None
        for pattern in (
            "%Y-%m-%d-%H:%M",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(
                    f"Unsupported publish time format: {text!r}"
                ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(ZoneInfo(timezone))


def _clean_cell_path(value: object) -> str:
    text = str(value or "").strip()
    while (
        len(text) >= 2
        and text[0] == text[-1]
        and text[0] in {'"', "'", "“", "”"}
    ):
        text = text[1:-1].strip()
    return text


def _image_signature(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _extensions_compatible(signature: str, suffix: str) -> bool:
    if signature == ".jpg":
        return suffix in {".jpg", ".jpeg"}
    return signature == suffix


def _normalized_path(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path.absolute()).casefold()


def _item_fingerprint(
    *,
    sequence: str,
    source_row: int,
    text: str,
    raw_images: tuple[str, ...],
    publish_at_utc: datetime | None,
    visibility: Visibility | None,
) -> str:
    payload = {
        "sequence": sequence,
        "source_row": source_row,
        "text": text,
        "images": list(raw_images),
        "publish_at_utc": (
            publish_at_utc.isoformat() if publish_at_utc else None
        ),
        "visibility": visibility.value if visibility else None,
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
