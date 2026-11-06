import asyncio
import base64
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright

from weibo_publisher import weibo

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.mark.asyncio
async def test_waits_for_delayed_composer_preview_image_to_load() -> None:
    async with _page_context() as page:
        await page.context.route(
            "https://wx2.sinaimg.cn/**",
            _delayed_png_route(delay_seconds=0.3),
        )
        await page.set_content(
            """
            <main>
              <section id="composer">
                <textarea placeholder="分享新鲜事"></textarea>
                <div id="previews"></div>
                <button type="button">发布</button>
              </section>
            </main>
            <script>
              setTimeout(() => {
                const image = document.createElement("img");
                image.className = "woo-picture-img";
                image.src = "https://wx2.sinaimg.cn/large/delayed.png";
                document.querySelector("#previews").append(image);
              }, 300);
            </script>
            """
        )

        started = time.monotonic()
        await weibo._wait_for_uploaded_image_previews(
            page,
            expected_count=1,
            timeout_seconds=2.0,
        )

        assert time.monotonic() - started >= 0.5
        assert await page.locator("#previews img").is_visible()


@pytest.mark.asyncio
async def test_preview_count_is_scoped_to_composer_not_feed() -> None:
    async with _page_context() as page:
        await page.context.route(
            "https://wx1.sinaimg.cn/**",
            _delayed_png_route(delay_seconds=0),
        )
        await page.context.route(
            "https://wx2.sinaimg.cn/**",
            _delayed_png_route(delay_seconds=0),
        )
        await page.set_content(
            """
            <main>
              <section id="feed">
                <img
                  id="feed-image"
                  class="woo-picture-img"
                  src="https://wx1.sinaimg.cn/large/feed.png"
                >
              </section>
              <section id="composer">
                <textarea placeholder="分享新鲜事"></textarea>
                <div id="previews"></div>
                <button type="button">发布</button>
              </section>
            </main>
            <script>
              const feedImage = document.querySelector("#feed-image");
              feedImage.addEventListener("load", () => {
                setTimeout(() => {
                  const image = document.createElement("img");
                  image.className = "woo-picture-img";
                  image.src = "https://wx2.sinaimg.cn/large/composer.png";
                  document.querySelector("#previews").append(image);
                }, 300);
              }, { once: true });
            </script>
            """
        )
        await page.locator("#feed-image").evaluate(
            "async (image) => { await image.decode(); }"
        )

        started = time.monotonic()
        await weibo._wait_for_uploaded_image_previews(
            page,
            expected_count=1,
            timeout_seconds=2.0,
        )

        assert time.monotonic() - started >= 0.25
        assert await page.locator("#previews img").count() == 1


@pytest.mark.asyncio
async def test_timeout_raises_when_composer_preview_never_loads() -> None:
    async with _page_context() as page:
        await page.context.route(
            "https://wx2.sinaimg.cn/**",
            _aborted_route,
        )
        await page.set_content(
            """
            <main>
              <section id="composer">
                <textarea placeholder="分享新鲜事"></textarea>
                <img
                  class="woo-picture-img"
                  src="https://wx2.sinaimg.cn/large/broken.png"
                >
                <button type="button">发布</button>
              </section>
            </main>
            """
        )

        with pytest.raises(RuntimeError, match="uploaded image preview"):
            await weibo._wait_for_uploaded_image_previews(
                page,
                expected_count=1,
                timeout_seconds=0.25,
            )


@pytest.mark.asyncio
async def test_prepare_page_does_not_save_preview_after_image_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nattached")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    async with _page_context() as page:
        await page.context.route(
            "https://weibo.com/",
            _html_route(
                """
                <header><a class="gn_name" href="/u/123">Account</a></header>
                <main>
                  <textarea placeholder="分享新鲜事"></textarea>
                  <input type="file" accept="image/*">
                  <img
                    class="woo-picture-img"
                    src="https://wx2.sinaimg.cn/large/broken.png"
                  >
                  <button type="button">发布</button>
                </main>
                """
            ),
        )
        await page.context.route(
            "https://wx2.sinaimg.cn/**",
            _aborted_route,
        )
        monkeypatch.setattr(weibo, "_PREVIEW_IMAGE_TIMEOUT_SECONDS", 0.25)

        with pytest.raises(RuntimeError, match="uploaded image preview"):
            await weibo._prepare_page(page, "preview text", [image], artifact_dir)

    assert not (artifact_dir / "preview.png").exists()


@asynccontextmanager
async def _page_context() -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            yield page
        finally:
            await browser.close()


def _delayed_png_route(delay_seconds: float):
    async def fulfill(route) -> None:
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        await route.fulfill(
            status=200,
            content_type="image/png",
            body=_PNG_BYTES,
        )

    return fulfill


async def _aborted_route(route) -> None:
    await route.abort()


def _html_route(body: str):
    async def fulfill(route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=f"<!doctype html><html><body>{body}</body></html>",
        )

    return fulfill
