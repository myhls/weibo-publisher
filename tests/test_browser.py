import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Page, async_playwright

from weibo_publisher import browser
from weibo_publisher.cli import main
from weibo_publisher.config import AppConfig, ensure_runtime_dirs
from weibo_publisher.logging_setup import configure_logging
from weibo_publisher.repository import JobRepository


def _local_config(config: AppConfig) -> AppConfig:
    return replace(config, browser_channel="", headless=True)


def _html(body: str, script: str = "") -> str:
    return f"<!doctype html><html><head>{script}</head><body>{body}</body></html>"


_AUTHENTICATED_HOME = _html(
    "<header>"
    '<a class="gn_name" href="/u/123">Account</a>'
    '<span class="verify">Verified badge</span>'
    "</header>"
)
_PUBLIC_HOME = _html('<header><a href="/u/123">Public profile</a></header>')
_COMPOSER_HOME = _html(
    "<main>"
    '<textarea placeholder="有什么新鲜事想分享给大家？"></textarea>'
    "<button>发送</button>"
    "</main>"
)
_CHALLENGE_HOME = _html(
    '<header><a class="gn_name" href="/u/123">Account</a></header>'
    '<div role="dialog"><h2>安全验证</h2><p>Complete verification.</p></div>'
)
_COMPOSER_CHALLENGE_HOME = _html(
    '<main><textarea placeholder="有什么新鲜事想分享给大家？"></textarea></main>'
    '<div role="dialog"><h2>安全验证</h2><p>Complete verification.</p></div>'
)
_DELAYED_HOME = _html(
    "",
    """
    <script>
      setTimeout(() => {
        document.body.innerHTML =
          '<header><a class="gn_name" href="/u/123">Account</a></header>';
      }, 150);
    </script>
    """,
)
_LOGIN_REDIRECT = _html(
    "<p>Scan the QR code in the browser.</p>",
    """
    <script>
      setTimeout(() => location.replace("https://weibo.com/"), 50);
    </script>
    """,
)


@asynccontextmanager
async def _routed_page(routes: dict[str, str]) -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        chrome = await playwright.chromium.launch(headless=True)
        context = await chrome.new_context()
        for url, body in routes.items():
            await context.route(url, _html_route(body))
        page = await context.new_page()
        try:
            yield page
        finally:
            await chrome.close()


def _routed_context_factory(routes: dict[str, str]):
    @asynccontextmanager
    async def routed_context(config: AppConfig) -> AsyncIterator[BrowserContext]:
        del config
        async with async_playwright() as playwright:
            chrome = await playwright.chromium.launch(headless=True)
            context = await chrome.new_context()
            for url, body in routes.items():
                await context.route(url, _html_route(body))
            try:
                yield context
            finally:
                await chrome.close()

    return routed_context


def _html_route(body: str):
    async def fulfill(route) -> None:
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=body,
        )

    return fulfill


@pytest.mark.asyncio
async def test_logged_in_requires_weibo_origin_and_authenticated_signal() -> None:
    routes = {
        "https://weibo.com/home": _AUTHENTICATED_HOME,
        "https://weibo.com/verified": _AUTHENTICATED_HOME,
        "https://weibo.com/public": _PUBLIC_HOME,
        "https://weibo.com/session": _CHALLENGE_HOME,
        "https://example.com/": _AUTHENTICATED_HOME,
    }
    async with _routed_page(routes) as page:
        await page.goto("https://weibo.com/home")
        assert await browser._logged_in(page) is True

        await page.goto("https://weibo.com/verified")
        assert await browser._logged_in(page) is True

        await page.goto("https://weibo.com/public")
        assert await browser._logged_in(page) is False

        await page.goto("https://weibo.com/session")
        assert await browser._logged_in(page) is False

        await page.goto("https://example.com/")
        assert await browser._logged_in(page) is False


@pytest.mark.asyncio
async def test_session_detects_composer_but_does_not_treat_profile_links_as_auth() -> None:
    routes = {
        "https://weibo.com/": _COMPOSER_HOME,
        "https://weibo.com/public": _PUBLIC_HOME,
        "https://weibo.com/challenge": _COMPOSER_CHALLENGE_HOME,
    }
    async with _routed_page(routes) as page:
        await page.goto("https://weibo.com/")
        assert await browser._session_state(page) == "logged_in"

        await page.goto("https://weibo.com/public")
        assert await browser._session_state(page) == "pending"

        await page.goto("https://weibo.com/challenge")
        assert await browser._session_state(page) == "blocked"


