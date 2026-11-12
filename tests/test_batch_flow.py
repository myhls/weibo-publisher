from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import openpyxl
import pytest
from openpyxl import Workbook

from weibo_publisher.cli import main
from weibo_publisher.config import load_config
from weibo_publisher.models import (
    Batch,
    BatchStatus,
    Job,
    JobStatus,
    Visibility,
)
from weibo_publisher.repository import ConfirmationError, JobRepository


def _batch_job(
    job_id: str,
    batch_id: str,
    publish_at_utc: datetime,
) -> Job:
    return Job(
        id=job_id,
        text=f"post {job_id}",
        images=[],
        publish_at_utc=publish_at_utc,
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.DRAFT,
        fingerprint="",
        created_at=datetime(2026, 11, 10, 8, tzinfo=UTC),
        batch_id=batch_id,
        source_row=9,
        source_sequence=job_id,
        source_fingerprint=f"source-{job_id}",
    )


def _create_batch(
    repo: JobRepository,
    jobs: list[Job],
) -> Batch:
    batch = Batch(
        id=jobs[0].batch_id or "batch-1",
        name="Test batch",
        source_path=Path("C:/test/batch.xlsx"),
        source_fingerprint="batch-source",
        local_date=date(2026, 11, 11),
        timezone="Asia/Shanghai",
        status=BatchStatus.DRAFT,
        created_at=datetime(2026, 11, 10, 8, tzinfo=UTC),
    )
    repo.create_batch(batch, jobs)
    return batch


