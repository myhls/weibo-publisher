from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

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

_VISIBILITY_SNAPSHOT_SCRIPT = """
(root, { knownLabels, selectors, triggerAction, ownerToken }) => {
  const empty = () => ({
    label: null,
    labels: [],
    hasTrigger: false,
    clicked: false,
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

  const clicked = best !== null && triggerAction === true;
  if (clicked) {
    if (ownerToken) {
      best.element.setAttribute(
        "data-codex-visibility-owner",
        ownerToken,
      );
      const popupSelectors = [
        '[role="listbox"]',
        '[role="menu"]',
        '[role="dialog"]',
        '[id*="visibility-option"]',
        '[id*="visibility-menu"]',
        '[id*="visibility-popup"]',
        '[id*="visibility-dropdown"]',
        '[class*="visibility-option"]',
        '[class*="visibility-menu"]',
        '[class*="visibility-popup"]',
        '[class*="visibility-dropdown"]',
        '[class*="dropdown"]',
        '[class*="popup"]',
        '[class*="popover"]',
      ];
      for (const popup of document.querySelectorAll(
        popupSelectors.join(","),
      )) {
        if (popup instanceof HTMLElement && isVisible(popup)) {
          popup.setAttribute(
            "data-codex-visibility-baseline",
            ownerToken,
          );
        }
      }
    }
    best.element.click();
  }
  return {
    label: best?.label ?? labels[0] ?? null,
    labels,
    hasTrigger: best !== null,
    clicked,
  };
}
"""