@pytest.mark.asyncio
async def test_persistent_context_closes_after_body_error(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = app_config
    fake_context = _FakeContext()
    fake_chromium = _FakeChromium(fake_context)

    monkeypatch.setattr(
        browser,
        "async_playwright",
        lambda: _FakePlaywrightManager(fake_chromium),
    )

    with pytest.raises(RuntimeError, match="body failed"):
        async with browser.persistent_context(config):
            raise RuntimeError("body failed")

    assert fake_context.closed is True
    assert fake_chromium.launch_options["user_data_dir"] == str(config.profile_dir)
    assert fake_chromium.launch_options["headless"] is False
    assert fake_chromium.launch_options["channel"] == "msedge"


@pytest.mark.asyncio
async def test_check_session_waits_for_delayed_spa_marker(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser,
        "persistent_context",
        _routed_context_factory({"https://weibo.com/": _DELAYED_HOME}),
    )

    assert (
        await browser.check_session(
            _local_config(app_config),
            timeout_seconds=1,
        )
        is True
    )


@pytest.mark.asyncio
async def test_login_polls_until_manual_login_redirects_home(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser,
        "persistent_context",
        _routed_context_factory(
            {
                browser.LOGIN_URL: _LOGIN_REDIRECT,
                browser.HOME_URL: _AUTHENTICATED_HOME,
            }
        ),
    )

    result = await browser.login(
        _local_config(app_config),
        timeout_seconds=1,
        poll_interval_seconds=0.02,
    )

    assert result is True


@pytest.mark.asyncio
async def test_doctor_checks_all_local_prerequisites_without_creating_database(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser,
        "persistent_context",
        _routed_context_factory({"https://weibo.com/": _AUTHENTICATED_HOME}),
    )

    checks = await browser.run_doctor(_local_config(app_config))

    assert all(check.ok for check in checks), checks
    assert {check.name for check in checks} == {
        "Packages",
        "Browser",
        "Runtime directories",
        "Database",
        "Session",
    }
    assert all("cookie" not in check.detail.lower() for check in checks)
    assert all(str(app_config.profile_dir) not in check.detail for check in checks)
    assert app_config.db_path.exists() is False


def test_database_check_never_creates_missing_database(app_config: AppConfig) -> None:
    check = browser._check_database(app_config)

    assert check.ok is True
    assert check.detail == "not created yet"
    assert app_config.db_path.exists() is False


def test_database_check_does_not_mutate_existing_database(app_config: AppConfig) -> None:
    ensure_runtime_dirs(app_config)
    JobRepository(app_config.db_path).initialize()
    original_bytes = app_config.db_path.read_bytes()

    check = browser._check_database(app_config)

    assert check.ok is True
    assert check.detail == "read-only quick check passed"
    assert app_config.db_path.read_bytes() == original_bytes


def test_doctor_can_start_and_report_missing_playwright(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[app]\nbrowser_channel = ""\nheadless = true\n',
        encoding="utf-8",
    )
    code = """
import sys

sys.modules["playwright"] = None
sys.modules["playwright.async_api"] = None

from weibo_publisher.cli import main

raise SystemExit(main(["--root", sys.argv[1], "doctor"]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Packages: FAIL - missing: Playwright" in result.stdout
    assert "Session: FAIL - not checked because the browser is unavailable" in result.stdout
    assert not (tmp_path / "runtime" / "state.db").exists()


def test_configure_logging_writes_to_runtime_logs(app_config: AppConfig) -> None:
    configure_logging(app_config)

    log_file = app_config.logs_dir / "publisher.log"
    assert log_file.is_file()


def test_cli_wires_login_and_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[app]\nbrowser_channel = ""\nheadless = true\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        browser,
        "persistent_context",
        _routed_context_factory({"https://weibo.com/": _AUTHENTICATED_HOME}),
    )

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    doctor_output = capsys.readouterr().out
    assert "Session: OK" in doctor_output
    assert "Database: OK - not created yet" in doctor_output
    assert "cookie" not in doctor_output.lower()
    assert not (tmp_path / "runtime" / "state.db").exists()

    async def fake_login(config: AppConfig) -> bool:
        assert config.root == tmp_path
        return True

    monkeypatch.setattr(browser, "login", fake_login)
    assert main(["--root", str(tmp_path), "login"]) == 0
    assert "Login successful" in capsys.readouterr().out


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[object] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.launch_options: dict[str, object] = {}

    async def launch_persistent_context(self, **kwargs: object) -> _FakeContext:
        self.launch_options = kwargs
        return self.context


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium


class _FakePlaywrightManager:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.playwright = _FakePlaywright(chromium)

    async def __aenter__(self) -> _FakePlaywright:
        return self.playwright

    async def __aexit__(self, *args: object) -> None:
        return None
