from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
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
from weibo_publisher.models import AttemptOutcome, Job, PublishResult, Visibility
from weibo_publisher.validation import normalize_text, validate_images

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
VISIBILITY_SELECTORS = [
    '[data-testid="visibility"]',
    '[id*="visibility"]',
    '[data-visibility]',
    'button[aria-label*="可见"]',
    '[role="button"][aria-label*="可见"]',
    'button:has-text("公开")',
    'button:has-text("粉丝")',
    'button:has-text("好友圈")',
    'button:has-text("仅自己可见")',
    'button:has-text("群可见")',
    '[class*="visibility"]',
]

_UPLOAD_INDICATOR_SELECTORS = (
    '[class*="upload"][class*="progress"]',
    '[class*="uploading"]',
    '[data-upload-state="uploading"]',
    '[class*="upload"] [aria-busy="true"]',
    '[class*="upload"] [class*="loading"]',
    '[class*="pic_add"] [class*="loading"]',
)
_UPLOAD_TIMEOUT_SECONDS = 5.0
_UPLOAD_POLL_INTERVAL_SECONDS = 0.1
_PREVIEW_IMAGE_TIMEOUT_SECONDS = 30.0
_PREVIEW_IMAGE_POLL_INTERVAL_SECONDS = 0.2
_PUBLISH_RESPONSE_TIMEOUT_MS = 30_000
_PUBLISH_ENDPOINT = "/ajax/statuses/update"
_PUBLIC_VISIBILITY_LABELS = frozenset({"公开", "公开可见", "所有人可见"})
_PRIVATE_VISIBILITY_LABEL = "仅自己可见"
_OTHER_VISIBILITY_LABELS = frozenset({"粉丝", "好友圈", "群可见"})
_KNOWN_VISIBILITY_LABELS = (
    _PUBLIC_VISIBILITY_LABELS
    | {_PRIVATE_VISIBILITY_LABEL}
    | _OTHER_VISIBILITY_LABELS
)
_VISIBILITY_TIMEOUT_SECONDS = 3.0
_VISIBILITY_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class _AttachedImageIdentity:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _PreparedComposerState:
    text: str
    image_paths: tuple[str, ...]
    visibility: Visibility
    attached_images: tuple[_AttachedImageIdentity, ...]


class WeiboPublisher:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.page: Page | None = None
        self._context: BrowserContext | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._prepared_state: _PreparedComposerState | None = None

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
            await _prepare_page(
                self.page,
                job.text,
                images,
                job.visibility,
                artifact_dir,
            )
            attached_images = await _read_attached_image_identities(self.page)
            if len(attached_images) != len(images):
                raise RuntimeError(
                    f"expected {len(images)} attached image(s), "
                    f"found {len(attached_images)} after preparation"
                )
            self._prepared_state = _PreparedComposerState(
                text=normalize_text(job.text),
                image_paths=tuple(str(image) for image in images),
                visibility=job.visibility,
                attached_images=attached_images,
            )
        except BaseException:
            await self.close()
            raise

    async def submit(self, job: Job, artifact_dir: Path) -> PublishResult:
        if self.page is None:
            raise RuntimeError("Weibo publisher has not been prepared")

        page = self.page
        try:
            images = validate_images(job.images)
            if job.visibility not in {Visibility.PUBLIC, Visibility.PRIVATE}:
                raise RuntimeError("followers visibility is not supported yet")
            prepared_state = self._prepared_state
            if prepared_state is None:
                raise RuntimeError("Weibo publisher has not been prepared")
            if (
                prepared_state.text != normalize_text(job.text)
                or prepared_state.image_paths
                != tuple(str(image) for image in images)
                or prepared_state.visibility is not job.visibility
            ):
                raise RuntimeError(
                    "submit job does not match the prepared composer state"
                )
            await _validate_submit_page(
                page,
                job,
                prepared_state.attached_images,
                job.visibility,
            )
            button = await first_visible(page, PUBLISH_SELECTORS)
        except Exception as exc:  # noqa: BLE001 - pre-click failures are definite
            await _screenshot_safely(
                page,
                artifact_dir / "submit-validation-failed.png",
            )
            return PublishResult(
                outcome=AttemptOutcome.FAILED,
                error=f"Pre-submit validation failed: {exc}",
            )

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
        self._prepared_state = None
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
        await _prepare_page(
            page,
            job.text,
            images,
            job.visibility,
            artifact_dir,
        )