_VISIBILITY_PORTAL_OPTION_SCRIPT = """
({ ownerToken, targetLabel }) => {
  const normalize = (value) =>
    String(value ?? "").replace(/\\s+/g, " ").trim();
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
  const owner = document.querySelector(
    `[data-codex-visibility-owner="${ownerToken}"]`,
  );
  if (!(owner instanceof HTMLElement) || !isVisible(owner)) {
    return {
      acted: false,
      label: null,
      detail: "current composer visibility trigger is unavailable",
    };
  }

  const popupSelectors = [
    '[role="listbox"]',
    '[role="menu"]',
    '[role="dialog"]',
    '[id*="visibility-option"]',
    '[id*="visibility-menu"]',
    '[id*="visibility-popup"]',
    '[id*="visibility-dropdown"]',
    '[class*="visibility-option"]',
    '[class*="visibility-menu"]',
    '[class*="visibility-popup"]',
    '[class*="visibility-dropdown"]',
    '[class*="dropdown"]',
    '[class*="popup"]',
    '[class*="popover"]',
  ];
  const controlledIds = [
    owner.getAttribute("aria-controls"),
    owner.getAttribute("aria-owns"),
  ].flatMap((value) => String(value ?? "").split(/\\s+/))
    .filter(Boolean);
  const controlledPopups = controlledIds
    .map((id) => document.getElementById(id))
    .filter(
      (popup) =>
        popup instanceof HTMLElement && isVisible(popup),
    );
  const newlyOpenedPopups = [];
  for (const popup of document.querySelectorAll(
    popupSelectors.join(","),
  )) {
    if (
      popup instanceof HTMLElement &&
      isVisible(popup) &&
      popup.getAttribute("data-codex-visibility-baseline") !== ownerToken
    ) {
      newlyOpenedPopups.push(popup);
    }
  }
  const popups = controlledPopups.length
    ? controlledPopups
    : newlyOpenedPopups;
  if (popups.length === 0) {
    return {
      acted: false,
      label: null,
      detail: "no newly opened visibility popup was found",
    };
  }

  const interactiveSelector =
    'button, a, [role="button"], [role="option"], ' +
    '[role="menuitem"], [role="menuitemradio"], [role="radio"], ' +
    'li, [data-visibility], [tabindex]';
  const candidates = new Set();
  for (const popup of popups) {
    for (const candidate of popup.querySelectorAll(interactiveSelector)) {
      candidates.add(candidate);
    }
  }

  const target = normalize(targetLabel);
  let best = null;
  let bestScore = 0;
  for (const candidate of candidates) {
    if (
      !(candidate instanceof HTMLElement) ||
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
    if (!values.includes(target)) {
      continue;
    }
    let score = 0;
    if (candidate.matches('button, a, [role="button"]')) {
      score += 30;
    }
    if (
      candidate.matches(
        '[role="option"], [role="menuitem"], ' +
        '[role="menuitemradio"], [role="radio"]',
      )
    ) {
      score += 50;
    }
    if (
      candidate.matches(
        '[data-visibility], [data-testid="visibility"]',
      )
    ) {
      score += 20;
    }
    if (candidate.matches("li, [tabindex]")) {
      score += 10;
    }
    if (score > bestScore) {
      best = candidate;
      bestScore = score;
    }
  }
  if (best === null) {
    return {
      acted: false,
      label: null,
      detail: "visibility popup did not contain an interactive target",
    };
  }
  best.click();
  return {
    acted: true,
    label: target,
    detail: "visibility option clicked in popup opened by current trigger",
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
(root, { targetLabel }) => {
  if (
    !(root instanceof HTMLElement) ||
    root === document.body ||
    root === document.documentElement
  ) {
    return { acted: false, label: null };
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

  const popupSelectors = [
    '[role="listbox"]',
    '[role="menu"]',
    '[role="dialog"]',
    '[id*="visibility-option"]',
    '[id*="visibility-menu"]',
    '[id*="visibility-popup"]',
    '[id*="visibility-dropdown"]',
    '[class*="visibility-option"]',
    '[class*="visibility-menu"]',
    '[class*="visibility-popup"]',
    '[class*="visibility-dropdown"]',
    '[class*="dropdown"]',
    '[class*="popup"]',
    '[class*="popover"]',
  ];
  const interactiveSelector =
    'button, a, [role="button"], [role="option"], [role="menuitem"], ' +
    '[role="menuitemradio"], [role="radio"], li, [data-visibility], ' +
    '[data-testid="visibility"], [tabindex]';
  const candidates = new Set();
  for (const popup of root.querySelectorAll(popupSelectors.join(","))) {
    if (
      popup === root ||
      !(popup instanceof HTMLElement) ||
      !isVisible(popup)
    ) {
      continue;
    }
    for (const candidate of popup.querySelectorAll(interactiveSelector)) {
      candidates.add(candidate);
    }
  }

  let best = null;
  let bestScore = -Infinity;
  for (const candidate of candidates) {
    if (
      !(candidate instanceof HTMLElement) ||
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
      if (candidate.matches('button, a, [role="button"]')) {
        score += 30;
      }
      if (
        candidate.matches(
          '[role="option"], [role="menuitem"], ' +
          '[role="menuitemradio"], [role="radio"]'
        )
      ) {
        score += 50;
      }
      if (candidate.matches('[data-visibility], [data-testid="visibility"]')) {
        score += 20;
      }
      if (candidate.matches('li, [tabindex]')) {
        score += 10;
      }
      if (score > 0 && score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
  }
  if (best === null) {
    return { acted: false, label: null };
  }
  best.click();
  return { acted: true, label: target };
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
    has_trigger: bool


@dataclass(frozen=True, slots=True)
class _VisibilityActionResult:
    label: str


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
        except BaseException as exc:
            if self.page is not None:
                await capture_failure_diagnostics(
                    self.page,
                    artifact_dir,
                    exc,
                )
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
            await capture_failure_diagnostics(page, artifact_dir, exc)
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
        try:
            await _prepare_page(
                page,
                job.text,
                images,
                job.visibility,
                artifact_dir,
            )
        except Exception as exc:
            await capture_failure_diagnostics(page, artifact_dir, exc)
            raise


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
        has_publish_control = await _has_nearby_publish_control(
            parent,
            composer,
        )
        has_visibility_control = await _has_visible_visibility_control(parent)
        if has_publish_control and has_visibility_control:
            return parent
        current = parent
    raise RuntimeError(
        "Active Weibo composer region could not be identified: "
        "no non-page ancestor contains a visible publish control near the "
        "current editor; visibility could not be verified"
    )


async def _has_nearby_publish_control(
    root: Locator,
    composer: Locator,
) -> bool:
    try:
        return bool(
            await composer.evaluate(
                """
                (composer) => {
                  const normalize = (value) =>
                    String(value ?? "").replace(/\\s+/g, " ").trim();
                  const isVisible = (element) => {
                    if (
                      !(element instanceof HTMLElement) ||
                      element.hidden ||
                      element.getClientRects().length === 0
                    ) {
                      return false;
                    }
                    const style = getComputedStyle(element);
                    return (
                      style.display !== "none" &&
                      style.visibility !== "hidden" &&
                      Number(style.opacity) !== 0
                    );
                  };
                  const composerRect = composer.getBoundingClientRect();
                  const currentRoot = composer.closest("form") ??
                    composer.parentElement?.parentElement;
                  if (!currentRoot) {
                    return false;
                  }
                  for (const control of currentRoot.querySelectorAll(
                    'button, a, [role="button"], [class*="submit"]',
                  )) {
                    if (
                      !isVisible(control) ||
                      !/发布|发送/.test(
                        normalize(control.innerText || control.textContent),
                      )
                    ) {
                      continue;
                    }
                    const rect = control.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const composerCenterX =
                      composerRect.left + composerRect.width / 2;
                    const composerCenterY =
                      composerRect.top + composerRect.height / 2;
                    if (
                      Math.abs(centerX - composerCenterX) <= 700 &&
                      Math.abs(centerY - composerCenterY) <= 300
                    ) {
                      return true;
                    }
                  }
                  return false;
                }
                """
            )
        )
    except Exception:  # noqa: BLE001 - a detached ancestor is not a match
        _ = root
        return False


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
                "triggerAction": False,
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
    raw_has_trigger = raw_snapshot.get("hasTrigger")
    raw_clicked = raw_snapshot.get("clicked")
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
    if not isinstance(raw_has_trigger, bool):
        raise RuntimeError(  # noqa: TRY004 - browser data must fail closed
            "Weibo visibility snapshot returned invalid trigger state"
        )
    if not isinstance(raw_clicked, bool):
        raise RuntimeError(  # noqa: TRY004 - browser data must fail closed
            "Weibo visibility snapshot returned invalid action state"
        )
    if raw_label is not None and raw_label not in raw_labels:
        raise RuntimeError(
            "Weibo visibility snapshot label was not present in its labels"
        )
    if raw_has_trigger and raw_label is None:
        raise RuntimeError(
            "Weibo visibility snapshot found a trigger without a label"
        )
    if raw_clicked:
        raise RuntimeError(
            "Weibo visibility read unexpectedly clicked a trigger"
        )
    return _VisibilitySnapshot(
        label=raw_label,
        labels=tuple(raw_labels),
        has_trigger=raw_has_trigger,
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
            and snapshot.has_trigger
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

    page = composer_region.page
    baseline_token = await _mark_visible_exact_text_nodes(page, target_label)
    await _click_current_visibility_trigger(composer_region, snapshot.label)
    try:
        await _wait_for_new_exact_text_action(
            page,
            target_label,
            baseline_token,
        )
        await _wait_for_visibility_snapshot(
            composer_region,
            expected_labels=expected_labels,
        )
    finally:
        with suppress(Exception):
            await page.evaluate(
                """
                (token) => {
                  document
                    .querySelectorAll(
                      `[data-codex-text-baseline="${token}"]`,
                    )
                    .forEach((element) => {
                      element.removeAttribute("data-codex-text-baseline");
                    });
                }
                """,
                baseline_token,
            )


async def _mark_visible_exact_text_nodes(page: Page, text: str) -> str:
    token = uuid4().hex
    await page.evaluate(
        """
        ({ token, text }) => {
          const normalize = (value) =>
            String(value ?? "").replace(/\\s+/g, " ").trim();
          const isVisible = (element) => {
            if (
              !(element instanceof HTMLElement) ||
              element.hidden ||
              element.getClientRects().length === 0
            ) {
              return false;
            }
            const style = getComputedStyle(element);
            return (
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              Number(style.opacity) !== 0
            );
          };
          for (const element of document.querySelectorAll(
            "button, a, [role=button], [role=option], " +
            "[role=menuitem], [role=menuitemradio], li, div, span",
          )) {
            if (
              isVisible(element) &&
              normalize(element.innerText || element.textContent) === text
            ) {
              element.setAttribute("data-codex-text-baseline", token);
            }
          }
        }
        """,
        {"token": token, "text": text},
    )
    return token


async def _click_current_visibility_trigger(
    composer_region: Locator,
    text: str,
) -> None:
    acted = await composer_region.evaluate(
        """
        (root, { text }) => {
          const normalize = (value) =>
            String(value ?? "").replace(/\\s+/g, " ").trim();
          const isVisible = (element) => {
            if (
              !(element instanceof HTMLElement) ||
              element.hidden ||
              element.getClientRects().length === 0
            ) {
              return false;
            }
            const style = getComputedStyle(element);
            return (
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              Number(style.opacity) !== 0
            );
          };
          const composer = [...root.querySelectorAll("textarea")]
            .find(isVisible);
          if (!composer) {
            return false;
          }
          const composerRect = composer.getBoundingClientRect();
          let send = null;
          let sendDistance = Infinity;
          for (const control of root.querySelectorAll(
            'button, a, [role="button"], [class*="submit"]',
          )) {
            if (
              !isVisible(control) ||
              !/发布|发送/.test(
                normalize(control.innerText || control.textContent),
              )
            ) {
              continue;
            }
            const rect = control.getBoundingClientRect();
            const distance = Math.hypot(
              rect.left - composerRect.right,
              rect.top - composerRect.bottom,
            );
            if (distance < sendDistance) {
              send = control;
              sendDistance = distance;
            }
          }
          if (!send || sendDistance > 800) {
            return false;
          }
          const sendRect = send.getBoundingClientRect();
          const sendX = sendRect.left + sendRect.width / 2;
          const sendY = sendRect.top + sendRect.height / 2;
          let best = null;
          let bestScore = Infinity;
          for (const candidate of root.querySelectorAll(
            "button, a, [role=button], div, span",
          )) {
            if (
              !isVisible(candidate) ||
              normalize(
                candidate.innerText || candidate.textContent,
              ) !== text
            ) {
              continue;
            }
            const rect = candidate.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            if (
              Math.abs(centerX - sendX) > 350 ||
              Math.abs(centerY - sendY) > 160
            ) {
              continue;
            }
            const score =
              Math.abs(centerX - sendX) +
              Math.abs(centerY - sendY) * 2;
            if (score < bestScore) {
              best = candidate;
              bestScore = score;
            }
          }
          if (!best) {
            return false;
          }
          best.click();
          return true;
        }
        """,
        {"text": text},
    )
    if acted is not True:
        raise RuntimeError(
            f"Weibo visibility trigger {text} near the current editor "
            "could not be clicked"
        )


async def _wait_for_new_exact_text_action(
    page: Page,
    text: str,
    baseline_token: str,
) -> None:
    deadline = time.monotonic() + _VISIBILITY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while True:
        try:
            acted = await page.evaluate(
                """
                ({ text, token }) => {
                  const normalize = (value) =>
                    String(value ?? "").replace(/\\s+/g, " ").trim();
                  const isVisible = (element) => {
                    if (
                      !(element instanceof HTMLElement) ||
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
                      Number(style.opacity) !== 0
                    );
                  };
                  let best = null;
                  let bestScore = 0;
                  for (const element of document.querySelectorAll(
                    "button, a, [role=button], [role=option], " +
                    "[role=menuitem], [role=menuitemradio], " +
                    "[role=radio], li, [tabindex]",
                  )) {
                    if (
                      !isVisible(element) ||
                      element.getAttribute(
                        "data-codex-text-baseline",
                      ) === token ||
                      normalize(
                        element.innerText || element.textContent,
                      ) !== text
                    ) {
                      continue;
                    }
                    let score = 0;
                    if (element.matches("button, a, [role=button]")) {
                      score += 20;
                    }
                    if (
                      element.matches(
                        "[role=option], [role=menuitem], " +
                        "[role=menuitemradio], [role=radio]",
                      )
                    ) {
                      score += 50;
                    }
                    if (element.matches("li, [tabindex]")) {
                      score += 10;
                    }
                    if (score > bestScore) {
                      best = element;
                      bestScore = score;
                    }
                  }
                  if (best === null) {
                    return false;
                  }
                  best.click();
                  return true;
                }
                """,
                {"text": text, "token": baseline_token},
            )
        except Exception as exc:  # noqa: BLE001 - retry until deadline
            last_error = exc
            acted = False
        if acted is True:
            return
        if acted is not False:
            raise RuntimeError(
                "Weibo visibility option action returned invalid data"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(
            min(_VISIBILITY_POLL_INTERVAL_SECONDS, remaining)
        )

    error = RuntimeError(
        f"Weibo visibility option {text} did not appear in the popup opened "
        f"by the current composer trigger within "
        f"{_VISIBILITY_TIMEOUT_SECONDS:.1f}s"
    )
    if last_error is not None:
        raise error from last_error
    raise error


async def _wait_for_visibility_action(
    context: Page | Locator,
    script: str,
    arguments: dict[str, object],
    description: str,
) -> _VisibilityActionResult:
    deadline = time.monotonic() + _VISIBILITY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    current_label: str | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw_result = await asyncio.wait_for(
                context.evaluate(script, arguments),
                timeout=max(0.001, remaining),
            )
        except Exception as exc:  # noqa: BLE001 - retry until readiness deadline
            last_error = exc
            raw_result = None
        if not isinstance(raw_result, dict):
            if raw_result is not None:
                raise RuntimeError(
                    f"Weibo {description} returned invalid action data"
                )
        else:
            acted = raw_result.get(
                "acted",
                raw_result.get("clicked", False),
            )
            label = raw_result.get("label")
            if not isinstance(acted, bool):
                raise RuntimeError(  # noqa: TRY004 - browser data must fail closed
                    f"Weibo {description} returned invalid action state"
                )
            if label is not None and (
                not isinstance(label, str)
                or label not in _KNOWN_VISIBILITY_LABELS
            ):
                raise RuntimeError(
                    f"Weibo {description} returned an invalid label"
                )
            if isinstance(label, str):
                current_label = label
            if acted:
                if label is None:
                    raise RuntimeError(
                        f"Weibo {description} acted without a current label"
                    )
                return _VisibilityActionResult(label=label)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_VISIBILITY_POLL_INTERVAL_SECONDS, remaining))

    error = RuntimeError(
        f"Weibo {description} could not be completed within "
        f"{_VISIBILITY_TIMEOUT_SECONDS:.1f}s; "
        f"current label is {current_label or 'unavailable'}"
    )
    if last_error is not None:
        raise error from last_error
    raise error


def _visibility_labels_for(visibility: Visibility) -> frozenset[str]:
    if visibility is Visibility.PUBLIC:
        return _PUBLIC_VISIBILITY_LABELS
    if visibility is Visibility.PRIVATE:
        return frozenset({_PRIVATE_VISIBILITY_LABEL})
    raise RuntimeError("followers visibility is not supported yet")


async def capture_failure_diagnostics(
    page: Page,
    artifact_dir: Path,
    error: BaseException,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    await _screenshot_safely(page, artifact_dir / "failure.png")
    try:
        raw = await page.evaluate(
            """
            ({
              textSelectors,
              visibilitySelectors,
              knownLabels,
              popupSelectors,
            }) => {
              const normalize = (value) =>
                String(value ?? "").replace(/\\s+/g, " ").trim();
              const isVisible = (element) => {
                if (!(element instanceof HTMLElement)) {
                  return false;
                }
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
              let composer = null;
              for (const selector of textSelectors) {
                composer = [...document.querySelectorAll(selector)]
                  .find(isVisible) ?? null;
                if (composer) {
                  break;
                }
              }

              let composerRoot = null;
              if (composer) {
                composerRoot = composer.parentElement;
                while (
                  composerRoot &&
                  composerRoot !== document.body &&
                  composerRoot !== document.documentElement
                ) {
                  const hasPublish = [...composerRoot.querySelectorAll(
                    'button, a, [role="button"]',
                  )].some(
                    (candidate) =>
                      isVisible(candidate) &&
                      /发布|发送/.test(normalize(candidate.innerText)),
                  );
                  const hasVisibility = [...composerRoot.querySelectorAll(
                    visibilitySelectors.join(","),
                  )].some(
                    (candidate) =>
                      isVisible(candidate) &&
                      knownLabels.includes(normalize(candidate.innerText)),
                  );
                  if (hasPublish && hasVisibility) {
                    break;
                  }
                  composerRoot = composerRoot.parentElement;
                }
                if (
                  !composerRoot ||
                  composerRoot === document.body ||
                  composerRoot === document.documentElement
                ) {
                  composerRoot = composer.parentElement;
                }
              }

              const popovers = [];
              for (const popup of document.querySelectorAll(
                popupSelectors.join(","),
              )) {
                if (!isVisible(popup)) {
                  continue;
                }
                popovers.push({
                  tag: popup.tagName.toLowerCase(),
                  role: popup.getAttribute("role"),
                  id: popup.id || null,
                  className:
                    typeof popup.className === "string"
                      ? popup.className
                      : null,
                  text: normalize(popup.innerText).slice(0, 4000),
                  html: popup.outerHTML,
                });
              }
              const visibilityLabels = [];
              if (composerRoot) {
                for (const candidate of composerRoot.querySelectorAll(
                  visibilitySelectors.join(","),
                )) {
                  if (!isVisible(candidate)) {
                    continue;
                  }
                  const label = normalize(candidate.innerText);
                  if (knownLabels.includes(label) && !visibilityLabels.includes(label)) {
                    visibilityLabels.push(label);
                  }
                }
              }
              return {
                url: location.href,
                title: document.title,
                composer_found: composer !== null,
                composer_root_found: composerRoot !== null,
                composer_text: composer ? normalize(composer.value ?? composer.innerText) : null,
                composer_html: composerRoot ? composerRoot.outerHTML : null,
                visibility_labels: visibilityLabels,
                popovers,
                active_element_html:
                  document.activeElement instanceof HTMLElement
                    ? document.activeElement.outerHTML
                    : null,
              };
            }
            """,
            {
                "textSelectors": TEXT_SELECTORS,
                "visibilitySelectors": _VISIBILITY_CSS_SELECTORS,
                "knownLabels": sorted(_KNOWN_VISIBILITY_LABELS),
                "popupSelectors": [
                    '[role="listbox"]',
                    '[role="menu"]',
                    '[role="dialog"]',
                    '[id*="visibility-option"]',
                    '[id*="visibility-menu"]',
                    '[id*="visibility-popup"]',
                    '[id*="visibility-dropdown"]',
                    '[class*="visibility-option"]',
                    '[class*="visibility-menu"]',
                    '[class*="visibility-popup"]',
                    '[class*="visibility-dropdown"]',
                    '[class*="dropdown"]',
                    '[class*="popup"]',
                    '[class*="popover"]',
                ],
            },
        )
    except Exception as diagnostics_error:  # noqa: BLE001 - diagnostics are best effort
        raw = {"diagnostics_error": str(diagnostics_error)}

    if not isinstance(raw, dict):
        raw = {}
    composer_html = raw.pop("composer_html", None)
    popovers = raw.pop("popovers", [])
    diagnostics = {
        "captured_at": datetime.now(UTC).isoformat(),
        "error_type": type(error).__name__,
        "error": str(error),
        **raw,
        "popover_count": len(popovers) if isinstance(popovers, list) else 0,
    }
    (artifact_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (artifact_dir / "visibility-popovers.json").write_text(
        json.dumps(popovers, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (artifact_dir / "composer.html").write_text(
        composer_html if isinstance(composer_html, str) else "",
        encoding="utf-8",
    )


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
