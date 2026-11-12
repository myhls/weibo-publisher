from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from openpyxl import Workbook

from weibo_publisher.batch import (
    analyze_workbook,
    build_draft_jobs,
)
from weibo_publisher.models import JobStatus, Visibility


def _write_batch_workbook(
    path: Path,
    records: list[dict[str, object]],
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "发布清单"
    sheet.row_dimensions[8]
    headers = {
        1: "序号",
        2: "正文（必填）",
        3: "图片1",
        20: "图片18",
        21: "图片数（自动）",
        22: "可见范围（必填）",
        23: "发布时间（北京时间，可留空）",
    }
    for column, value in headers.items():
        sheet.cell(8, column, value)

    for offset, record in enumerate(records):
        row_number = 9 + offset
        sheet.cell(row_number, 1, record.get("sequence", f"{offset + 1:03d}"))
        sheet.cell(row_number, 2, record["text"])
        images = record.get("images", [])
        for index, image in enumerate(images):
            sheet.cell(
                row_number,
                3 + index,
                str(image) if image is not None else None,
            )
        sheet.cell(
            row_number,
            21,
            sum(image is not None for image in images),
        )
        sheet.cell(row_number, 22, record.get("visibility", "public"))
        sheet.cell(
            row_number,
            23,
            record.get("publish_time", "2026-11-11-10：00"),
        )
    workbook.save(path)


def _image(path: Path, kind: str = "jpeg") -> Path:
    if kind == "jpeg":
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg")
    elif kind == "png":
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png")
    else:
        raise ValueError(kind)
    return path


def test_analyze_workbook_accepts_quoted_images_and_fullwidth_time(
    tmp_path: Path,
) -> None:
    first = _image(tmp_path / "1.jpg")
    second = _image(tmp_path / "2.png", "png")
    workbook_path = tmp_path / "batch.xlsx"
    text = "关于银白色的一切║10:00\n下一棒:@测试"
    _write_batch_workbook(
        workbook_path,
        [
            {
                "text": text,
                "images": [f'"{first}"', f'"{second}"'],
                "publish_time": "2026-11-11-10：00",
            }
        ],
    )

    analysis = analyze_workbook(
        workbook_path,
        local_date=date(2026, 11, 11),
        minimum_gap_seconds=240,
        prepare_lead_seconds=180,
        daily_limit=100,
        now=datetime(2026, 11, 10, 12, tzinfo=UTC),
    )

    assert analysis.can_confirm
    assert len(analysis.items) == 1
    item = analysis.items[0]
    assert item.images == (first, second)
    assert item.publish_at_utc == datetime(2026, 11, 11, 2, tzinfo=UTC)
    assert item.visibility is Visibility.PUBLIC

    jobs = build_draft_jobs(analysis, "batch-1", id_factory=lambda: "job-1")
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.DRAFT
    assert jobs[0].batch_id == "batch-1"
    assert jobs[0].source_row == 9
    assert jobs[0].source_fingerprint == item.source_fingerprint


def test_analyze_workbook_blocks_schedule_and_duplicate_issues(
    tmp_path: Path,
) -> None:
    shared = _image(tmp_path / "shared.jpg")
    workbook_path = tmp_path / "batch.xlsx"
    _write_batch_workbook(
        workbook_path,
        [
            {
                "sequence": "001",
                "text": "第一条║10:00",
                "images": [shared],
                "publish_time": "2026-11-11-10：00",
            },
            {
                "sequence": "002",
                "text": "第二条║10:03",
                "images": [shared],
                "publish_time": "2026-11-11-10：03",
            },
        ],
    )

    analysis = analyze_workbook(
        workbook_path,
        local_date=date(2026, 11, 11),
        minimum_gap_seconds=240,
        prepare_lead_seconds=180,
        daily_limit=100,
        now=datetime(2026, 11, 10, 12, tzinfo=UTC),
    )

    assert not analysis.can_confirm
    codes = {
        issue.code for item in analysis.items for issue in item.blocking_issues
    }
    assert "minimum_gap_violation" in codes
    assert "duplicate_image_across_rows" in codes


def test_analyze_workbook_blocks_missing_non_contiguous_and_mismatched_images(
    tmp_path: Path,
) -> None:
    wrong_suffix = _image(tmp_path / "wrong.png")
    workbook_path = tmp_path / "batch.xlsx"
    _write_batch_workbook(
        workbook_path,
        [
            {
                "text": "测试║10:00",
                "images": [
                    tmp_path / "missing.jpg",
                    None,
                    wrong_suffix,
                ],
                "publish_time": "2026-11-11-10：00",
            }
        ],
    )

    analysis = analyze_workbook(
        workbook_path,
        local_date=date(2026, 11, 11),
        minimum_gap_seconds=240,
        prepare_lead_seconds=180,
        daily_limit=100,
        now=datetime(2026, 11, 10, 12, tzinfo=UTC),
    )

    codes = {issue.code for issue in analysis.items[0].blocking_issues}
    assert "missing_images" not in codes
    assert "image1_missing" in codes
    assert "non_contiguous_images" in codes
    assert "image3_format_mismatch" in codes
    assert not analysis.can_confirm


def test_analyze_workbook_blocks_body_time_mismatch(tmp_path: Path) -> None:
    image = _image(tmp_path / "1.jpg")
    workbook_path = tmp_path / "batch.xlsx"
    _write_batch_workbook(
        workbook_path,
        [
            {
                "text": "测试║10:01",
                "images": [image],
                "publish_time": "2026-11-11-10：00",
            }
        ],
    )

    analysis = analyze_workbook(
        workbook_path,
        local_date=date(2026, 11, 11),
        minimum_gap_seconds=240,
        prepare_lead_seconds=180,
        daily_limit=100,
        now=datetime(2026, 11, 10, 12, tzinfo=UTC),
    )

    codes = {issue.code for issue in analysis.items[0].blocking_issues}
    assert "body_time_mismatch" in codes
