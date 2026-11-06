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
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from weibo_publisher.config import AppConfig, ensure_runtime_dirs
from weibo_publisher.repository import JobRepository

LOGIN_URL = "https://passport.weibo.com/sso/signin"
HOME_URL = "https://weibo.com/"

_DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_NAVIGATION_TIMEOUT_MS = 30_000
_BLOCKED_URL_MARKERS = (
    "passport",
    "login",
    "signin",
    "verify",
    "verification",
    "captcha",
    "challenge",
    "visitor.weibo",
)
_BLOCKED_SELECTORS = (
    'form[action*="login"]',
    'form[action*="signin"]',
    "#captcha",
    '[id*="captcha"]',
    '[class*="captcha"]',
    '[id*="verify"]',
    '[class*="verify"]',
)
_LOGGED_IN_SELECTORS = (
    '[class*="gn_name"]',
    'header a[href*="/u/"]',
    'a[href*="/u/"][class*="name"]',
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


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
                except PlaywrightError:
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


async def check_session(config: AppConfig) -> bool:
    async with persistent_context(config) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(
            HOME_URL,
            wait_until="domcontentloaded",
            timeout=_NAVIGATION_TIMEOUT_MS,
        )
        valid = await _logged_in(page)
    logger.info("Weibo session check: %s", "valid" if valid else "missing")
    return valid


async def _logged_in(page: Page) -> bool:
    parsed = urlparse(page.url)
    if parsed.scheme == "file":
        url_target = Path(unquote(parsed.path)).name.lower()
    else:
        url_target = f"{parsed.netloc}{parsed.path}{parsed.query}".lower()
    if any(marker in url_target for marker in _BLOCKED_URL_MARKERS):
        return False

    for selector in _BLOCKED_SELECTORS:
        if await _has_visible_element(page, selector):
            return False

    for selector in _LOGGED_IN_SELECTORS:
        if await _has_visible_element(page, selector):
            return True
    return False


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
    try:
        JobRepository(config.db_path).initialize()
        with sqlite3.connect(config.db_path) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            return DoctorCheck("Database", False, "quick check failed")
    except (OSError, sqlite3.Error):
        logger.exception("Database diagnostic failed")
        return DoctorCheck("Database", False, "not accessible")
    return DoctorCheck("Database", True, "schema accessible; quick check passed")
