from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import logging
import sqlite3
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from weibo_publisher.config import AppConfig, ensure_runtime_dirs

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

LOGIN_URL = "https://passport.weibo.com/sso/signin"
HOME_URL = "https://weibo.com/"

_DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_SESSION_WAIT_TIMEOUT_SECONDS = 5.0
_SESSION_POLL_INTERVAL_SECONDS = 0.2
_NAVIGATION_TIMEOUT_MS = 30_000
_ALLOWED_SESSION_HOSTS = frozenset({"weibo.com", "www.weibo.com"})
_BLOCKED_HOST_MARKERS = ("passport.weibo.com", "visitor.weibo.com")
_BLOCKED_PATH_MARKERS = (
    "/login",
    "/signin",
    "/verify",
    "/verification",
    "/captcha",
    "/challenge",
)
_BLOCKED_SELECTORS = (
    'form[action*="login"]',
    'form[action*="signin"]',
    'form[action*="passport.weibo"]',
    "#captcha",
    '[data-testid="captcha"]',
    'iframe[src*="captcha"]',
    '[class*="verifyDialog"]',
    '[class*="verify_dialog"]',
    '[class*="security-verify"]',
    '[class*="security_verify"]',
    '[role="dialog"]:has-text("安全验证")',
    '[role="dialog"]:has-text("请输入验证码")',
    '[role="dialog"]:has-text("完成验证")',
    '[role="dialog"]:has-text("身份验证")',
    '[role="dialog"]:has-text("访问过于频繁")',
)
_VERIFICATION_TITLES = (
    "安全验证",
    "微博验证",
    "验证码",
    "身份验证",
    "访问异常",
    "访问过于频繁",
)
_AUTHENTICATED_SELECTORS = (
    '[class*="gn_name"]',
    'textarea[placeholder*="有什么新鲜事"]',
    'textarea[placeholder*="分享给大家"]',
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


def _playwright_api():
    return importlib.import_module("playwright.async_api")


def async_playwright():
    return _playwright_api().async_playwright()


@asynccontextmanager
async def persistent_context(config: AppConfig) -> AsyncIterator[BrowserContext]:
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        launch_options: dict[str, object] = {
            "user_data_dir": str(config.profile_dir),
            "headless": config.headless,
            "viewport": {"width": 1365, "height": 900},
            "locale": "zh-CN",
            "timezone_id": config.default_timezone,
        }
        if config.browser_channel:
            launch_options["channel"] = config.browser_channel

        context = await playwright.chromium.launch_persistent_context(
            **launch_options
        )
        try:
            yield context
        finally:
            await context.close()


async def login(
    config: AppConfig,
    *,
    timeout_seconds: float = _DEFAULT_LOGIN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> bool:
    logger.info("Opening Weibo login in the dedicated browser profile")
    playwright_error = _playwright_api().Error
    async with persistent_context(config) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=_NAVIGATION_TIMEOUT_MS,
        )

        deadline = time.monotonic() + max(timeout_seconds, 0)
        while True:
            for open_page in context.pages:
                if open_page.is_closed():
                    continue
                try:
                    logged_in = await _logged_in(open_page)
                except playwright_error:
                    continue
                if logged_in:
                    logger.info("Weibo login detected")
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval_seconds, remaining))

    logger.info("Weibo login was not completed before the timeout")
    return False


async def check_session(
    config: AppConfig,
    *,
    timeout_seconds: float = _SESSION_WAIT_TIMEOUT_SECONDS,
) -> bool:
    async with persistent_context(config) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(
            HOME_URL,
            wait_until="domcontentloaded",
            timeout=_NAVIGATION_TIMEOUT_MS,
        )
        valid = await _wait_for_session(page, timeout_seconds)
    logger.info("Weibo session check: %s", "valid" if valid else "missing")
    return valid


async def _logged_in(page: Page) -> bool:
    return await _session_state(page) == "logged_in"


