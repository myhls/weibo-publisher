from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from weibo_publisher import weibo
from weibo_publisher.models import Visibility


@pytest.mark.asyncio
async def test_private_option_uses_newly_opened_global_portal() -> None:
    async with _local_page(_portal_html()) as page:
        region = page.locator("#composer-shell")

        await weibo._ensure_visibility(region, Visibility.PRIVATE)

        assert await page.locator("#visibility").inner_text() == "仅自己可见"
        assert await page.evaluate("window.portalOptionClicks") == 1
        assert await page.evaluate("window.decoyOptionClicks ?? 0") == 0
        assert await page.evaluate("window.navigationClickCount ?? 0") == 0
        assert await page.evaluate("window.publishClickCount ?? 0") == 0


@pytest.mark.asyncio
async def test_failure_diagnostics_save_composer_and_popover_structure(
    tmp_path: Path,
) -> None:
    async with _local_page(_portal_html(open_portal=True)) as page:
        await weibo.capture_failure_diagnostics(
            page,
            tmp_path,
            RuntimeError("visibility option could not be selected"),
        )

    diagnostics_path = tmp_path / "diagnostics.json"
    composer_path = tmp_path / "composer.html"
    popovers_path = tmp_path / "visibility-popovers.json"
    screenshot_path = tmp_path / "failure.png"

    assert diagnostics_path.is_file()
    assert composer_path.is_file()
    assert popovers_path.is_file()
    assert screenshot_path.is_file()

    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    popovers = json.loads(popovers_path.read_text(encoding="utf-8"))
    assert diagnostics["error"] == "visibility option could not be selected"
    assert diagnostics["composer_found"] is True
    assert diagnostics["popover_count"] >= 1
    assert popovers
    assert "<textarea" in composer_path.read_text(encoding="utf-8")
    assert "仅自己可见" in popovers_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_visibility_trigger_uses_interactive_wrapper_not_inner_text() -> None:
    async with _local_page(_wrapped_trigger_html()) as page:
        region = page.locator("#composer-shell")

        snapshot = await weibo._read_visibility_snapshot(region)
        token = await weibo._mark_visible_exact_text_nodes(page, "仅自己可见")
        await weibo._click_current_visibility_trigger(region, snapshot.label)

        assert await page.evaluate("window.wrapperClickCount") == 1
        await weibo._wait_for_new_exact_text_action(
            page,
            "仅自己可见",
            token,
        )
        assert await page.locator("#current-visibility").inner_text() == "仅自己可见"


@asynccontextmanager
async def _local_page(html: str) -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()

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


def _portal_html(*, open_portal: bool = False) -> str:
    hidden = "" if open_portal else "hidden"
    return f"""
      <!doctype html>
      <html>
        <style>
          #composer-shell {{ position: relative; width: 600px; height: 160px; }}
          #body {{ width: 500px; height: 50px; }}
          #limits {{ position: absolute; left: 380px; top: 80px; }}
          #publish {{ position: absolute; left: 450px; top: 80px; }}
        </style>
        <body>
          <header><a class="gn_name" href="/u/123">Account</a></header>
          <main id="composer-shell">
            <a id="navigation-public" href="/mygroups?gid=1">公开</a>
            <textarea id="body" placeholder="分享新鲜事"></textarea>
            <button
              id="visibility"
              type="button"
              aria-label="公开"
              aria-haspopup="menu"
              aria-expanded="false"
            >公开</button>
            <button id="publish" type="button">发送</button>
          </main>
          <div id="visibility-portal" role="menu" {hidden}>
            <button type="button" role="menuitemradio">公开</button>
            <button type="button" role="menuitemradio">仅自己可见</button>
          </div>
          <div id="pre-existing-decoy" role="dialog">
            <button type="button" role="menuitemradio">仅自己可见</button>
          </div>
          <script>
            const trigger = document.querySelector("#visibility");
            const portal = document.querySelector("#visibility-portal");
            document.querySelector("#navigation-public").addEventListener(
              "click",
              (event) => {{
                event.preventDefault();
                window.navigationClickCount =
                  (window.navigationClickCount ?? 0) + 1;
              }}
            );
            trigger.addEventListener("click", () => {{
              trigger.setAttribute("aria-expanded", "true");
              portal.hidden = false;
            }});
            for (const option of portal.querySelectorAll("button")) {{
              option.addEventListener("click", () => {{
                window.portalOptionClicks =
                  (window.portalOptionClicks ?? 0) + 1;
                const label = option.textContent;
                trigger.textContent = label;
                trigger.setAttribute("aria-label", label);
                trigger.setAttribute("aria-expanded", "false");
                portal.hidden = true;
              }});
            }}
            document.querySelector("#pre-existing-decoy button").addEventListener(
              "click",
              () => {{
                window.decoyOptionClicks =
                  (window.decoyOptionClicks ?? 0) + 1;
              }}
            );
            document.querySelector("#publish").addEventListener("click", () => {{
              window.publishClickCount =
                (window.publishClickCount ?? 0) + 1;
            }});
          </script>
        </body>
      </html>
    """


def _wrapped_trigger_html() -> str:
    return """
      <!doctype html>
      <html>
        <style>
          #composer-shell { position: relative; width: 600px; height: 160px; }
          #body { width: 500px; height: 50px; }
          #limits { position: absolute; left: 380px; top: 80px; }
          #publish { position: absolute; left: 450px; top: 80px; }
        </style>
        <body>
          <main id="composer-shell">
            <textarea id="body" placeholder="分享新鲜事"></textarea>
            <div id="limits" class="woo-pop-wrap">
              <span id="visibility" class="woo-pop-ctrl">
                <div id="current-visibility">公开</div>
              </span>
            </div>
            <button id="publish" type="button">发送</button>
          </main>
          <div id="visibility-menu" role="menu" hidden>
            <button type="button" role="menuitemradio">仅自己可见</button>
          </div>
          <script>
            const visibility = document.querySelector("#visibility");
            const current = document.querySelector("#current-visibility");
            const menu = document.querySelector("#visibility-menu");
            visibility.addEventListener("click", () => {
              window.wrapperClickCount =
                (window.wrapperClickCount ?? 0) + 1;
              menu.hidden = false;
            });
            current.addEventListener("click", () => {
              window.innerTextClickCount =
                (window.innerTextClickCount ?? 0) + 1;
            });
            menu.querySelector("button").addEventListener("click", () => {
              current.textContent = "仅自己可见";
              menu.hidden = true;
            });
          </script>
        </body>
      </html>
    """
