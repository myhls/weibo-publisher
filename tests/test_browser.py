from dataclasses import replace
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from weibo_publisher import browser
from weibo_publisher.cli import main
from weibo_publisher.config import AppConfig
from weibo_publisher.logging_setup import configure_logging


def _local_config(config: AppConfig) -> AppConfig:
    return replace(config, browser_channel="", headless=True)


def _write_page(path: Path, body: str, *, redirect: str | None = None) -> str:
    script = ""
    if redirect:
        script = f'<script>setTimeout(() => location.replace("{redirect}"), 50);</script>'
    path.write_text(
        f"<!doctype html><html><head>{script}</head><body>{body}</body></html>",
        encoding="utf-8",
    )
    return path.resolve().as_uri()


@pytest.mark.asyncio
async def test_logged_in_detection_rejects_login_and_verification_pages(
    tmp_path: Path,
) -> None:
    home = _write_page(
        tmp_path / "home.html",
        '<a class="gn_name">Account</a>'
        '<form action="/login" hidden><input name="username"></form>',
    )
    login = _write_page(
        tmp_path / "login.html",
        '<a class="gn_name">Account</a>',
    )
    verification = _write_page(
        tmp_path / "verification.html",
        '<a class="gn_name">Account</a><div id="captcha"></div>',
    )

    async with async_playwright() as playwright:
        chrome = await playwright.chromium.launch(headless=True)
        page = await chrome.new_page()
        try:
            await page.goto(home)
            assert await browser._logged_in(page) is True

            await page.goto(login)
            assert await browser._logged_in(page) is False

            await page.goto(verification)
            assert await browser._logged_in(page) is False
        finally:
            await chrome.close()


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
async def test_check_session_uses_local_home_page(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _write_page(tmp_path / "home.html", '<a class="gn_name">Account</a>')
    monkeypatch.setattr(browser, "HOME_URL", home)

    assert await browser.check_session(_local_config(app_config)) is True
    assert app_config.profile_dir.is_dir()


@pytest.mark.asyncio
async def test_login_polls_until_manual_login_redirects_home(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _write_page(tmp_path / "home.html", '<a class="gn_name">Account</a>')
    login = _write_page(
        tmp_path / "signin.html",
        "<p>Scan the QR code in the browser.</p>",
        redirect=home,
    )
    monkeypatch.setattr(browser, "LOGIN_URL", login)
    monkeypatch.setattr(browser, "HOME_URL", home)

    result = await browser.login(
        _local_config(app_config),
        timeout_seconds=1,
        poll_interval_seconds=0.02,
    )

    assert result is True


@pytest.mark.asyncio
async def test_doctor_checks_all_local_prerequisites(
    app_config: AppConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _write_page(tmp_path / "home.html", '<a class="gn_name">Account</a>')
    monkeypatch.setattr(browser, "HOME_URL", home)

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
    home = _write_page(tmp_path / "home.html", '<a class="gn_name">Account</a>')
    monkeypatch.setattr(browser, "HOME_URL", home)

    assert main(["--root", str(tmp_path), "doctor"]) == 0
    doctor_output = capsys.readouterr().out
    assert "Session: OK" in doctor_output
    assert "cookie" not in doctor_output.lower()

    async def fake_login(config: AppConfig) -> bool:
        assert config.root == tmp_path
        return True

    monkeypatch.setattr("weibo_publisher.cli.login", fake_login)
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