async def _session_state(page: Page) -> str:
    parsed = urlparse(page.url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_SESSION_HOSTS
        or any(marker in host for marker in _BLOCKED_HOST_MARKERS)
        or any(marker in path for marker in _BLOCKED_PATH_MARKERS)
    ):
        return "blocked"

    title = (await page.title()).lower()
    if any(marker.lower() in title for marker in _VERIFICATION_TITLES):
        return "blocked"

    for selector in _BLOCKED_SELECTORS:
        if await _has_visible_element(page, selector):
            return "blocked"

    for selector in _AUTHENTICATED_SELECTORS:
        if await _has_visible_element(page, selector):
            return "logged_in"
    return "pending"


async def _wait_for_session(page: Page, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        state = await _session_state(page)
        if state == "logged_in":
            return True
        if state == "blocked":
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_SESSION_POLL_INTERVAL_SECONDS, remaining))


async def _has_visible_element(page: Page, selector: str) -> bool:
    locator = page.locator(selector)
    for index in range(min(await locator.count(), 5)):
        if await locator.nth(index).is_visible():
            return True
    return False


async def run_doctor(config: AppConfig) -> list[DoctorCheck]:
    checks = [
        _check_packages(),
        await _check_browser(config),
        _check_runtime_directories(config),
        _check_database(config),
    ]

    browser_check = checks[1]
    if not browser_check.ok:
        checks.append(
            DoctorCheck(
                "Session",
                False,
                "not checked because the browser is unavailable",
            )
        )
        return checks

    try:
        session_ok = await check_session(config)
    except Exception:
        logger.exception("Session diagnostic failed")
        checks.append(DoctorCheck("Session", False, "check failed"))
    else:
        detail = "valid" if session_ok else "missing or verification required"
        checks.append(DoctorCheck("Session", session_ok, detail))
    return checks


def _check_packages() -> DoctorCheck:
    packages = (
        ("playwright", "Playwright"),
        ("apscheduler", "APScheduler"),
    )
    missing: list[str] = []
    versions: list[str] = []
    for module_name, distribution_name in packages:
        try:
            importlib.import_module(module_name)
            version = importlib.metadata.version(distribution_name)
        except (ImportError, importlib.metadata.PackageNotFoundError):
            missing.append(distribution_name)
        else:
            versions.append(f"{distribution_name} {version}")

    if missing:
        return DoctorCheck("Packages", False, f"missing: {', '.join(missing)}")
    return DoctorCheck("Packages", True, ", ".join(versions))


async def _check_browser(config: AppConfig) -> DoctorCheck:
    channel = config.browser_channel or "bundled Chromium"
    launch_options: dict[str, object] = {"headless": True}
    if config.browser_channel:
        launch_options["channel"] = config.browser_channel

    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_options)
            return DoctorCheck("Browser", True, f"{channel} available")
    except Exception:
        logger.exception("Browser diagnostic failed")
        return DoctorCheck("Browser", False, f"{channel} unavailable")
    finally:
        if browser is not None:
            await browser.close()


def _check_runtime_directories(config: AppConfig) -> DoctorCheck:
    try:
        ensure_runtime_dirs(config)
        directories = (
            config.runtime_dir,
            config.profile_dir,
            config.artifacts_dir,
            config.logs_dir,
        )
        if not all(directory.is_dir() for directory in directories):
            return DoctorCheck("Runtime directories", False, "one or more missing")
        with tempfile.NamedTemporaryFile(dir=config.runtime_dir):
            pass
    except OSError:
        logger.exception("Runtime directory diagnostic failed")
        return DoctorCheck("Runtime directories", False, "not writable")
    return DoctorCheck("Runtime directories", True, "present and writable")


def _check_database(config: AppConfig) -> DoctorCheck:
    if not config.db_path.exists():
        return DoctorCheck("Database", True, "not created yet")
    try:
        database_uri = f"{config.db_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            return DoctorCheck("Database", False, "quick check failed")
    except (OSError, sqlite3.Error):
        logger.exception("Database diagnostic failed")
        return DoctorCheck("Database", False, "not accessible")
    return DoctorCheck("Database", True, "read-only quick check passed")
