import base64
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
_PREVIEW_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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


def test_parse_exact_live_private_ok_response() -> None:
    result = parse_publish_response(
        {
            "ok": 1,
            "msg": "发布成功",
            "data": {
                "idstr": "5362366986061756",
                "mid": "5362366986061756",
                "title": {"text": "仅自己可见"},
            },
        }
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.weibo_mid == "5362366986061756"
    assert result.weibo_url == "https://weibo.com/detail/5362366986061756"
    assert result.error is None


@pytest.mark.parametrize("ok_value", [1, True, "1", "true"])
def test_parse_ok_success_values_with_post_id(ok_value: object) -> None:
    result = parse_publish_response(
        {
            "ok": ok_value,
            "data": {"idstr": "123456"},
        }
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.weibo_mid == "123456"


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
        {"ok": 0, "msg": "发布失败"},
        {"ok": False, "message": "request rejected"},
        {"ok": 1, "code": 100001, "msg": "code rejected"},
    ],
)
def test_parse_explicit_failure_returns_failed_message(
    payload: dict[str, Any],
) -> None:
    result = parse_publish_response(payload)

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error in {"发布失败", "request rejected", "code rejected"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"msg": "no outcome"},
        {"ok": None},
        {"ok": 2, "data": {"idstr": "123"}},
        {"ok": 1, "data": {}},
        {"ok": 1, "data": {"mid": "   "}},
    ],
)
def test_parse_missing_or_invalid_outcome_is_unknown(
    payload: dict[str, Any],
) -> None:
    result = parse_publish_response(payload)

    assert result.outcome is AttemptOutcome.UNKNOWN
    assert result.error


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
async def test_private_submit_uses_exact_live_payload_and_one_click(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    job = _job(images=[image], visibility=Visibility.PRIVATE)

    result, click_count = await _submit_with_response(
        app_config,
        monkeypatch,
        {
            "ok": 1,
            "msg": "发布成功",
            "data": {
                "idstr": "5362366986061756",
                "mid": "5362366986061756",
                "title": {"text": "仅自己可见"},
            },
        },
        job=job,
    )

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.weibo_mid == "5362366986061756"
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


@pytest.mark.asyncio
async def test_submit_text_drift_fails_before_click(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_dir = app_config.artifacts_dir / "job-text-drift"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"code": 100000, "data": {"idstr": "123"}},
        artifact_dir=artifact_dir,
    )

    try:
        assert publisher.page is not None
        await publisher.page.fill("#body", "changed after preparation")
        result = await publisher.submit(_job(), artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error is not None and "composer text" in result.error
    assert click_count == 0
    assert observed["post_requests"] == 0


@pytest.mark.asyncio
async def test_submit_missing_image_attachment_fails_before_click(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nattached")
    job = _job(images=[image])
    artifact_dir = app_config.artifacts_dir / "job-missing-image"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"code": 100000, "data": {"idstr": "123"}},
        artifact_dir=artifact_dir,
        job=job,
    )

    try:
        assert publisher.page is not None
        await publisher.page.set_input_files("#image", [])
        result = await publisher.submit(job, artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error is not None and "attached image" in result.error
    assert click_count == 0
    assert observed["post_requests"] == 0


@pytest.mark.asyncio
async def test_submit_same_count_image_replacement_fails_before_click(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_dir = tmp_path / "original"
    replacement_dir = tmp_path / "replacement"
    original_dir.mkdir()
    replacement_dir.mkdir()
    original = original_dir / "image.png"
    replacement = replacement_dir / "image.png"
    original.write_bytes(b"\x89PNG\r\n\x1a\noriginal")
    replacement.write_bytes(b"\x89PNG\r\n\x1a\nreplaced")
    assert original.stat().st_size == replacement.stat().st_size
    job = _job(images=[original])
    artifact_dir = app_config.artifacts_dir / "job-replaced-image"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"code": 100000, "data": {"idstr": "123"}},
        artifact_dir=artifact_dir,
        job=job,
    )

    try:
        assert publisher.page is not None
        await publisher.page.set_input_files("#image", str(replacement))
        result = await publisher.submit(job, artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error is not None and "attached image" in result.error
    assert click_count == 0
    assert observed["post_requests"] == 0


@pytest.mark.asyncio
async def test_submit_unfinished_upload_fails_before_click(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nattached")
    job = _job(images=[image])
    artifact_dir = app_config.artifacts_dir / "job-uploading"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"code": 100000, "data": {"idstr": "123"}},
        artifact_dir=artifact_dir,
        job=job,
    )

    try:
        assert publisher.page is not None
        await publisher.page.evaluate(
            """
            () => {
              const indicator = document.createElement("div");
              indicator.className = "uploading";
              indicator.textContent = "Uploading";
              document.body.appendChild(indicator);
            }
            """
        )
        result = await publisher.submit(job, artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error is not None and "upload has not completed" in result.error
    assert click_count == 0
    assert observed["post_requests"] == 0


@pytest.mark.parametrize(
    ("visibility_label", "expected_error"),
    [
        ("仅自己可见", "visibility does not match public job"),
        (None, "visibility could not be verified"),
    ],
)
@pytest.mark.asyncio
async def test_submit_requires_verified_public_visibility(
    visibility_label: str | None,
    expected_error: str,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_dir = app_config.artifacts_dir / "job-visibility"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"code": 100000, "data": {"idstr": "123"}},
        artifact_dir=artifact_dir,
    )

    try:
        assert publisher.page is not None
        visibility = publisher.page.locator("#visibility")
        if visibility_label is None:
            await visibility.evaluate("element => element.remove()")
        else:
            await visibility.evaluate(
                """
                (element, label) => {
                  element.textContent = label;
                  element.setAttribute("aria-label", label);
                }
                """,
                visibility_label,
            )
        result = await publisher.submit(_job(), artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error is not None and expected_error in result.error
    assert click_count == 0
    assert observed["post_requests"] == 0


@pytest.mark.asyncio
async def test_submit_rejects_public_label_for_private_job_before_click(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(visibility=Visibility.PRIVATE)
    artifact_dir = app_config.artifacts_dir / "job-private-mismatch"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"ok": 1, "data": {"idstr": "123"}},
        artifact_dir=artifact_dir,
        job=job,
    )

    try:
        assert publisher.page is not None
        await publisher.page.locator("#visibility").evaluate(
            """
            (element) => {
              element.textContent = "公开";
              element.setAttribute("aria-label", "公开");
            }
            """
        )
        result = await publisher.submit(job, artifact_dir)
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.FAILED
    assert result.error is not None
    assert "visibility does not match private job" in result.error
    assert click_count == 0
    assert observed["post_requests"] == 0