async def _prepare_page(
    page: Page,
    text: str,
    images: list[Path],
    visibility: Visibility,
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
        await _wait_for_uploaded_image_previews(page, len(images))

    await _ensure_visibility(page, visibility)

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
    raw_code = str(code).strip() if code is not None else ""
    ok = payload.get("ok")

    if _is_ok_failure(ok) or (raw_code and raw_code != "100000"):
        return PublishResult(
            outcome=AttemptOutcome.FAILED,
            error=_response_message(payload),
        )

    ok_success = _is_ok_success(ok)
    legacy_success = raw_code == "100000"
    if ok_success or legacy_success:
        mid = _post_id_from_payload(payload)
        if mid is None:
            return PublishResult(
                outcome=AttemptOutcome.UNKNOWN,
                error=(
                    "Success response without post ID"
                    if ok_success
                    else "Success code without post ID"
                ),
            )
        return PublishResult(
            outcome=AttemptOutcome.SUCCESS,
            weibo_mid=mid,
            weibo_url=f"https://weibo.com/detail/{mid}",
        )

    return PublishResult(
        outcome=AttemptOutcome.UNKNOWN,
        error="Publish response did not include a recognized outcome",
    )


def _is_ok_success(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true"}
    return False


def _is_ok_failure(value: object) -> bool:
    if value is False:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false"}
    return False


def _response_message(payload: dict) -> str:
    message = payload.get("msg") or payload.get("message")
    if message is None or not str(message).strip():
        return "Weibo rejected the publish request"
    return str(message)


def _post_id_from_payload(payload: dict) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("idstr", "mid"):
        raw_mid = data.get(key)
        if isinstance(raw_mid, bool) or not isinstance(raw_mid, (str, int)):
            continue
        mid = str(raw_mid).strip()
        if mid:
            return mid
    return None


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
            raise RuntimeError("Image upload did not complete before timeout")
        await asyncio.sleep(min(_UPLOAD_POLL_INTERVAL_SECONDS, remaining))


async def _wait_for_uploaded_image_previews(
    page: Page,
    expected_count: int,
    *,
    timeout_seconds: float | None = None,
) -> None:
    if expected_count <= 0:
        return

    composer = await first_visible(page, TEXT_SELECTORS)
    wait_seconds = (
        _PREVIEW_IMAGE_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    deadline = time.monotonic() + max(wait_seconds, 0)
    loaded_count = 0
    while True:
        loaded_count = await _count_loaded_composer_previews(composer)
        if loaded_count >= expected_count:
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Expected {expected_count} uploaded image preview(s) in the "
                f"composer, but only {loaded_count} loaded before timeout"
            )
        await asyncio.sleep(min(_PREVIEW_IMAGE_POLL_INTERVAL_SECONDS, remaining))


async def _count_loaded_composer_previews(composer: Locator) -> int:
    raw_count = await composer.evaluate(
        """
        (composer) => {
          const normalize = (value) =>
            String(value ?? "").replace(/\\s+/g, " ").trim();
          const publicLabels = new Set(["公开", "公开可见", "所有人可见"]);
          const publishWords = ["发布", "发送"];
          const hasPublishControl = (root) => {
            const controls = root.querySelectorAll(
              'button, a, [role="button"], [class*="submit"]'
            );
            for (const control of controls) {
              if (!(control instanceof HTMLElement)) {
                continue;
              }
              const labels = [
                normalize(control.innerText || control.textContent || ""),
                normalize(control.getAttribute("aria-label") || ""),
              ];
              if (
                labels.some((label) =>
                  publishWords.some((word) => label.includes(word))
                )
              ) {
                return true;
              }
            }
            return false;
          };
          const hasPublicVisibilityLabel = (root) => {
            const controls = root.querySelectorAll(
              '[data-testid="visibility"], [data-visibility], ' +
              '[aria-label*="可见"], button, [role="button"], ' +
              '[class*="visibility"]'
            );
            for (const control of controls) {
              if (!(control instanceof HTMLElement)) {
                continue;
              }
              const labels = [
                normalize(control.innerText || control.textContent || ""),
                normalize(control.getAttribute("aria-label") || ""),
                normalize(control.getAttribute("data-visibility") || ""),
              ];
              if (labels.some((label) => publicLabels.has(label))) {
                return true;
              }
            }
            return false;
          };

          let root = composer.parentElement;
          while (
            root &&
            root !== document.body &&
            root !== document.documentElement &&
            !hasPublishControl(root) &&
            !hasPublicVisibilityLabel(root)
          ) {
            root = root.parentElement;
          }
          if (!root || root === document.body || root === document.documentElement) {
            return 0;
          }

          const images = root.querySelectorAll('img[class*="woo-picture-img"]');
          let count = 0;
          for (const image of images) {
            let url;
            try {
              url = new URL(image.src, document.baseURI);
            } catch {
              continue;
            }
            if (
              url.protocol === "https:" &&
              /^wx[^.]*\\.sinaimg\\.cn$/i.test(url.hostname) &&
              image.complete === true &&
              image.naturalWidth > 0
            ) {
              count += 1;
            }
          }
          return count;
        }
        """
    )
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        raise TypeError("composer preview count was not an integer")
    return raw_count


async def _validate_submit_page(
    page: Page,
    job: Job,
    prepared_images: tuple[_AttachedImageIdentity, ...],
    visibility: Visibility,
) -> None:
    composer = await first_visible(page, TEXT_SELECTORS)
    composer_text = await _read_composer_text(composer)
    if normalize_text(composer_text) != normalize_text(job.text):
        raise RuntimeError("composer text does not match the prepared job")

    attached_images = await _read_attached_image_identities(page)
    expected_images = len(job.images)
    if len(attached_images) != expected_images:
        raise RuntimeError(
            f"expected {expected_images} attached image(s), "
            f"found {len(attached_images)}"
        )
    if attached_images != prepared_images:
        raise RuntimeError(
            "attached image content or metadata does not match the prepared job"
        )

    if await _has_visible_indicator(page):
        raise RuntimeError("image upload has not completed")

    visibility_label = await _read_visibility_label(page)
    if visibility_label is None:
        raise RuntimeError("visibility could not be verified before submit")
    expected_labels = _visibility_labels_for(visibility)
    if visibility_label not in expected_labels:
        raise RuntimeError(
            f"visibility does not match {visibility.value} job: "
            f"{visibility_label}"
        )


async def _read_composer_text(composer: Locator) -> str:
    tag_name = await composer.evaluate(
        "(element) => element.tagName.toLowerCase()"
    )
    if tag_name == "textarea":
        return await composer.input_value()
    return await composer.text_content() or ""


async def _read_attached_image_identities(
    page: Page,
) -> tuple[_AttachedImageIdentity, ...]:
    for selector in IMAGE_INPUT_SELECTORS:
        locator = page.locator(selector)
        if await locator.count():
            try:
                raw_identities = await locator.first.evaluate(
                    """
                    async (input) => {
                      if (!globalThis.crypto?.subtle) {
                        throw new Error("crypto.subtle is unavailable");
                      }
                      const identities = [];
                      for (const file of Array.from(input.files ?? [])) {
                        const digest = await globalThis.crypto.subtle.digest(
                          "SHA-256",
                          await file.arrayBuffer(),
                        );
                        const sha256 = Array.from(
                          new Uint8Array(digest),
                          (byte) => byte.toString(16).padStart(2, "0"),
                        ).join("");
                        identities.push({
                          name: file.name,
                          size: file.size,
                          sha256,
                        });
                      }
                      return identities;
                    }
                    """
                )
            except Exception as exc:
                raise RuntimeError(
                    f"attached image identity could not be computed: {exc}"
                ) from exc
            return _parse_attached_image_identities(raw_identities)
    return ()


def _parse_attached_image_identities(
    raw_identities: object,
) -> tuple[_AttachedImageIdentity, ...]:
    if not isinstance(raw_identities, list):
        raise TypeError("attached image identity response was not a list")

    identities: list[_AttachedImageIdentity] = []
    for raw_identity in raw_identities:
        if not isinstance(raw_identity, dict):
            raise TypeError("attached image identity was not an object")
        name = raw_identity.get("name")
        size = raw_identity.get("size")
        sha256 = raw_identity.get("sha256")
        if not isinstance(name, str) or not name:
            raise RuntimeError("attached image identity is missing a name")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RuntimeError("attached image identity has an invalid size")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise RuntimeError("attached image identity has an invalid SHA-256")
        identities.append(
            _AttachedImageIdentity(
                name=name,
                size=size,
                sha256=sha256,
            )
        )
    return tuple(identities)


async def _read_visibility_label(page: Page) -> str | None:
    for selector in VISIBILITY_SELECTORS:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if not await candidate.is_visible():
                continue
            label = await _visibility_label_for(candidate)
            if label is not None:
                return label

    for label in _KNOWN_VISIBILITY_LABELS:
        locator = page.get_by_text(label, exact=True)
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return label
    return None


async def _ensure_visibility(page: Page, visibility: Visibility) -> None:
    if visibility is Visibility.PUBLIC:
        current = await _read_visibility_label(page)
        if current not in _PUBLIC_VISIBILITY_LABELS:
            current_label = current or "unavailable"
            raise RuntimeError(
                "public visibility could not be verified after preparation; "
                f"current label is {current_label}"
            )
        return

    if visibility is Visibility.PRIVATE:
        current = await _read_visibility_label(page)
        if current == _PRIVATE_VISIBILITY_LABEL:
            return

        trigger = await _find_visibility_trigger(page)
        await trigger.click()
        option = await _first_visible_exact_text(
            page,
            _PRIVATE_VISIBILITY_LABEL,
        )
        await option.click()
        await _wait_for_locator_visibility(
            trigger,
            visibility,
        )
        return

    raise RuntimeError("followers visibility is not supported yet")


async def _find_visibility_trigger(page: Page) -> Locator:
    for selector in VISIBILITY_SELECTORS:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if not await candidate.is_visible():
                continue
            if await _visibility_label_for(candidate) is not None:
                return candidate

    for label in _KNOWN_VISIBILITY_LABELS:
        locator = page.get_by_text(label, exact=True)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    raise RuntimeError("Weibo visibility control could not be found")


async def _first_visible_exact_text(page: Page, text: str) -> Locator:
    locator = page.get_by_text(text, exact=True)
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    raise RuntimeError(f"Weibo visibility option {text} could not be found")


async def _visibility_label_for(candidate: Locator) -> str | None:
    values = [
        await candidate.inner_text(),
        await candidate.get_attribute("aria-label") or "",
        await candidate.get_attribute("data-visibility") or "",
    ]
    for value in values:
        label = normalize_text(value)
        if label in _KNOWN_VISIBILITY_LABELS:
            return label
    return None


async def _wait_for_locator_visibility(
    locator: Locator,
    visibility: Visibility,
) -> None:
    expected_labels = _visibility_labels_for(visibility)
    expected_label = (
        _PRIVATE_VISIBILITY_LABEL
        if visibility is Visibility.PRIVATE
        else "公开"
    )
    deadline = time.monotonic() + _VISIBILITY_TIMEOUT_SECONDS
    current: str | None = None
    while True:
        current = await _visibility_label_for(locator)
        if current in expected_labels:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f"Weibo visibility {expected_label} could not be verified; "
                f"current label is {current or 'unavailable'}"
            )
        await asyncio.sleep(min(_VISIBILITY_POLL_INTERVAL_SECONDS, remaining))


def _visibility_labels_for(visibility: Visibility) -> frozenset[str]:
    if visibility is Visibility.PUBLIC:
        return _PUBLIC_VISIBILITY_LABELS
    if visibility is Visibility.PRIVATE:
        return frozenset({_PRIVATE_VISIBILITY_LABEL})
    raise RuntimeError("followers visibility is not supported yet")


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
