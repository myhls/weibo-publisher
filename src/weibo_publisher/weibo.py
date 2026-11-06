from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from weibo_publisher.browser import (
    _NAVIGATION_TIMEOUT_MS,
    _SESSION_WAIT_TIMEOUT_SECONDS,
    HOME_URL,
    _wait_for_session,
    persistent_context,
)
from weibo_publisher.config import AppConfig
from weibo_publisher.models import Job, PublishResult
from weibo_publisher.validation import validate_images

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page

TEXT_SELECTORS = [
    'textarea[placeholder*="分享"]',
    'textarea[placeholder*="新鲜事"]',
    '[contenteditable="true"][class*="editor"]',
    '[contenteditable="true"]',
]
IMAGE_INPUT_SELECTORS = [
    'input[type="file"][accept*="image"]',
    'input[type="file"]',
]
PUBLISH_SELECTORS = [
    'button:has-text("发布")',
    'a:has-text("发布")',
    '[class*="submit"]:has-text("发布")',
]

_UPLOAD_INDICATOR_SELECTORS = (
    '[class*="upload"][class*="progress"]',
    '[class*="uploading"]',
    '[class*="upload"] [class*="loading"]',
    '[class*="pic_add"] [class*="loading"]',
)
_UPLOAD_TIMEOUT_SECONDS = 5.0
_UPLOAD_POLL_INTERVAL_SECONDS = 0.1


class WeiboPublisher:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def prepare(self, job: Job, artifact_dir: Path) -> None:
        await preview_job(self.config, job, artifact_dir)

    async def submit(self, job: Job, artifact_dir: Path) -> PublishResult:
        del job, artifact_dir
        raise NotImplementedError("Weibo submission is not implemented yet")

    async def close(self) -> None:
        return None


async def preview_job(
    config: AppConfig,
    job: Job,
    artifact_dir: Path,
) -> None:
    validate_images(job.images)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    async with persistent_context(config) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(
            HOME_URL,
            wait_until="domcontentloaded",
            timeout=_NAVIGATION_TIMEOUT_MS,
        )
        if not await _wait_for_session(page, _SESSION_WAIT_TIMEOUT_SECONDS):
            raise RuntimeError(
                "Weibo session is not valid or requires verification; run login first"
            )

        composer = await first_visible(page, TEXT_SELECTORS)
        tag_name = await composer.evaluate("(element) => element.tagName.toLowerCase()")
        contenteditable = await composer.get_attribute("contenteditable")
        if tag_name == "textarea":
            await composer.fill(job.text)
        elif contenteditable == "true":
            await composer.click()
            await page.keyboard.insert_text(job.text)
        else:
            raise RuntimeError("Matched Weibo control is not an editable composer")

        if job.images:
            image_input = await _first_present(page, IMAGE_INPUT_SELECTORS)
            await image_input.set_input_files([str(image) for image in job.images])
            await _wait_for_uploads(page)

        await page.screenshot(
            path=str(artifact_dir / "preview.png"),
            full_page=True,
        )


async def first_visible(page: Page, selectors: list[str]) -> Locator:
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    raise RuntimeError("No matching Weibo control found")


async def _first_present(page: Page, selectors: list[str]) -> Locator:
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count():
            return locator.first
    raise RuntimeError("No matching Weibo image input found")


async def _wait_for_uploads(page: Page) -> None:
    deadline = time.monotonic() + _UPLOAD_TIMEOUT_SECONDS
    while await _has_visible_indicator(page):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_UPLOAD_POLL_INTERVAL_SECONDS, remaining))


async def _has_visible_indicator(page: Page) -> bool:
    for selector in _UPLOAD_INDICATOR_SELECTORS:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return True
    return False
