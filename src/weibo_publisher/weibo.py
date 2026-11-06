from __future__ import annotations

import asyncio
import re
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
    'button:has-text("发送")',
    'a:has-text("发布")',
    'a:has-text("发送")',
    '[class*="submit"]:has-text("发布")',
    '[class*="submit"]:has-text("发送")',
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
_VISIBILITY_CSS_SELECTORS = [
    selector
    for selector in VISIBILITY_SELECTORS
    if ":has-text(" not in selector
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
_VISIBILITY_TIMEOUT_SECONDS = 5.0
_VISIBILITY_POLL_INTERVAL_SECONDS = 0.05
_VISIBILITY_CLICK_TIMEOUT_MS = 1_000
_VISIBILITY_TRIGGER_MARKER = "data-codex-visibility-trigger"
_VISIBILITY_OPTION_MARKER = "data-codex-visibility-option"

_VISIBILITY_SNAPSHOT_SCRIPT = """
(root, { knownLabels, selectors, triggerMarker, optionMarker }) => {
  const empty = () => ({
    label: null,
    labels: [],
    triggerMarked: false,
  });
  if (
    !(root instanceof HTMLElement) ||
    root === document.body ||
    root === document.documentElement
  ) {
    return empty();
  }

  const normalize = (value) =>
    String(value ?? "").replace(/\\s+/g, " ").trim();
  const known = new Set(knownLabels);
  const isVisible = (element) => {
    if (
      element.hidden ||
      element.getAttribute("aria-hidden") === "true" ||
      element.getClientRects().length === 0
    ) {
      return false;
    }
    const style = getComputedStyle(element);
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      style.visibility !== "collapse" &&
      Number(style.opacity) !== 0
    );
  };
  const isDisabled = (element) =>
    element.matches(":disabled") ||
    element.getAttribute("aria-disabled") === "true";
  const exactLabel = (element) => {
    const values = [
      element.innerText ?? "",
      element.textContent ?? "",
      element.getAttribute("aria-label") ?? "",
      element.getAttribute("data-visibility") ?? "",
    ];
    for (const value of values) {
      const label = normalize(value);
      if (known.has(label)) {
        return label;
      }
    }
    return null;
  };

  for (const marked of root.querySelectorAll(
    `[${triggerMarker}], [${optionMarker}]`
  )) {
    marked.removeAttribute(triggerMarker);
    marked.removeAttribute(optionMarker);
  }

  const selectorSet = new Set([
    ...selectors,
    "button",
    "a",
    '[role="button"]',
    "[aria-haspopup]",
  ]);
  const candidates = root.querySelectorAll(Array.from(selectorSet).join(","));
  const labels = [];
  let best = null;
  let bestScore = -Infinity;

  for (const candidate of candidates) {
    if (
      !(candidate instanceof HTMLElement) ||
      !isVisible(candidate) ||
      isDisabled(candidate)
    ) {
      continue;
    }
    const label = exactLabel(candidate);
    if (label === null) {
      continue;
    }
    if (!labels.includes(label)) {
      labels.push(label);
    }

    const semanticTrigger = candidate.matches(
      'button, a, [role="button"], [data-visibility], ' +
      '[data-testid="visibility"], [id*="visibility"], ' +
      '[class*="visibility"]'
    );
    const selectedTrigger = candidate.matches(selectors.join(","));
    if (!semanticTrigger && !selectedTrigger) {
      continue;
    }

    let score = 0;
    if (candidate.matches('button, a, [role="button"]')) {
      score += 20;
    }
    if (candidate.matches('[data-visibility], [data-testid="visibility"]')) {
      score += 50;
    }
    if (candidate.matches('[id*="visibility"], [class*="visibility"]')) {
      score += 20;
    }
    if (normalize(candidate.getAttribute("aria-label") ?? "") === label) {
      score += 15;
    }
    if (candidate.hasAttribute("aria-haspopup")) {
      score += 25;
    }
    if (normalize(candidate.innerText || candidate.textContent || "") === label) {
      score += 10;
    }
    if (score > bestScore) {
      best = { element: candidate, label };
      bestScore = score;
    }
  }

  if (best !== null) {
    best.element.setAttribute(triggerMarker, "true");
  }
  return {
    label: best?.label ?? labels[0] ?? null,
    labels,
    triggerMarked: best !== null,
  };
}
"""

_VISIBILITY_DISCOVERY_SCRIPT = """
(root, { knownLabels, selectors }) => {
  if (
    !(root instanceof HTMLElement) ||
    root === document.body ||
    root === document.documentElement
  ) {
    return false;
  }
  const normalize = (value) =>
    String(value ?? "").replace(/\\s+/g, " ").trim();
  const known = new Set(knownLabels);
  const isVisible = (element) => {
    if (
      element.hidden ||
      element.getAttribute("aria-hidden") === "true" ||
      element.getClientRects().length === 0
    ) {
      return false;
    }
    const style = getComputedStyle(element);
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      style.visibility !== "collapse" &&
      Number(style.opacity) !== 0
    );
  };
  const selectorSet = new Set([
    ...selectors,
    "button",
    "a",
    '[role="button"]',
    "[aria-haspopup]",
  ]);
  for (const candidate of root.querySelectorAll(
    Array.from(selectorSet).join(",")
  )) {
    if (
      !(candidate instanceof HTMLElement) ||
      !isVisible(candidate) ||
      candidate.matches(":disabled") ||
      candidate.getAttribute("aria-disabled") === "true"
    ) {
      continue;
    }
    const labels = [
      candidate.innerText ?? "",
      candidate.textContent ?? "",
      candidate.getAttribute("aria-label") ?? "",
      candidate.getAttribute("data-visibility") ?? "",
    ].map(normalize);
    if (labels.some((label) => known.has(label))) {
      return true;
    }
  }
  return false;
}
"""

_VISIBILITY_OPTION_SCRIPT = """
(root, { targetLabel, triggerMarker, optionMarker }) => {
  if (!(root instanceof HTMLElement)) {
    return false;
  }
  const normalize = (value) =>
    String(value ?? "").replace(/\\s+/g, " ").trim();
  const target = normalize(targetLabel);
  const isVisible = (element) => {
    if (
      element.hidden ||
      element.getAttribute("aria-hidden") === "true" ||
      element.getClientRects().length === 0
    ) {
      return false;
    }
    const style = getComputedStyle(element);
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      style.visibility !== "collapse" &&
      Number(style.opacity) !== 0
    );
  };

  for (const marked of root.querySelectorAll(`[${optionMarker}]`)) {
    marked.removeAttribute(optionMarker);
  }
  const candidates = root.querySelectorAll("*");
  let best = null;
  let bestScore = -Infinity;
  for (const candidate of candidates) {
    if (
      !(candidate instanceof HTMLElement) ||
      candidate.matches(`[${triggerMarker}]`) ||
      !isVisible(candidate) ||
      candidate.matches(":disabled") ||
      candidate.getAttribute("aria-disabled") === "true"
    ) {
      continue;
    }
    const values = [
      candidate.innerText ?? "",
      candidate.textContent ?? "",
      candidate.getAttribute("aria-label") ?? "",
      candidate.getAttribute("data-visibility") ?? "",
    ].map(normalize);
    if (values.includes(target)) {
      let score = 0;
      if (candidate.matches('button, a, [role="button"], [role="menuitem"]')) {
        score += 30;
      }
      if (candidate.matches('[role="option"], li')) {
        score += 20;
      }
      if (candidate.matches('[data-visibility], [data-testid="visibility"]')) {
        score += 20;
      }
      if (candidate.matches('[class*="visibility"]')) {
        score += 10;
      }
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
  }
  if (best === null) {
    return false;
  }
  best.setAttribute(optionMarker, "true");
  return true;
}
"""


@dataclass(frozen=True, slots=True)
class _AttachedImageIdentity:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _VisibilitySnapshot:
    label: str | None
    labels: tuple[str, ...]
    trigger_marked: bool


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
    composer_region: Locator | None = None
    if tag_name == "textarea":
        await composer.fill(text)
    elif contenteditable == "true":
        composer_region = await _active_composer_region(composer)
        await composer.click()
        await page.keyboard.insert_text(text)
    else:
        raise RuntimeError("Matched Weibo control is not an editable composer")

    if images:
        image_input = await _first_present(page, IMAGE_INPUT_SELECTORS)
        await image_input.set_input_files([str(image) for image in images])
        await _wait_for_uploads(page)
        await _wait_for_uploaded_image_previews(page, len(images))

    if composer_region is None:
        composer_region = await _active_composer_region(composer)
    await _ensure_visibility(composer_region, visibility)

    await page.screenshot(
        path=str(artifact_dir / "preview.png"),
        full_page=True,
    )


async def first_visible(page: Page | Locator, selectors: list[str]) -> Locator:
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    raise RuntimeError("No matching Weibo control found")


async def _active_composer_region(composer: Locator) -> Locator:
    current = composer
    for _ in range(20):
        parent = current.locator("xpath=..")
        if await parent.count() == 0:
            break
        parent = parent.first
        tag_name = await parent.evaluate(
            "(element) => element.tagName.toLowerCase()"
        )
        if tag_name in {"body", "html"}:
            break
        has_publish_control = await _has_visible_control(
            parent,
            PUBLISH_SELECTORS,
        )
        has_visibility_control = await _has_visible_visibility_control(parent)
        if has_publish_control and has_visibility_control:
            return parent
        current = parent
    raise RuntimeError(
        "Active Weibo composer region could not be identified: "
        "no non-page ancestor contains both a visible publish control and "
        "a visible visibility control; visibility could not be verified"
    )


async def _has_visible_control(
    root: Page | Locator,
    selectors: list[str],
) -> bool:
    for selector in selectors:
        locator = root.locator(selector)
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return True
    return False


async def _has_visible_visibility_control(root: Locator) -> bool:
    try:
        return bool(
            await root.evaluate(
                _VISIBILITY_DISCOVERY_SCRIPT,
                {
                    "knownLabels": sorted(_KNOWN_VISIBILITY_LABELS),
                    "selectors": _VISIBILITY_CSS_SELECTORS,
                },
            )
        )
    except Exception:  # noqa: BLE001 - a detached ancestor is not a match
        return False


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
        if re.fullmatch(r"[1-9][0-9]*", mid):
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

    composer_region = await _active_composer_region(composer)
    snapshot = await _wait_for_visibility_snapshot(composer_region)
    visibility_label = snapshot.label
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


async def _read_visibility_snapshot(
    composer_region: Locator,
    *,
    timeout_ms: int | None = None,
) -> _VisibilitySnapshot:
    try:
        raw_snapshot = await composer_region.evaluate(
            _VISIBILITY_SNAPSHOT_SCRIPT,
            {
                "knownLabels": sorted(_KNOWN_VISIBILITY_LABELS),
                "selectors": _VISIBILITY_CSS_SELECTORS,
                "triggerMarker": _VISIBILITY_TRIGGER_MARKER,
                "optionMarker": _VISIBILITY_OPTION_MARKER,
            },
            timeout=timeout_ms,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Weibo visibility snapshot could not be read safely: {exc}"
        ) from exc

    if not isinstance(raw_snapshot, dict):
        raise RuntimeError(  # noqa: TRY004 - browser data must fail closed
            "Weibo visibility snapshot returned invalid data"
        )
    raw_label = raw_snapshot.get("label")
    raw_labels = raw_snapshot.get("labels")
    raw_trigger_marked = raw_snapshot.get("triggerMarked")
    if raw_label is not None and (
        not isinstance(raw_label, str)
        or raw_label not in _KNOWN_VISIBILITY_LABELS
    ):
        raise RuntimeError("Weibo visibility snapshot returned an invalid label")
    if not isinstance(raw_labels, list) or any(
        not isinstance(label, str) or label not in _KNOWN_VISIBILITY_LABELS
        for label in raw_labels
    ):
        raise RuntimeError("Weibo visibility snapshot returned invalid labels")
    if not isinstance(raw_trigger_marked, bool):
        raise RuntimeError(  # noqa: TRY004 - browser data must fail closed
            "Weibo visibility snapshot returned invalid trigger state"
        )
    if raw_label is not None and raw_label not in raw_labels:
        raise RuntimeError(
            "Weibo visibility snapshot label was not present in its labels"
        )
    if raw_trigger_marked and raw_label is None:
        raise RuntimeError(
            "Weibo visibility snapshot marked a trigger without a label"
        )
    return _VisibilitySnapshot(
        label=raw_label,
        labels=tuple(raw_labels),
        trigger_marked=raw_trigger_marked,
    )


async def _wait_for_visibility_snapshot(
    composer_region: Locator,
    *,
    expected_labels: frozenset[str] | None = None,
    timeout_seconds: float | None = None,
) -> _VisibilitySnapshot:
    expected = expected_labels or _KNOWN_VISIBILITY_LABELS
    wait_seconds = (
        _VISIBILITY_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(timeout_seconds, 0)
    )
    deadline = time.monotonic() + wait_seconds
    current: str | None = None
    last_error: Exception | None = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            snapshot = await _read_visibility_snapshot(
                composer_region,
                timeout_ms=max(1, int(remaining * 1_000)),
            )
        except RuntimeError as exc:
            last_error = exc
            snapshot = None
        if (
            snapshot is not None
            and snapshot.label in expected
            and snapshot.trigger_marked
        ):
            return snapshot
        if snapshot is not None:
            current = snapshot.label

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(
            min(_VISIBILITY_POLL_INTERVAL_SECONDS, remaining)
        )

    expected_label = (
        " / ".join(sorted(expected))
        if expected != _KNOWN_VISIBILITY_LABELS
        else None
    )
    detail = (
        f"expected label {expected_label} was not selected; "
        f"current label is {current or 'unavailable'}"
        if expected_label is not None
        else (
            "no current trigger and label appeared before the readiness "
            f"timeout; current label is {current or 'unavailable'}"
        )
    )
    error = RuntimeError(
        f"Weibo visibility could not be verified: {detail} "
        f"within {wait_seconds:.1f}s"
    )
    if last_error is not None:
        raise error from last_error
    raise error


async def _ensure_visibility(
    composer_region: Locator,
    visibility: Visibility,
) -> None:
    expected_labels = _visibility_labels_for(visibility)
    target_label = (
        _PRIVATE_VISIBILITY_LABEL
        if visibility is Visibility.PRIVATE
        else "公开"
    )
    snapshot = await _wait_for_visibility_snapshot(composer_region)
    if snapshot.label in expected_labels:
        return

    if snapshot.label not in _KNOWN_VISIBILITY_LABELS:
        current_label = snapshot.label or "unavailable"
        raise RuntimeError(
            f"Weibo visibility {target_label} could not be selected; "
            f"current composer label is {current_label}"
        )

    await _click_marked_visibility_element(
        composer_region,
        _VISIBILITY_TRIGGER_MARKER,
        "visibility trigger",
    )
    await _wait_for_visibility_option(composer_region, target_label)
    await _click_marked_visibility_element(
        composer_region,
        _VISIBILITY_OPTION_MARKER,
        f"visibility option {target_label}",
    )
    await _wait_for_visibility_snapshot(
        composer_region,
        expected_labels=expected_labels,
    )


async def _wait_for_visibility_option(
    composer_region: Locator,
    target_label: str,
) -> None:
    deadline = time.monotonic() + _VISIBILITY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            marked = await composer_region.evaluate(
                _VISIBILITY_OPTION_SCRIPT,
                {
                    "targetLabel": target_label,
                    "triggerMarker": _VISIBILITY_TRIGGER_MARKER,
                    "optionMarker": _VISIBILITY_OPTION_MARKER,
                },
                timeout=max(1, int(remaining * 1_000)),
            )
        except Exception as exc:  # noqa: BLE001 - retry until readiness deadline
            last_error = exc
            marked = False
        if marked is True:
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_VISIBILITY_POLL_INTERVAL_SECONDS, remaining))

    error = RuntimeError(
        f"Weibo visibility option {target_label} could not be selected "
        f"within {_VISIBILITY_TIMEOUT_SECONDS:.1f}s"
    )
    if last_error is not None:
        raise error from last_error
    raise error


async def _click_marked_visibility_element(
    composer_region: Locator,
    marker: str,
    description: str,
) -> None:
    marked = composer_region.locator(f'[{marker}="true"]')
    try:
        clicked = await marked.evaluate(
            """
            (element) => {
              element.click();
              return true;
            }
            """,
            timeout=_VISIBILITY_CLICK_TIMEOUT_MS,
        )
        if clicked is not True:
            raise RuntimeError("marked element did not accept the click")
    except Exception as exc:
        raise RuntimeError(
            f"Weibo {description} could not be clicked safely: {exc}"
        ) from exc


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