@pytest.mark.asyncio
async def test_prepare_selects_and_verifies_private_visibility_after_content(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    job = _job(images=[image], visibility=Visibility.PRIVATE)
    artifact_dir = app_config.artifacts_dir / "job-private-prepare"

    publisher, _ = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"ok": 1, "data": {"idstr": "123"}},
        artifact_dir=artifact_dir,
        job=job,
    )

    try:
        assert publisher.page is not None
        current_label = await publisher.page.locator("#visibility").inner_text()
        prepared_before_opening = await publisher.page.evaluate(
            "window.visibilityOpenedWithPreparedContent"
        )
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert current_label == "仅自己可见"
    assert prepared_before_opening is True
    assert click_count == 0
    assert (artifact_dir / "preview.png").is_file()


@pytest.mark.asyncio
async def test_submit_revalidates_image_and_visibility_before_one_click(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nattached")
    job = _job(images=[image])
    artifact_dir = app_config.artifacts_dir / "job-revalidated"
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload={"code": 100000, "data": {"idstr": "456"}},
        artifact_dir=artifact_dir,
        job=job,
    )

    try:
        result = await publisher.submit(job, artifact_dir)
        assert publisher.page is not None
        click_count = await publisher.page.evaluate(
            "window.publishClickCount ?? 0"
        )
    finally:
        await publisher.close()

    assert result.outcome is AttemptOutcome.SUCCESS
    assert result.weibo_mid == "456"
    assert click_count == 1
    assert observed["post_requests"] == 1


async def _submit_with_response(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    response_payload: dict[str, Any],
    *,
    artifact_dir: Path | None = None,
    job: Job | None = None,
) -> tuple[PublishResult, int]:
    artifact_dir = artifact_dir or app_config.artifacts_dir / "job-submit"
    job = job or _job()
    publisher, observed = await _prepared_publisher(
        app_config,
        monkeypatch,
        response_payload=response_payload,
        artifact_dir=artifact_dir,
        job=job,
    )

    try:
        result = await publisher.submit(job, artifact_dir)
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
    job: Job | None = None,
) -> tuple[WeiboPublisher, dict[str, int]]:
    observed = {"post_requests": 0}
    monkeypatch.setattr(
        weibo,
        "persistent_context",
        _routed_context_factory(response_payload, observed),
    )
    publisher = WeiboPublisher(_local_config(app_config))
    await publisher.prepare(job or _job(), artifact_dir)
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
            await context.route(
                "https://wx*.sinaimg.cn/**",
                _preview_image_route,
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
          <input id="image" type="file" accept="image/*" multiple style="display:none">
          <button id="visibility" type="button" aria-label="公开">公开</button>
          <div id="visibility-options" hidden>
            <button type="button">公开</button>
            <button type="button">粉丝</button>
            <button type="button">好友圈</button>
            <button type="button">仅自己可见</button>
            <button type="button">群可见</button>
          </div>
          <button id="publish" type="button">发布</button>
        </main>
        <script>
          const visibility = document.querySelector("#visibility");
          const visibilityOptions = document.querySelector("#visibility-options");
          visibility.addEventListener("click", () => {{
            window.visibilityOpenedWithPreparedContent =
              document.querySelector("#body").value.length > 0 &&
              document.querySelector("#image").files.length > 0;
            visibilityOptions.hidden = false;
          }});
          for (const option of visibilityOptions.querySelectorAll("button")) {{
            option.addEventListener("click", () => {{
              visibility.textContent = option.textContent;
              visibility.setAttribute("aria-label", option.textContent);
              visibilityOptions.hidden = true;
            }});
          }}
          document.querySelector("#image").addEventListener("change", (event) => {{
            for (const file of Array.from(event.target.files ?? [])) {{
              const image = document.createElement("img");
              image.className = "woo-picture-img";
              image.src =
                `https://wx2.sinaimg.cn/large/${{encodeURIComponent(file.name)}}`;
              event.target.parentElement.appendChild(image);
            }}
          }});
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


async def _preview_image_route(route) -> None:
    await route.fulfill(
        status=200,
        content_type="image/png",
        body=_PREVIEW_PNG_BYTES,
    )


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


def _job(
    *,
    images: list[Path] | None = None,
    visibility: Visibility = Visibility.PUBLIC,
) -> Job:
    return Job(
        id="job-submit",
        text="submit test",
        images=images or [],
        publish_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="Asia/Shanghai",
        visibility=visibility,
        status=JobStatus.READY,
        fingerprint="fingerprint",
        created_at=datetime.now(UTC),
    )
