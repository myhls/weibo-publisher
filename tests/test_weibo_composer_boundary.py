from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from weibo_publisher import weibo
from weibo_publisher.models import Visibility


@pytest.mark.asyncio
async def test_composer_region_uses_common_ancestor_for_visibility_selection() -> None:
    async with _local_page(_page_html(has_common_ancestor=True)) as page:
        composer = await weibo.first_visible(page, weibo.TEXT_SELECTORS)
        region = await weibo._active_composer_region(composer)

        assert await region.get_attribute("id") == "composer-shell"
        assert await region.locator("#body").count() == 1
        assert await region.locator("#publish").count() == 1
        assert await region.locator("#visibility").count() == 1

        await weibo._ensure_visibility(region, Visibility.PRIVATE)
        assert await page.locator("#visibility").inner_text() == "仅自己可见"

        await weibo._ensure_visibility(region, Visibility.PUBLIC)
        assert await page.locator("#visibility").inner_text() == "公开"
        assert await page.evaluate("window.publishClickCount ?? 0") == 0


@pytest.mark.asyncio
async def test_prepare_page_uses_three_branch_common_ancestor(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    async with _local_page(_page_html(has_common_ancestor=True)) as page:
        await weibo._prepare_page(
            page,
            "three branch composer",
            [],
            Visibility.PRIVATE,
            artifact_dir,
        )

        assert await page.locator("#body").input_value() == "three branch composer"
        assert await page.locator("#visibility").inner_text() == "仅自己可见"
        assert await page.evaluate("window.publishClickCount ?? 0") == 0
        assert await page.evaluate("window.editorClickCount ?? 0") == 0
        assert (artifact_dir / "preview.png").is_file()


@pytest.mark.asyncio
async def test_prepare_fails_before_click_without_non_page_common_ancestor(
    tmp_path: Path,
) -> None:
    async with _local_page(_page_html(has_common_ancestor=False)) as page:
        with pytest.raises(
            RuntimeError,
            match="contains both a visible publish control and a visible visibility control",
        ):
            await weibo._prepare_page(
                page,
                "must not publish",
                [],
                Visibility.PRIVATE,
                tmp_path / "artifacts",
            )

        assert await page.evaluate("window.publishClickCount ?? 0") == 0
        assert await page.evaluate("window.editorClickCount ?? 0") == 0


@pytest.mark.asyncio
async def test_prepare_survives_repeated_visibility_control_rebuilds(
    tmp_path: Path,
) -> None:
    async with _local_page(_dynamic_visibility_page_html()) as page:
        region = page.locator("#composer-shell")
        await asyncio.sleep(0.35)

        snapshot = await weibo._read_visibility_snapshot(region)

        assert snapshot.label == "公开"
        assert snapshot.trigger_marked is True
        assert await page.evaluate("window.visibilityRebuildCount") >= 2

        artifact_dir = tmp_path / "artifacts"
        artifact_dir.mkdir()
        await weibo._prepare_page(
            page,
            "dynamic visibility",
            [],
            Visibility.PRIVATE,
            artifact_dir,
        )

        assert await page.locator("#body").input_value() == "dynamic visibility"
        assert await page.locator("#current-visibility").inner_text() == "仅自己可见"
        assert await page.evaluate("window.publishClickCount ?? 0") == 0
        assert await page.evaluate("window.editorClickCount ?? 0") == 0
        assert (artifact_dir / "preview.png").is_file()


@pytest.mark.asyncio
async def test_visibility_selection_fails_closed_before_control_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(weibo, "_VISIBILITY_TIMEOUT_SECONDS", 0.3)
    async with _local_page(_stalled_visibility_page_html()) as page:
        region = page.locator("#composer-shell")
        started = time.monotonic()

        with pytest.raises(RuntimeError, match="visibility"):
            await weibo._ensure_visibility(region, Visibility.PRIVATE)
        elapsed = time.monotonic() - started

        assert elapsed < 2.0
        assert await page.evaluate("window.publishClickCount ?? 0") == 0
        assert await page.evaluate("window.editorClickCount ?? 0") == 0


@asynccontextmanager
async def _local_page(
    html: str,
    *,
    default_timeout_ms: int = 1_200,
) -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        context.set_default_timeout(default_timeout_ms)

        async def fulfill_local(route) -> None:
            if route.request.url == weibo.HOME_URL:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
            else:
                await route.abort()

        await context.route("https://**", fulfill_local)
        page = await context.new_page()
        try:
            await page.goto(weibo.HOME_URL)
            yield page
        finally:
            await browser.close()


def _page_html(*, has_common_ancestor: bool) -> str:
    if has_common_ancestor:
        composer_markup = """
          <main id="composer-shell">
            <section id="composer-branch">
              <textarea id="body" placeholder="分享新鲜事"></textarea>
            </section>
            <section id="send-branch">
              <button id="publish" type="button">发送</button>
            </section>
            <section id="visibility-branch">
              <button id="visibility" type="button" aria-label="公开">公开</button>
              <div id="visibility-options" hidden>
                <button type="button">公开</button>
                <button type="button">粉丝</button>
                <button type="button">好友圈</button>
                <button type="button">仅自己可见</button>
                <button type="button">群可见</button>
              </div>
            </section>
          </main>
        """
    else:
        composer_markup = """
          <section id="composer-branch">
            <div id="body" class="editor" contenteditable="true"></div>
          </section>
          <section id="send-branch">
            <button id="publish" type="button">发送</button>
          </section>
          <section id="visibility-branch">
            <button id="visibility" type="button" aria-label="公开">公开</button>
            <div id="visibility-options" hidden>
              <button type="button">公开</button>
              <button type="button">仅自己可见</button>
            </div>
          </section>
        """

    return f"""
      <!doctype html>
      <html>
        <body>
          <header><a class="gn_name" href="/u/123">Account</a></header>
          {composer_markup}
          <script>
            const visibility = document.querySelector("#visibility");
            const visibilityOptions =
              document.querySelector("#visibility-options");
            visibility.addEventListener("click", () => {{
              visibilityOptions.hidden = false;
            }});
            for (const option of visibilityOptions.querySelectorAll("button")) {{
              option.addEventListener("click", () => {{
                visibility.textContent = option.textContent;
                visibility.setAttribute("aria-label", option.textContent);
                visibilityOptions.hidden = true;
              }});
            }}
            document.querySelector("#publish").addEventListener("click", () => {{
              window.publishClickCount =
                (window.publishClickCount ?? 0) + 1;
            }});
            document.querySelector("#body").addEventListener("click", () => {{
              window.editorClickCount =
                (window.editorClickCount ?? 0) + 1;
            }});
          </script>
        </body>
      </html>
    """


def _dynamic_visibility_page_html() -> str:
    return """
      <!doctype html>
      <html>
        <body>
          <header><a class="gn_name" href="/u/123">Account</a></header>
          <main id="composer-shell">
            <textarea id="body" placeholder="分享新鲜事"></textarea>
            <section id="visibility-slot"></section>
            <div id="visibility-options" hidden>
              <button type="button">公开</button>
              <button type="button">仅自己可见</button>
            </div>
            <button id="publish" type="button">发送</button>
          </main>
          <script>
            let currentLabel = "公开";
            let generation = 0;
            const slot = document.querySelector("#visibility-slot");
            const options = document.querySelector("#visibility-options");
            let rebuildTimer;

            const renderTrigger = () => {
              generation += 1;
              window.visibilityRebuildCount =
                (window.visibilityRebuildCount ?? 0) + 1;
              const trigger = document.createElement("button");
              trigger.type = "button";
              trigger.id = "current-visibility";
              trigger.className = "audience-selector";
              trigger.dataset.audienceControl = String(generation);
              trigger.textContent = currentLabel;
              trigger.setAttribute("aria-label", currentLabel);
              trigger.setAttribute("aria-haspopup", "listbox");
              trigger.addEventListener("click", () => {
                clearInterval(rebuildTimer);
                options.hidden = false;
              });
              slot.replaceChildren(trigger);
            };

            for (const option of options.querySelectorAll("button")) {
              option.addEventListener("click", () => {
                currentLabel = option.textContent;
                options.hidden = true;
                renderTrigger();
              });
            }

            renderTrigger();
            rebuildTimer = setInterval(renderTrigger, 150);
          </script>
          <script>
            document.querySelector("#publish").addEventListener("click", () => {
              window.publishClickCount =
                (window.publishClickCount ?? 0) + 1;
            });
            document.querySelector("#body").addEventListener("click", () => {
              window.editorClickCount =
                (window.editorClickCount ?? 0) + 1;
            });
          </script>
        </body>
      </html>
    """


def _stalled_visibility_page_html() -> str:
    return """
      <!doctype html>
      <html>
        <body>
          <header><a class="gn_name" href="/u/123">Account</a></header>
          <main id="composer-shell">
            <textarea id="body" placeholder="分享新鲜事"></textarea>
            <section id="visibility-slot"></section>
            <button id="publish" type="button">发送</button>
          </main>
          <script>
            document.querySelector("#publish").addEventListener("click", () => {
              window.publishClickCount =
                (window.publishClickCount ?? 0) + 1;
            });
            document.querySelector("#body").addEventListener("click", () => {
              window.editorClickCount =
                (window.editorClickCount ?? 0) + 1;
            });
          </script>
        </body>
      </html>
    """
