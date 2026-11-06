from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack, suppress
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
from weibo_publisher.models import AttemptOutcome, Job, PublishResult
from weibo_publisher.validation import validate_images

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Locator, Page

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
_PUBLISH_RESPONSE_TIMEOUT_MS = 30_000
_PUBLISH_ENDPOINT = "/ajax/statuses/update"


class WeiboPublisher:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.page: Page | None = None
        self._context: BrowserContext | None = None
        self._exit_stack: AsyncExitStack | None = None

    async def prepare(self, job: Job, artifact_dir: Path) -> None:
        if self._exit_stack is not None:
            raise RuntimeError("Weibo publisher has already been prepared")

        images = validate_images(job.images)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        exit_stack = AsyncExitStack()
        self._exit_stack = exit_stack
        try:
            context = await exit_stack.enter_async_context(
                persistent_context(self.config)
            )
            self._context = context
            self.page = context.pages[0] if context.pages else await context.new_page()
            await _prepare_page(self.page, job.text, images, artifact_dir)
        except BaseException:
            await self.close()
            raise

    async def submit(self, job: Job, artifact_dir: Path) -> PublishResult:
        if self.page is None:
            raise RuntimeError("Weibo publisher has not been prepared")
        del job

        page = self.page
        button = await first_visible(page, PUBLISH_SELECTORS)
        try:
            async with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and _PUBLISH_ENDPOINT in response.url
                ),
                timeout=_PUBLISH_RESPONSE_TIMEOUT_MS,
            ) as response_info:
                await button.click()
            response = await response_info.value
            payload = await response.json()
            result = parse_publish_response(payload)
        except Exception as exc:  # noqa: BLE001 - every post-click failure is unknown
            await _screenshot_safely(page, artifact_dir / "submit-unknown.png")
            return PublishResult(
                outcome=AttemptOutcome.UNKNOWN,
                error=f"Submit outcome unknown: {exc}",
            )

        screenshot_name = (
            "submit-unknown.png"
            if result.outcome is AttemptOutcome.UNKNOWN
            else "submit-result.png"
        )
        await _screenshot_safely(page, artifact_dir / screenshot_name)
        return result

    async def close(self) -> None:
        exit_stack = self._exit_stack
        self._exit_stack = None
        self._context = None
        self.page = None
        if exit_stack is not None:
            await exit_stack.aclose()


async def preview_job(
    config: AppConfig,
    job: Job,
    artifact_dir: Path,
) -> None:
    images = validate_images(job.images)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    async with persistent_context(config) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await _prepare_page(page, job.text, images, artifact_dir)


async def _prepare_page(
    page: Page,
    text: str,
    images: list[Path],
    artifact_dir: Path,
) -> None:
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
        await composer.fill(text)
    elif contenteditable == "true":
        await composer.click()
        await page.keyboard.insert_text(text)
    else:
        raise RuntimeError("Matched Weibo control is not an editable composer")

    if images:
        image_input = await _first_present(page, IMAGE_INPUT_SELECTORS)
        await image_input.set_input_files([str(image) for image in images])
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


def parse_publish_response(payload: dict) -> PublishResult:
    if not isinstance(payload, dict):
        return PublishResult(
            outcome=AttemptOutcome.UNKNOWN,
            error="Publish response was not a JSON object",
        )

    code = payload.get("code")
    if str(code).strip() == "100000":
        data = payload.get("data")
        if not isinstance(data, dict):
            return PublishResult(
                outcome=AttemptOutcome.UNKNOWN,
                error="Success code without post ID",
            )

        raw_mid = data.get("idstr") or data.get("mid")
        if isinstance(raw_mid, bool) or not isinstance(raw_mid, (str, int)):
            return PublishResult(
                outcome=AttemptOutcome.UNKNOWN,
                error="Success code without post ID",
            )

        mid = str(raw_mid).strip()
        if not mid:
            return PublishResult(
                outcome=AttemptOutcome.UNKNOWN,
                error="Success code without post ID",
            )
        return PublishResult(
            outcome=AttemptOutcome.SUCCESS,
            weibo_mid=mid,
            weibo_url=f"https://weibo.com/detail/{mid}",
        )

    if code is None or not str(code).strip():
        return PublishResult(
            outcome=AttemptOutcome.UNKNOWN,
            error="Publish response did not include a status code",
        )

    return PublishResult(
        outcome=AttemptOutcome.FAILED,
        error=str(payload.get("msg") or payload.get("message") or payload),
    )


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


async def _screenshot_safely(page: Page, path: Path) -> None:
    with suppress(Exception):
        await page.screenshot(path=str(path), full_page=True)


async def _has_visible_indicator(page: Page) -> bool:
    for selector in _UPLOAD_INDICATOR_SELECTORS:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return True
    return False