def test_confirm_batch_approves_all_jobs_atomically(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    jobs = [
        _batch_job(
            "job-1",
            "batch-1",
            datetime(2026, 11, 11, 2, 0, tzinfo=UTC),
        ),
        _batch_job(
            "job-2",
            "batch-1",
            datetime(2026, 11, 11, 2, 5, tzinfo=UTC),
        ),
    ]
    _create_batch(repo, jobs)

    confirmed = repo.confirm_batch(
        "batch-1",
        {"job-1": "fingerprint-1", "job-2": "fingerprint-2"},
        datetime(2026, 11, 10, 9, tzinfo=UTC),
        daily_limit=100,
        minimum_gap_seconds=240,
        allow_over_limit=False,
    )

    assert [job.id for job in confirmed] == ["job-1", "job-2"]
    assert all(job.status is JobStatus.APPROVED for job in confirmed)
    batch = repo.get_batch("batch-1")
    assert batch is not None
    assert batch.status is BatchStatus.APPROVED


def test_confirm_batch_rejects_small_gap_without_partial_approval(
    tmp_path: Path,
) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    jobs = [
        _batch_job(
            "job-1",
            "batch-1",
            datetime(2026, 11, 11, 2, 0, tzinfo=UTC),
        ),
        _batch_job(
            "job-2",
            "batch-1",
            datetime(2026, 11, 11, 2, 3, tzinfo=UTC),
        ),
    ]
    _create_batch(repo, jobs)

    with pytest.raises(ConfirmationError, match="minimum gap"):
        repo.confirm_batch(
            "batch-1",
            {"job-1": "fingerprint-1", "job-2": "fingerprint-2"},
            datetime(2026, 11, 10, 9, tzinfo=UTC),
            daily_limit=100,
            minimum_gap_seconds=240,
            allow_over_limit=False,
        )

    stored = repo.list_batch_jobs("batch-1")
    assert all(job.status is JobStatus.DRAFT for job in stored)
    assert repo.get_batch("batch-1").status is BatchStatus.DRAFT


def test_pause_and_resume_batch_controls_scheduler_gate(
    tmp_path: Path,
) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    job = _batch_job(
        "job-1",
        "batch-1",
        datetime(2026, 11, 11, 2, 0, tzinfo=UTC),
    )
    _create_batch(repo, [job])
    repo.confirm_batch(
        "batch-1",
        {"job-1": "fingerprint-1"},
        datetime(2026, 11, 10, 9, tzinfo=UTC),
        daily_limit=100,
        minimum_gap_seconds=240,
        allow_over_limit=False,
    )

    assert repo.batch_is_runnable("batch-1")
    assert repo.pause_batch("batch-1", "verification required")
    assert not repo.batch_is_runnable("batch-1")
    resumed = repo.resume_batch("batch-1")
    assert resumed.status is BatchStatus.APPROVED
    assert repo.batch_is_runnable("batch-1")


def test_cancel_draft_batch_cancels_jobs(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    jobs = [
        _batch_job(
            "job-1",
            "batch-1",
            datetime(2026, 11, 11, 2, 0, tzinfo=UTC),
        ),
        _batch_job(
            "job-2",
            "batch-1",
            datetime(2026, 11, 11, 2, 5, tzinfo=UTC),
        ),
    ]
    _create_batch(repo, jobs)

    cancelled = repo.cancel_batch("batch-1")

    assert cancelled.status is BatchStatus.CANCELLED
    assert all(
        job.status is JobStatus.CANCELLED
        for job in repo.list_batch_jobs("batch-1")
    )


def _write_cli_batch(path: Path, images: list[Path]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "发布清单"
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
    for offset, (sequence, local_time) in enumerate(
        (("001", "10:00"), ("002", "10:05")),
    ):
        row = 9 + offset
        sheet.cell(row, 1, sequence)
        sheet.cell(row, 2, f"测试║{local_time}")
        sheet.cell(row, 3, str(images[offset]))
        sheet.cell(row, 21, 1)
        sheet.cell(row, 22, "public")
        sheet.cell(row, 23, f"2026-11-20-{local_time.replace(':', '：')}")
    workbook.save(path)


def test_cli_batch_import_and_explicit_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    images = [tmp_path / "image-1.jpg", tmp_path / "image-2.jpg"]
    for image in images:
        image.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    workbook = tmp_path / "batch.xlsx"
    _write_cli_batch(workbook, images)
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "batch-import",
                "--file",
                str(workbook),
                "--date",
                "2026-11-20",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Records: 2" in output
    assert "No jobs were approved" in output

    repo = JobRepository(load_config(root=tmp_path).db_path)
    batch = repo.list_batches()[0]
    jobs = repo.list_batch_jobs(batch.id)
    assert all(job.status is JobStatus.DRAFT for job in jobs)
    assert main(["confirm", jobs[0].id]) == 2

    assert main(["batch-confirm", batch.id]) == 2
    assert main(["batch-confirm", batch.id, "--yes"]) == 0
    approved = repo.list_batch_jobs(batch.id)
    assert all(job.status is JobStatus.APPROVED for job in approved)


def test_cli_batch_confirm_rejects_changed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = [tmp_path / "image-1.jpg", tmp_path / "image-2.jpg"]
    for image in images:
        image.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    workbook_path = tmp_path / "batch.xlsx"
    _write_cli_batch(workbook_path, images)
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "batch-import",
                "--file",
                str(workbook_path),
                "--date",
                "2026-11-20",
            ]
        )
        == 0
    )
    repo = JobRepository(load_config(root=tmp_path).db_path)
    batch = repo.list_batches()[0]

    workbook = openpyxl.load_workbook(workbook_path)
    workbook["发布清单"]["B9"] = "changed body║10:00"
    workbook.save(workbook_path)
    workbook.close()

    assert main(["batch-confirm", batch.id, "--yes"]) == 2
    assert all(
        job.status is JobStatus.DRAFT
        for job in repo.list_batch_jobs(batch.id)
    )


def test_cli_batch_import_replace_reuses_one_draft_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = [tmp_path / "image-1.jpg", tmp_path / "image-2.jpg"]
    for image in images:
        image.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    workbook = tmp_path / "batch.xlsx"
    _write_cli_batch(workbook, images)
    monkeypatch.chdir(tmp_path)
    args = [
        "batch-import",
        "--file",
        str(workbook),
        "--date",
        "2026-11-20",
    ]

    assert main(args) == 0
    repo = JobRepository(load_config(root=tmp_path).db_path)
    original_batch = repo.list_batches()[0]
    assert main(args) == 2
    assert main([*args, "--replace"]) == 0

    batches = repo.list_batches()
    assert len(batches) == 1
    assert batches[0].id == original_batch.id
    assert len(repo.list_batch_jobs(batches[0].id)) == 2
