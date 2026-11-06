import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import BrowserContext, async_playwright

from weibo_publisher import weibo
from weibo_publisher.config import AppConfig
from weibo_publisher.models import (
    AttemptOutcome,
    Job,
    JobStatus,
    PublishResult,
    Visibility,
)
from weibo_publisher.weibo import WeiboPublisher, parse_publish_response

_PUBLISH_URL = "https://weibo.com/ajax/statuses/update"


def test_parse_success_response() -> None:
    result = parse_publish_response(
        {
            "code": 100000,
            "data": {"idstr": "123456"},
        }
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.weibo_mid == "123456"
    assert result.weibo_url == "https://weibo.com/detail/123456"


def test_parse_error_response() -> None:
    result = parse_publish_response(
        {
            "code": 100001,
            "msg": "发布失败",
        }
    )

    assert result.outcome is AttemptOutcome.FAILED
    assert "发布失败" in result.error


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 100000, "data": {}},
        {"code": 100000, "data": {"idstr": "   "}},
        {"code": 100000, "data": {"idstr": ""}},
    ],
)
def test_parse_success_without_valid_id_is_unknown(payload: dict[str, Any]) -> None:
    result = parse_publish_response(payload)

    assert result.outcome is AttemptOutcome.UNKNOWN
    assert result.error == "Success code without post ID"


@pytest.mark.asyncio
async def test_submit_requires_prepare(tmp_path: Path, app_config: AppConfig) -> None:
    publisher = WeiboPublisher(_local_config(app_config))

    with pytest.raises(RuntimeError, match="not been prepared"):
        await publisher.submit(_job(), tmp_path)


@pytest.mark.asyncio
async def test_submit_success_uses_one_click_and_returns_detail_url(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, click_count = await _submit_with_response(
        app_config,
        monkeypatch,
        {"code": 100000, "data": {"idstr": "987654"}},
    )

    assert result == PublishResult(
        outcome=AttemptOutcome.SUCCESS,
        weibo_mid="987654",
        weibo_url="https://weibo.com/detail/987654",
    )
    assert click_count == 1


@pytest.mark.asyncio
async def test_submit_error_response_returns_failed_message(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, click_count = await _submit_with_response(
        app_config,
        monkeypatch,
        {"code": 100001, "msg": "发布失败"},
    )

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error == "发布失败"
    assert click_count == 1


@pytest.mark.asyncio
async def test_submit_success_without_id_is_unknown_and_does_not_reclick(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_dir = app_config.artifacts_dir / "job-no-id"
    result, click_count = await _submit_with_response(
        app_config,
        monkeypatch,
        {"code": 100000, "data": {}},
        artifact_dir=artifact_dir,
    )

    assert result.outcome is AttemptOutcome.UNKNOWN
    assert result.error == "Success code without post ID"
    assert click_count == 1
    assert (artifact_dir / "submit-unknown.png").is_file()


@pytest.mark.asyncio
async def test_submit_timeout_after_click_is_unknown_and_does_not_reclick(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_dir = app_config.artifacts_dir / "job-timeout"
    monkeypatch.setattr(weibo, "_PUBLISH_RESPONSE_TIMEOUT_MS", 100)
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload=None,
        artifact_dir=artifact_dir,
    )

    try:
        result = await publisher.submit(_job(), artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.UNKNOWN
    assert result.error is not None and "Submit outcome unknown" in result.error
    assert click_count == 1
    assert observed["post_requests"] == 0
    assert (artifact_dir / "submit-unknown.png").is_file()


async def _submit_with_response(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    response_payload: dict[str, Any],
    *,
    artifact_dir: Path | None = None,
) -> tuple[PublishResult, int]:
    artifact_dir = artifact_dir or app_config.artifacts_dir / "job-submit"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload=response_payload,
        artifact_dir=artifact_dir,
    )

    try:
        result = await publisher.submit(_job(), artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
        assert observed["post_requests"] == 1
        screenshot_name = (
            "submit-unknown.png"
            if result.outcome is AttemptOutcome.UNKNOWN
            else "submit-result.png"
        )
        assert (artifact_dir / screenshot_name).is_file()
    finally:
        await publisher.close()

    return result, click_count


async def _prepared_publisher(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_payload: dict[str, Any] | None,
    artifact_dir: Path,
) -> tuple[WeiboPublisher, dict[str, int]]:
    observed = {"post_requests": 0}
    monkeypatch.setattr(
        weibo,
        "persistent_context",
        _routed_context_factory(response_payload, observed),
    )
    publisher = WeiboPublisher(_local_config(app_config))
    await publisher.prepare(_job(), artifact_dir)
    return publisher, observed


def _routed_context_factory(
    response_payload: dict[str, Any] | None,
    observed: dict[str, int],
):
    @asynccontextmanager
    async def routed_context(config: AppConfig) -> AsyncIterator[BrowserContext]:
        del config
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.route(
                "https://weibo.com/",
                _html_route(_page_html(sends_post=response_payload is not None)),
            )
            await context.route(
                _PUBLISH_URL,
                _publish_route(response_payload, observed),
            )
            await context.new_page()
            try:
                yield context
            finally:
                await browser.close()

    return routed_context


def _page_html(*, sends_post: bool) -> str:
    post_request = (
        'fetch("/ajax/statuses/update", { method: "POST" });'
        if sends_post
        else ""
    )
    return f"""
    <!doctype html>
    <html>
      <body>
        <header><a class="gn_name" href="/u/123">Account</a></header>
        <main>
          <textarea id="body" placeholder="分享新鲜事"></textarea>
          <button id="publish" type="button">发布</button>
        </main>
        <script>
          document.querySelector("#publish").addEventListener("click", () => {{
            window.publishClickCount = (window.publishClickCount ?? 0) + 1;
            {post_request}
          }});
        </script>
      </body>
    </html>
    """


def _html_route(body: str):
    async def fulfill(route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=body,
        )

    return fulfill


def _publish_route(
    response_payload: dict[str, Any] | None,
    observed: dict[str, int],
):
    async def fulfill(route) -> None:
        observed["post_requests"] += 1
        if response_payload is None:
            await route.abort()
            return
        await route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(response_payload, ensure_ascii=False),
        )

    return fulfill


def _local_config(config: AppConfig) -> AppConfig:
    return replace(config, browser_channel="", headless=True)


def _job() -> Job:
    return Job(
        id="job-submit",
        text="submit test",
        images=[],
        publish_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.READY,
        fingerprint="fingerprint",
        created_at=datetime.now(UTC),
    )
