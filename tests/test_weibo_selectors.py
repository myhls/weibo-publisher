from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, async_playwright

from weibo_publisher import weibo
from weibo_publisher.cli import main
from weibo_publisher.config import AppConfig, load_config
from weibo_publisher.models import Job, JobStatus, Visibility
from weibo_publisher.repository import JobRepository
from weibo_publisher.weibo import (
    IMAGE_INPUT_SELECTORS,
    PUBLISH_SELECTORS,
    TEXT_SELECTORS,
)


def test_text_selectors_include_contenteditable() -> None:
    assert any("contenteditable" in selector for selector in TEXT_SELECTORS)


def test_image_selectors_target_file_input() -> None:
    assert any('type="file"' in selector for selector in IMAGE_INPUT_SELECTORS)


def test_publish_selectors_include_text_fallback() -> None:
    assert any("发布" in selector for selector in PUBLISH_SELECTORS)


@pytest.mark.asyncio
async def test_weibo_publisher_prepare_fills_textarea_attaches_all_images_and_never_publishes(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_image = tmp_path / "first.png"
    second_image = tmp_path / "second.png"
    first_image.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    second_image.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    observed: dict[str, object] = {}
    page_html = _html(
        """
        <header><a class="gn_name" href="/u/123">Account</a></header>
        <main>
          <textarea placeholder="分享" style="display:none"></textarea>
          <textarea id="body" placeholder="分享新鲜事"></textarea>
          <input
            id="image-input"
            type="file"
            accept="image/*"
            multiple
            style="display:none"
          >
          <button id="publish" type="button">发布</button>
        </main>
        """,
        _PUBLISH_TRIPWIRE,
    )
    monkeypatch.setattr(
        weibo,
        "persistent_context",
        _observing_routed_context_factory(
            {"https://weibo.com/": page_html},
            observed,
        ),
    )
    artifact_dir = app_config.artifacts_dir / "job-1"
    job = _job(images=[first_image, second_image])

    publisher = weibo.WeiboPublisher(_local_config(app_config))
    try:
        await publisher.prepare(job, artifact_dir)
        assert publisher.page is not None
        assert not publisher.page.is_closed()
        await _observe_page(publisher.page, observed)
    finally:
        await publisher.close()

    assert observed["textarea"] == job.text
    assert observed["contenteditable"] is None
    assert observed["files"] == ["first.png", "second.png"]
    assert observed["publish_tripwire_armed"] is True
    assert observed["page_errors"] == []
    assert observed["publish_clicked"] is None
    assert observed["context_closed"] is True
    assert (artifact_dir / "preview.png").read_bytes().startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_weibo_publisher_close_releases_retained_context(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    page_html = _html(
        """
        <header><a class="gn_name" href="/u/123">Account</a></header>
        <main>
          <textarea id="body" placeholder="分享新鲜事"></textarea>
          <button id="publish" type="button">发布</button>
        </main>
        """,
        _PUBLISH_TRIPWIRE,
    )
    monkeypatch.setattr(
        weibo,
        "persistent_context",
        _observing_routed_context_factory(
            {"https://weibo.com/": page_html},
            observed,
        ),
    )
    publisher = weibo.WeiboPublisher(_local_config(app_config))

    await publisher.prepare(_job(), app_config.artifacts_dir / "job-close")

    assert publisher.page is not None
    assert not publisher.page.is_closed()
    assert observed["context_closed"] is False

    await publisher.close()

    assert publisher.page is None
    assert observed["context_closed"] is True

    await publisher.close()
    assert observed["close_count"] == 1


@pytest.mark.asyncio
async def test_preview_job_inserts_text_into_first_visible_contenteditable(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    page_html = _html(
        """
        <header><a class="gn_name" href="/u/123">Account</a></header>
        <main>
          <div class="editor" contenteditable="true" style="display:none"></div>
          <div id="body" class="editor" contenteditable="true"></div>
          <button id="publish" type="button">发布</button>
        </main>
        """,
        _PUBLISH_TRIPWIRE,
    )
    monkeypatch.setattr(
        weibo,
        "persistent_context",
        _observing_routed_context_factory(
            {"https://weibo.com/": page_html},
            observed,
        ),
    )
    artifact_dir = app_config.artifacts_dir / "job-2"
    job = _job(text="contenteditable test")

    await weibo.preview_job(_local_config(app_config), job, artifact_dir)

    assert observed["textarea"] is None
    assert observed["contenteditable"] == job.text
    assert observed["files"] == []
    assert observed["publish_tripwire_armed"] is True
    assert observed["page_errors"] == []
    assert observed["publish_clicked"] is None
    assert (artifact_dir / "preview.png").is_file()


@pytest.mark.asyncio
async def test_preview_job_uses_normalized_image_paths(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    image = tmp_path / "normalized.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nnormalized")
    page_html = _html(
        """
        <header><a class="gn_name" href="/u/123">Account</a></header>
        <main>
          <textarea id="body" placeholder="分享新鲜事"></textarea>
          <input id="image-input" type="file" accept="image/*" multiple>
          <button id="publish" type="button">发布</button>
        </main>
        """,
        _PUBLISH_TRIPWIRE,
    )
    monkeypatch.setattr(
        weibo,
        "persistent_context",
        _observing_routed_context_factory(
            {"https://weibo.com/": page_html},
            observed,
        ),
    )
    monkeypatch.setattr(
        weibo,
        "validate_images",
        lambda paths: [image.resolve()],
    )
    job = _job(images=[Path("not-the-normalized-path.png")])

    await weibo.preview_job(
        _local_config(app_config),
        job,
        app_config.artifacts_dir / "job-normalized",
    )

    assert observed["files"] == ["normalized.png"]
    assert observed["publish_tripwire_armed"] is True
    assert observed["page_errors"] == []
    assert observed["publish_clicked"] is None


@pytest.mark.asyncio
async def test_preview_job_rejects_invalid_session_before_entering_text(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    page_html = _html(
        """
        <main>
          <textarea id="body" placeholder="分享新鲜事"></textarea>
          <button id="publish" type="button">发布</button>
        </main>
        """,
        _PUBLISH_TRIPWIRE,
    )
    monkeypatch.setattr(
        weibo,
        "persistent_context",
        _observing_routed_context_factory(
            {"https://weibo.com/": page_html},
            observed,
        ),
    )
    job = _job(text="must not be entered")

    with pytest.raises(RuntimeError, match="session"):
        await weibo.preview_job(
            _local_config(app_config),
            job,
            app_config.artifacts_dir / "job-3",
        )

    assert observed["textarea"] == ""
    assert observed["publish_tripwire_armed"] is True
    assert observed["page_errors"] == []
    assert observed["publish_clicked"] is None


def test_preview_cli_reports_missing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["preview", "missing"]) == 2
    assert "job not found: missing" in capsys.readouterr().out


@pytest.mark.parametrize(
    "status",
    [JobStatus.CANCELLED, JobStatus.PUBLISHED, JobStatus.READY],
)
def test_preview_cli_rejects_invalid_states(
    status: JobStatus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = _create_job(tmp_path, status=status)
    monkeypatch.chdir(tmp_path)

    assert main(["preview", job.id]) == 2
    output = capsys.readouterr().out
    assert "cannot preview" in output
    assert status.value in output


@pytest.mark.parametrize("status", [JobStatus.DRAFT, JobStatus.APPROVED])
def test_preview_cli_passes_job_and_artifact_directory_to_adapter(
    status: JobStatus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = _create_job(tmp_path, status=status)
    config = load_config(root=tmp_path)
    captured: dict[str, object] = {}

    async def fake_preview(
        actual_config: AppConfig,
        actual_job: Job,
        artifact_dir: Path,
    ) -> None:
        captured["config"] = actual_config
        captured["job"] = actual_job
        captured["artifact_dir"] = artifact_dir

    monkeypatch.setattr(weibo, "preview_job", fake_preview)
    monkeypatch.chdir(tmp_path)

    assert main(["preview", job.id]) == 0

    assert captured["config"] == config
    assert captured["job"].id == job.id
    assert captured["artifact_dir"] == config.artifacts_dir / job.id
    assert "Preview saved" in capsys.readouterr().out


_PUBLISH_TRIPWIRE = """
<script>
  window.addEventListener("DOMContentLoaded", () => {
    const publish = document.querySelector("#publish");
    if (!publish) {
      throw new Error("Publish tripwire target is missing");
    }
    publish.addEventListener("click", () => {
      document.body.dataset.publishClicked = "true";
    });
    publish.dataset.tripwireArmed = "true";
  });
</script>
"""


def _html(body: str, script: str = "") -> str:
    return f"<!doctype html><html><head>{script}</head><body>{body}</body></html>"


def _local_config(config: AppConfig) -> AppConfig:
    return replace(config, browser_channel="", headless=True)


def _observing_routed_context_factory(
    routes: dict[str, str],
    observed: dict[str, object],
):
    observed["context_closed"] = False
    observed["close_count"] = 0

    @asynccontextmanager
    async def routed_context(config: AppConfig) -> AsyncIterator[BrowserContext]:
        del config
        async with async_playwright() as playwright:
            chrome = await playwright.chromium.launch(headless=True)
            context = await chrome.new_context()
            for url, body in routes.items():
                await context.route(url, _html_route(body))
            page = await context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            observed["page_errors"] = page_errors
            observed["context"] = context
            try:
                yield context
            finally:
                observed["context_closed"] = True
                observed["close_count"] = int(observed["close_count"]) + 1
                await _observe_page(page, observed)
                await chrome.close()

    return routed_context


async def _observe_page(page, observed: dict[str, object]) -> None:
    textarea = page.locator("#body")
    if await textarea.count() and await textarea.first.get_attribute(
        "contenteditable"
    ) is None:
        observed["textarea"] = await textarea.first.input_value()
        observed["contenteditable"] = None
    elif await textarea.count():
        observed["textarea"] = None
        observed["contenteditable"] = await textarea.first.text_content()
    else:
        observed["textarea"] = None
        observed["contenteditable"] = None

    image_input = page.locator("#image-input")
    if await image_input.count():
        observed["files"] = await image_input.first.evaluate(
            "(input) => Array.from(input.files).map((file) => file.name)"
        )
    else:
        observed["files"] = []
    publish = page.locator("#publish")
    observed["publish_tripwire_armed"] = (
        bool(await publish.count())
        and await publish.first.get_attribute("data-tripwire-armed") == "true"
    )
    observed["publish_clicked"] = await page.evaluate(
        "document.body.dataset.publishClicked ?? null"
    )


def _html_route(body: str):
    async def fulfill(route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=body,
        )

    return fulfill


def _job(
    *,
    text: str = "preview text",
    images: list[Path] | None = None,
    status: JobStatus = JobStatus.DRAFT,
) -> Job:
    return Job(
        id="job-1",
        text=text,
        images=images or [],
        publish_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=status,
        fingerprint="fingerprint" if status is JobStatus.APPROVED else "",
        created_at=datetime.now(UTC),
    )


def _create_job(tmp_path: Path, *, status: JobStatus) -> Job:
    config = load_config(root=tmp_path)
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    repo = JobRepository(config.db_path)
    repo.initialize()
    job = _job(status=status)
    repo.create_job(job)
    return job
