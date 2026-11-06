from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from weibo_publisher.models import (
    AttemptOutcome,
    Job,
    JobStatus,
    PublishResult,
    Visibility,
)
from weibo_publisher.repository import JobRepository
from weibo_publisher.scheduler import JobExecutor
from weibo_publisher.validation import build_fingerprint


class LocalFilePublisher:
    def __init__(self, page_url: str):
        self.page_url = page_url
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.published = False
        self.click_count = 0
        self.clicks_after_prepare = 0
        self.prepared_at = None
        self.submit_invoked_at = None
        self.request_urls: list[str] = []
        self.blocked_network_urls: list[str] = []

    async def prepare(self, job, artifact_dir):
        del artifact_dir
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()

        async def block_network(route):
            self.blocked_network_urls.append(route.request.url)
            await route.abort()

        await self.context.route("http://**", block_network)
        await self.context.route("https://**", block_network)
        self.page = await self.context.new_page()
        self.page.on("request", lambda request: self.request_urls.append(request.url))
        await self.page.goto(self.page_url)
        await self.page.fill("#text", job.text)
        if job.images:
            await self.page.set_input_files(
                "#image",
                [str(path) for path in job.images],
            )
        self.clicks_after_prepare = await self.page.evaluate(
            "window.publishClickCount ?? 0"
        )
        self.prepared_at = datetime.now(UTC)

    async def submit(self, job, artifact_dir):
        del job, artifact_dir
        self.submit_invoked_at = datetime.now(UTC)
        await self.page.click("#publish")
        await self.page.wait_for_function(
            "document.body.dataset.published === 'true'"
        )
        self.published = True
        self.click_count = await self.page.evaluate("window.publishClickCount")
        return PublishResult(
            outcome=AttemptOutcome.SUCCESS,
            weibo_mid="mock-123",
            weibo_url="https://example.test/detail/mock-123",
        )

    async def close(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        self.context = None
        self.page = None


def make_approved_job(tmp_path: Path) -> Job:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nlocal-e2e")
    publish_at = datetime.now(UTC) + timedelta(seconds=5)
    return Job(
        id="job-local-1",
        text="可爱",
        images=[image],
        publish_at_utc=publish_at,
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.APPROVED,
        fingerprint=build_fingerprint(
            "可爱",
            [image],
            publish_at,
            Visibility.PUBLIC,
        ),
        created_at=datetime.now(UTC),
        confirmed_at=datetime.now(UTC),
    )


def fixture_url() -> str:
    return (Path(__file__).parent / "fixtures" / "mock_publish.html").as_uri()


@pytest.mark.asyncio
async def test_prepare_fills_text_attaches_image_and_does_not_publish(
    tmp_path: Path,
) -> None:
    publisher = LocalFilePublisher(fixture_url())
    job = make_approved_job(tmp_path)

    try:
        await publisher.prepare(job, tmp_path)

        assert await publisher.page.input_value("#text") == job.text
        assert await publisher.page.eval_on_selector(
            "#image",
            "(input) => Array.from(input.files, (file) => file.name)",
        ) == ["image.png"]
        assert await publisher.page.evaluate(
            "document.body.dataset.published || ''"
        ) == ""
        assert await publisher.page.evaluate("window.publishClickCount ?? 0") == 0
        assert publisher.published is False
        assert publisher.blocked_network_urls == []
        assert publisher.request_urls
        assert all(url.startswith("file:") for url in publisher.request_urls)
    finally:
        await publisher.close()


@pytest.mark.asyncio
async def test_executor_publishes_once_records_result_and_stays_local(
    app_config,
    tmp_path: Path,
) -> None:
    app_config.runtime_dir.mkdir(parents=True)
    repo = JobRepository(app_config.db_path)
    repo.initialize()
    job = make_approved_job(tmp_path)
    repo.create_job(job)
    publisher = LocalFilePublisher(fixture_url())

    await JobExecutor(
        app_config,
        repo,
        lambda: publisher,
    ).execute(job.id)

    stored = repo.get_job(job.id)
    attempts = repo.list_attempts(job.id)
    assert stored is not None
    assert stored.status is JobStatus.PUBLISHED
    assert stored.weibo_mid == "mock-123"
    assert stored.weibo_url == "https://example.test/detail/mock-123"
    assert stored.published_at is not None
    assert attempts[0]["outcome"] == AttemptOutcome.SUCCESS.value
    assert publisher.clicks_after_prepare == 0
    assert publisher.click_count == 1
    assert publisher.published is True
    assert publisher.prepared_at is not None
    assert publisher.prepared_at < job.publish_at_utc
    assert publisher.submit_invoked_at is not None
    assert publisher.submit_invoked_at >= job.publish_at_utc
    assert publisher.blocked_network_urls == []
    assert publisher.request_urls
    assert all(url.startswith("file:") for url in publisher.request_urls)
    assert not app_config.profile_dir.exists()
