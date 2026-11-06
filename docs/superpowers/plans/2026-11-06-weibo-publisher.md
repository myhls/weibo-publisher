# Weibo Publisher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-account CLI robot that publishes confirmed text-and-image Weibo posts unattended at a requested time.

**Architecture:** A Python package stores confirmed jobs in SQLite, schedules preparation three minutes before publication, drives a dedicated Playwright browser profile, and records an unambiguous published, failed, or unknown result. The runner is independent of Codex and refuses unsafe automatic retries.

**Tech Stack:** Python 3.11, Playwright async API, APScheduler 3.x, SQLite, zoneinfo, argparse, pytest, pytest-asyncio, ruff

**Spec:** `docs/superpowers/specs/2026-11-06-weibo-publisher-design.md`

## Global Constraints

- Python version is `>=3.11`.
- The project supports one Weibo account only.
- `daily_limit` defaults to `100`.
- `minimum_gap_seconds` defaults to `240`.
- Preparation starts three minutes before `publish_at`.
- The late cutoff is 30 seconds.
- The default task timezone is `Asia/Shanghai`.
- Only `approved` jobs may be scheduled.
- A job changed after confirmation must return to `draft`.
- An ambiguous submit result becomes `unknown` and is never retried automatically.
- Browser profile, SQLite state, artifacts, logs, and local config must be excluded from Git.
- The project uses MIT License.
- No password storage, captcha solving, proxy rotation, or verification bypass.

---

## File Map

```text
pyproject.toml
config.example.toml
README.md
SECURITY.md
LICENSE
.gitignore
src/weibo_publisher/__init__.py
src/weibo_publisher/__main__.py
src/weibo_publisher/config.py
src/weibo_publisher/models.py
src/weibo_publisher/validation.py
src/weibo_publisher/repository.py
src/weibo_publisher/publisher.py
src/weibo_publisher/browser.py
src/weibo_publisher/weibo.py
src/weibo_publisher/scheduler.py
src/weibo_publisher/cli.py
src/weibo_publisher/logging_setup.py
tests/conftest.py
tests/test_config.py
tests/test_models.py
tests/test_validation.py
tests/test_repository.py
tests/test_cli.py
tests/test_scheduler.py
tests/test_local_publish_flow.py
```

Responsibility boundaries:

- `config.py`: load and validate TOML configuration into one immutable object.
- `models.py`: enums and dataclasses shared by all modules.
- `validation.py`: text, image, time, fingerprint, daily-limit, and gap validation.
- `repository.py`: SQLite schema, atomic state transitions, attempts, and queries.
- `publisher.py`: protocol for fake and real publication backends.
- `browser.py`: persistent Playwright context and login/session checks.
- `weibo.py`: all Weibo DOM selectors, composer actions, and response parsing.
- `scheduler.py`: runner lock, APScheduler scanning, preparation, waiting, submission.
- `cli.py`: command parsing and user-facing command behavior.
- `logging_setup.py`: file and console logging without leaking session data.

---

### Task 1: Project Scaffold and Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `config.example.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/weibo_publisher/__init__.py`
- Create: `src/weibo_publisher/config.py`
- Test: `tests/conftest.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: none.
- Produces:
  - `AppConfig` frozen dataclass with `root`, `runtime_dir`, `db_path`, `profile_dir`, `artifacts_dir`, `logs_dir`, `browser_channel`, `headless`, `default_timezone`, `prepare_lead_seconds`, `late_cutoff_seconds`, `daily_limit`, `minimum_gap_seconds`, `allow_over_limit`, and `artifact_retention_days`.
  - `load_config(path: Path | None = None) -> AppConfig`

- [ ] **Step 1: Write the failing configuration tests**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from weibo_publisher.config import load_config


def test_load_config_uses_safe_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml", root=tmp_path)

    assert config.root == tmp_path
    assert config.runtime_dir == tmp_path / "runtime"
    assert config.db_path == tmp_path / "runtime" / "state.db"
    assert config.default_timezone == "Asia/Shanghai"
    assert config.prepare_lead_seconds == 180
    assert config.late_cutoff_seconds == 30
    assert config.daily_limit == 100
    assert config.minimum_gap_seconds == 240
    assert config.allow_over_limit is False
    assert config.headless is False


def test_load_config_rejects_unknown_timezone(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[app]\ndefault_timezone = "Mars/Olympus"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown timezone"):
        load_config(config_file, root=tmp_path)


def test_load_config_rejects_non_positive_limits(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[publishing]\ndaily_limit = 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="daily_limit"):
        load_config(config_file, root=tmp_path)
```

Create `tests/conftest.py`:

```python
from pathlib import Path

import pytest

from weibo_publisher.config import AppConfig


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    runtime = tmp_path / "runtime"
    return AppConfig(
        root=tmp_path,
        runtime_dir=runtime,
        db_path=runtime / "state.db",
        profile_dir=runtime / "profile",
        artifacts_dir=runtime / "artifacts",
        logs_dir=runtime / "logs",
        browser_channel="msedge",
        headless=False,
        default_timezone="Asia/Shanghai",
        prepare_lead_seconds=180,
        late_cutoff_seconds=30,
        daily_limit=100,
        minimum_gap_seconds=240,
        allow_over_limit=False,
        artifact_retention_days=30,
    )
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_config.py -v
```

Expected: FAIL because `weibo_publisher.config` does not exist.

- [ ] **Step 3: Create packaging and configuration**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "weibo-publisher"
version = "0.1.0"
description = "Single-account scheduled Weibo publisher"
readme = "README.md"
requires-python = ">=3.11"
license = {text = "MIT"}
dependencies = [
    "playwright>=1.52",
    "apscheduler>=3.10,<4",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "ruff>=0.8",
]

[project.scripts]
weibo-publisher = "weibo_publisher.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
target-version = "py311"
line-length = 100
```

Create `config.example.toml`:

```toml
[app]
browser_channel = "msedge"
headless = false
default_timezone = "Asia/Shanghai"

[publishing]
prepare_lead_seconds = 180
late_cutoff_seconds = 30
daily_limit = 100
minimum_gap_seconds = 240
allow_over_limit = false

[storage]
artifact_retention_days = 30
```

Create `.gitignore`:

```gitignore
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
config.toml
runtime/
*.log
.env
```

Create a minimal real `README.md` that Task 11 will expand:

```markdown
# Weibo Publisher

A local, single-account scheduled Weibo publisher for Windows.

The project is under active development. See
`docs/superpowers/specs/2026-11-06-weibo-publisher-design.md` for the design.
```

Create `src/weibo_publisher/__init__.py`:

```python
"""Scheduled single-account Weibo publisher."""

__version__ = "0.1.0"
```

Create `src/weibo_publisher/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class AppConfig:
    root: Path
    runtime_dir: Path
    db_path: Path
    profile_dir: Path
    artifacts_dir: Path
    logs_dir: Path
    browser_channel: str
    headless: bool
    default_timezone: str
    prepare_lead_seconds: int
    late_cutoff_seconds: int
    daily_limit: int
    minimum_gap_seconds: int
    allow_over_limit: bool
    artifact_retention_days: int


def load_config(path: Path | None = None, root: Path | None = None) -> AppConfig:
    root = (root or Path.cwd()).resolve()
    config_path = path or root / "config.toml"
    data: dict = {}
    if config_path.exists():
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    app = data.get("app", {})
    publishing = data.get("publishing", {})
    storage = data.get("storage", {})
    runtime = root / "runtime"

    timezone = str(app.get("default_timezone", "Asia/Shanghai"))
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone}") from exc

    daily_limit = int(publishing.get("daily_limit", 100))
    if daily_limit <= 0:
        raise ValueError("daily_limit must be positive")

    prepare_lead = int(publishing.get("prepare_lead_seconds", 180))
    late_cutoff = int(publishing.get("late_cutoff_seconds", 30))
    minimum_gap = int(publishing.get("minimum_gap_seconds", 240))
    if min(prepare_lead, late_cutoff, minimum_gap) < 0:
        raise ValueError("publishing timing values cannot be negative")

    return AppConfig(
        root=root,
        runtime_dir=runtime,
        db_path=runtime / "state.db",
        profile_dir=runtime / "profile",
        artifacts_dir=runtime / "artifacts",
        logs_dir=runtime / "logs",
        browser_channel=str(app.get("browser_channel", "msedge")),
        headless=bool(app.get("headless", False)),
        default_timezone=timezone,
        prepare_lead_seconds=prepare_lead,
        late_cutoff_seconds=late_cutoff,
        daily_limit=daily_limit,
        minimum_gap_seconds=minimum_gap,
        allow_over_limit=bool(publishing.get("allow_over_limit", False)),
        artifact_retention_days=int(storage.get("artifact_retention_days", 30)),
    )
```

- [ ] **Step 4: Run the tests and verify they pass**

Run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest tests/test_config.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml config.example.toml .gitignore README.md src/weibo_publisher tests
git commit -m "feat: scaffold configuration and package"
```

---

### Task 2: Job Models, State Machine, and Validation

**Files:**
- Create: `src/weibo_publisher/models.py`
- Create: `src/weibo_publisher/validation.py`
- Test: `tests/test_models.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Consumes: `AppConfig`.
- Produces:
  - `JobStatus`, `Visibility`, `AttemptOutcome`
  - `Job`, `PublishResult`
  - `normalize_text(text: str) -> str`
  - `build_fingerprint(text: str, images: list[Path], publish_at_utc: datetime, visibility: Visibility) -> str`
  - `parse_publish_time(value: str, timezone: str) -> datetime`
  - `validate_images(paths: list[Path]) -> list[Path]`
  - `assert_transition_allowed(current: JobStatus, target: JobStatus) -> None`

- [ ] **Step 1: Write the failing model and validation tests**

```python
# tests/test_models.py
from datetime import datetime, timezone

import pytest

from weibo_publisher.models import (
    Job,
    JobStatus,
    Visibility,
    assert_transition_allowed,
)


def test_job_returns_aware_publish_time() -> None:
    job = Job(
        id="job-1",
        text="可爱",
        images=[],
        publish_at_utc=datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.DRAFT,
        fingerprint="",
        created_at=datetime.now(timezone.utc),
    )

    assert job.publish_at_utc.tzinfo is timezone.utc


def test_disallowed_transition_raises() -> None:
    with pytest.raises(ValueError, match="draft -> published"):
        assert_transition_allowed(JobStatus.DRAFT, JobStatus.PUBLISHED)


def test_unknown_cannot_return_to_approved() -> None:
    with pytest.raises(ValueError):
        assert_transition_allowed(JobStatus.UNKNOWN, JobStatus.APPROVED)
```

```python
# tests/test_validation.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weibo_publisher.models import Visibility
from weibo_publisher.validation import (
    build_fingerprint,
    normalize_text,
    parse_publish_time,
    validate_images,
)


def test_normalize_text_strips_outer_whitespace() -> None:
    assert normalize_text("  可爱  ") == "可爱"


def test_fingerprint_changes_when_image_changes(tmp_path: Path) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"first")
    publish_at = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)

    first = build_fingerprint("可爱", [image], publish_at, Visibility.PUBLIC)
    image.write_bytes(b"second")
    second = build_fingerprint("可爱", [image], publish_at, Visibility.PUBLIC)

    assert first != second


def test_parse_publish_time_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_publish_time("2026-11-07T17:30:00", "Asia/Shanghai")


def test_parse_publish_time_converts_to_utc() -> None:
    result = parse_publish_time("2026-11-07T17:30:00+08:00", "Asia/Shanghai")
    assert result == datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)


def test_validate_images_rejects_unsupported_extension(tmp_path: Path) -> None:
    image = tmp_path / "note.txt"
    image.write_text("not an image", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported image"):
        validate_images([image])
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_models.py tests/test_validation.py -v
```

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement models and validation**

Create `src/weibo_publisher/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class JobStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    PREPARING = "preparing"
    READY = "ready"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    UNKNOWN = "unknown"
    MISSED = "missed"
    CANCELLED = "cancelled"


class Visibility(StrEnum):
    PUBLIC = "public"
    FOLLOWERS = "followers"


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class Job:
    id: str
    text: str
    images: list[Path]
    publish_at_utc: datetime
    timezone: str
    visibility: Visibility
    status: JobStatus
    fingerprint: str
    created_at: datetime
    confirmed_at: datetime | None = None
    last_attempt_at: datetime | None = None
    published_at: datetime | None = None
    weibo_mid: str | None = None
    weibo_url: str | None = None
    last_error: str | None = None
    artifact_dir: Path | None = None


@dataclass(slots=True)
class PublishResult:
    outcome: AttemptOutcome
    weibo_mid: str | None = None
    weibo_url: str | None = None
    error: str | None = None


_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.DRAFT: {JobStatus.APPROVED, JobStatus.CANCELLED},
    JobStatus.APPROVED: {
        JobStatus.PREPARING,
        JobStatus.CANCELLED,
        JobStatus.MISSED,
    },
    JobStatus.PREPARING: {
        JobStatus.READY,
        JobStatus.NEEDS_ATTENTION,
        JobStatus.FAILED,
    },
    JobStatus.READY: {
        JobStatus.PUBLISHING,
        JobStatus.NEEDS_ATTENTION,
        JobStatus.MISSED,
    },
    JobStatus.PUBLISHING: {JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.UNKNOWN},
    JobStatus.UNKNOWN: {JobStatus.NEEDS_ATTENTION},
    JobStatus.NEEDS_ATTENTION: set(),
    JobStatus.PUBLISHED: set(),
    JobStatus.FAILED: set(),
    JobStatus.MISSED: set(),
    JobStatus.CANCELLED: set(),
}


def assert_transition_allowed(current: JobStatus, target: JobStatus) -> None:
    if target not in _TRANSITIONS[current]:
        raise ValueError(f"Invalid state transition: {current} -> {target}")
```

Create `src/weibo_publisher/validation.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from weibo_publisher.models import Visibility

SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def normalize_text(text: str) -> str:
    return text.strip()


def parse_publish_time(value: str, timezone_name: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("publish time must include a timezone")
    ZoneInfo(timezone_name)
    return parsed.astimezone(timezone.utc)


def validate_images(paths: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for path in paths:
        absolute = path.expanduser().resolve()
        if absolute.suffix.lower() not in SUPPORTED_IMAGES:
            raise ValueError(f"Unsupported image: {absolute}")
        if not absolute.is_file():
            raise ValueError(f"Image does not exist: {absolute}")
        if absolute.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"Image exceeds 20 MB: {absolute}")
        resolved.append(absolute)
    return resolved


def build_fingerprint(
    text: str,
    images: list[Path],
    publish_at_utc: datetime,
    visibility: Visibility,
) -> str:
    digest = sha256()
    digest.update(normalize_text(text).encode("utf-8"))
    for image in images:
        digest.update(image.resolve().as_posix().encode("utf-8"))
        digest.update(image.read_bytes())
    digest.update(publish_at_utc.astimezone(timezone.utc).isoformat().encode("ascii"))
    digest.update(visibility.value.encode("ascii"))
    return digest.hexdigest()
```

- [ ] **Step 4: Run the tests and verify they pass**

Run:

```powershell
python -m pytest tests/test_models.py tests/test_validation.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/models.py src/weibo_publisher/validation.py tests/test_models.py tests/test_validation.py
git commit -m "feat: add job models and validation"
```

---

### Task 3: SQLite Job Repository and Atomic Transitions

**Files:**
- Create: `src/weibo_publisher/repository.py`
- Test: `tests/test_repository.py`

**Interfaces:**
- Consumes: `Job`, `JobStatus`, `Visibility`.
- Produces:
  - `JobRepository(db_path: Path)`
  - `initialize() -> None`
  - `create_job(job: Job) -> None`
  - `get_job(job_id: str) -> Job | None`
  - `list_jobs(status: JobStatus | None = None) -> list[Job]`
  - `transition(job_id: str, from_statuses: set[JobStatus], to_status: JobStatus, **fields) -> bool`
  - `count_for_local_date(local_date: date, timezone_name: str) -> int`
  - `find_gap_conflict(publish_at_utc: datetime, gap_seconds: int) -> Job | None`
  - `record_attempt(job_id: str, attempt_id: str) -> None`
  - `finish_attempt(attempt_id: str, outcome: str, error: str | None) -> None`

- [ ] **Step 1: Write the failing repository tests**

```python
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from weibo_publisher.models import Job, JobStatus, Visibility
from weibo_publisher.repository import JobRepository


def make_job() -> Job:
    return Job(
        id="job-1",
        text="可爱",
        images=[],
        publish_at_utc=datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.DRAFT,
        fingerprint="",
        created_at=datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc),
    )


def test_create_and_read_job(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    loaded = repo.get_job("job-1")
    assert loaded is not None
    assert loaded.text == "可爱"
    assert loaded.publish_at_utc == datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)


def test_conditional_transition_succeeds_once(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())

    first = repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)
    second = repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)

    assert first is True
    assert second is False
    assert repo.get_job("job-1").status is JobStatus.APPROVED


def test_list_jobs_filters_by_status(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())
    repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)

    assert [job.id for job in repo.list_jobs(JobStatus.APPROVED)] == ["job-1"]
    assert repo.list_jobs(JobStatus.DRAFT) == []


def test_count_for_local_date_excludes_cancelled(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())
    repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.CANCELLED)

    count = repo.count_for_local_date(
        date(2026, 11, 7),
        "Asia/Shanghai",
    )
    assert count == 0


def test_find_gap_conflict_returns_nearby_approved_job(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.initialize()
    repo.create_job(make_job())
    repo.transition("job-1", {JobStatus.DRAFT}, JobStatus.APPROVED)

    conflict = repo.find_gap_conflict(
        datetime(2026, 11, 7, 9, 31, tzinfo=timezone.utc),
        gap_seconds=240,
    )

    assert conflict is not None
    assert conflict.id == "job-1"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_repository.py -v
```

Expected: FAIL because `repository.py` does not exist.

- [ ] **Step 3: Implement the repository**

Create `src/weibo_publisher/repository.py` with a schema for `jobs` and
`attempts`. Use one SQLite connection per method, enable foreign keys, and
serialize `images` as JSON.

Core transition implementation:

```python
def _sql_value(self, value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def transition(
    self,
    job_id: str,
    from_statuses: set[JobStatus],
    to_status: JobStatus,
    **fields: object,
) -> bool:
    placeholders = ",".join("?" for _ in from_statuses)
    assignments = ["status = ?"]
    values: list[object] = [to_status.value]
    for key, value in fields.items():
        if key not in {
            "confirmed_at",
            "last_attempt_at",
            "published_at",
            "weibo_mid",
            "weibo_url",
            "last_error",
            "artifact_dir",
            "fingerprint",
        }:
            raise ValueError(f"Unsupported transition field: {key}")
        assignments.append(f"{key} = ?")
        values.append(self._sql_value(value))
    values.extend([job_id, *(status.value for status in from_statuses)])
    sql = (
        f"UPDATE jobs SET {', '.join(assignments)} "
        f"WHERE id = ? AND status IN ({placeholders})"
    )
    with self._connect() as connection:
        cursor = connection.execute(sql, values)
        return cursor.rowcount == 1
```

Daily-limit and gap queries:

```python
ACTIVE_STATUSES = {
    JobStatus.APPROVED,
    JobStatus.PREPARING,
    JobStatus.READY,
    JobStatus.PUBLISHING,
    JobStatus.PUBLISHED,
}


def count_for_local_date(self, local_date: date, timezone_name: str) -> int:
    start_local = datetime.combine(
        local_date,
        datetime.min.time(),
        tzinfo=ZoneInfo(timezone_name),
    )
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with self._connect() as connection:
        row = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM jobs
            WHERE status IN ({placeholders})
              AND publish_at_utc >= ?
              AND publish_at_utc < ?
            """,
            [
                *(status.value for status in ACTIVE_STATUSES),
                start_utc.isoformat(),
                end_utc.isoformat(),
            ],
        ).fetchone()
    return int(row[0])


def find_gap_conflict(
    self,
    publish_at_utc: datetime,
    gap_seconds: int,
) -> Job | None:
    start = publish_at_utc - timedelta(seconds=gap_seconds)
    end = publish_at_utc + timedelta(seconds=gap_seconds)
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with self._connect() as connection:
        row = connection.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE status IN ({placeholders})
              AND publish_at_utc BETWEEN ? AND ?
            ORDER BY publish_at_utc
            LIMIT 1
            """,
            [
                *(status.value for status in ACTIVE_STATUSES),
                start.isoformat(),
                end.isoformat(),
            ],
        ).fetchone()
    return self._row_to_job(row) if row else None
```

`_row_to_job` must parse all ISO timestamps with `datetime.fromisoformat`,
deserialize `images_json`, and convert status and visibility strings back to
their enums. `record_attempt` inserts the current UTC start time.
`finish_attempt` updates the attempt outcome and finished time.

The exact schema:

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    images_json TEXT NOT NULL,
    publish_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    visibility TEXT NOT NULL,
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    last_attempt_at TEXT,
    published_at TEXT,
    weibo_mid TEXT,
    weibo_url TEXT,
    last_error TEXT,
    artifact_dir TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    error TEXT
);
```

- [ ] **Step 4: Run repository tests**

Run:

```powershell
python -m pytest tests/test_repository.py -v
```

Expected: all repository tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/repository.py tests/test_repository.py
git commit -m "feat: persist jobs and atomic transitions"
```

---

### Task 4: CLI for Task Creation and Confirmation

**Files:**
- Create: `src/weibo_publisher/__main__.py`
- Create: `src/weibo_publisher/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config`, `JobRepository`, `build_fingerprint`, `validate_images`, `parse_publish_time`.
- Produces:
  - `main(argv: list[str] | None = None) -> int`
  - Subcommands: `add`, `show`, `confirm`, `list`, `cancel`, `status`.

- [ ] **Step 1: Write failing CLI tests**

```python
from pathlib import Path

import pytest

from weibo_publisher.cli import main


def test_add_creates_draft_and_confirm_approves(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)

    add_code = main(
        [
            "add",
            "--text",
            "可爱",
            "--image",
            str(image),
            "--at",
            "2026-11-07T17:30:00+08:00",
        ]
    )
    assert add_code == 0

    from weibo_publisher.config import load_config
    from weibo_publisher.repository import JobRepository

    config = load_config(root=tmp_path)
    repo = JobRepository(config.db_path)
    draft = repo.list_jobs()[0]
    assert draft.fingerprint == ""

    confirm_code = main(["confirm", draft.id])
    assert confirm_code == 0
    approved = repo.get_job(draft.id)
    assert approved is not None
    assert approved.fingerprint


def test_confirm_rejects_missing_image(tmp_path: Path, monkeypatch, capsys) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.chdir(tmp_path)
    main(
        [
            "add",
            "--text",
            "可爱",
            "--image",
            str(image),
            "--at",
            "2026-11-07T17:30:00+08:00",
        ]
    )
    image.unlink()

    from weibo_publisher.config import load_config
    from weibo_publisher.repository import JobRepository

    config = load_config(root=tmp_path)
    draft = JobRepository(config.db_path).list_jobs()[0]
    code = main(["confirm", draft.id])

    assert code == 2
    assert "Image does not exist" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_cli.py -v
```

Expected: FAIL because `cli.main` does not exist.

- [ ] **Step 3: Implement the CLI**

Use `argparse` with subparsers. `add` creates `JobStatus.DRAFT`, stores image
paths, and prints:

```text
Created draft <id>
Text: 可爱
Time: 2026-11-07T17:30:00+08:00
Images: 1
Confirm with: weibo-publisher confirm <id>
```

`confirm` performs:

1. Load the draft.
2. Validate text and images.
3. Validate that `publish_at_utc >= now + prepare_lead_seconds`.
4. Enforce daily limit unless `allow_over_limit=True`.
5. Enforce `minimum_gap_seconds` against approved or later jobs.
6. Build the fingerprint.
7. Transition `draft -> approved`.

`cancel` only permits `draft` or `approved`.

`list` prints a fixed-width table with ID, status, local publish time, text
preview, and result URL.

`show` prints all editable and runtime fields except private session data.

`status` prints the latest attempts and artifact path.

Create `src/weibo_publisher/__main__.py`:

```python
from weibo_publisher.cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Run CLI tests**

Run:

```powershell
python -m pytest tests/test_cli.py -v
```

Expected: all CLI tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/cli.py src/weibo_publisher/__main__.py tests/test_cli.py
git commit -m "feat: add task CLI and confirmation flow"
```

---

### Task 5: Playwright Browser Login and Session Doctor

**Files:**
- Create: `src/weibo_publisher/browser.py`
- Create: `src/weibo_publisher/logging_setup.py`
- Modify: `src/weibo_publisher/cli.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `AppConfig`.
- Produces:
  - `async def login(config: AppConfig) -> bool`
  - `async def check_session(config: AppConfig) -> bool`
  - `persistent_context(config: AppConfig)` async context manager
  - `configure_logging(config: AppConfig) -> None`

- [ ] **Step 1: Add a session-independent browser test**

Add to `tests/test_config.py`:

```python
def test_runtime_directories_are_created(tmp_path: Path) -> None:
    from weibo_publisher.config import ensure_runtime_dirs

    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)

    assert config.runtime_dir.is_dir()
    assert config.artifacts_dir.is_dir()
    assert config.logs_dir.is_dir()
```

Expected first failure: `ensure_runtime_dirs` is missing.

- [ ] **Step 2: Implement runtime directories and logging**

Modify `config.py`:

```python
def ensure_runtime_dirs(config: AppConfig) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
```

Create `logging_setup.py`:

```python
import logging
from logging.handlers import RotatingFileHandler


def configure_logging(config) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        config.logs_dir / "publisher.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
```

- [ ] **Step 3: Implement Playwright browser session**

Create `browser.py`:

```python
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio

from playwright.async_api import async_playwright

LOGIN_URL = "https://passport.weibo.com/sso/signin"
HOME_URL = "https://weibo.com/"


@asynccontextmanager
async def persistent_context(config):
    async with async_playwright() as playwright:
        launch_options = {
            "user_data_dir": str(config.profile_dir),
            "headless": config.headless,
            "viewport": {"width": 1365, "height": 900},
            "locale": "zh-CN",
            "timezone_id": config.default_timezone,
        }
        if config.browser_channel:
            launch_options["channel"] = config.browser_channel
        context = await playwright.chromium.launch_persistent_context(**launch_options)
        try:
            yield context
        finally:
            await context.close()


async def login(config) -> bool:
    async with persistent_context(config) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        for _ in range(150):
            if "passport" not in page.url.lower():
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30_000)
                return await _logged_in(page)
            await asyncio.sleep(2)
    return False


async def check_session(config) -> bool:
    async with persistent_context(config) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30_000)
        return await _logged_in(page)


async def _logged_in(page) -> bool:
    if any(word in page.url.lower() for word in ("passport", "login")):
        return False
    selectors = [
        '[class*="gn_name"]',
        '[class*="username"]',
        'a[href*="/u/"]',
    ]
    for selector in selectors:
        if await page.locator(selector).count():
            return True
    return False
```

Wire `login` and `doctor` in `cli.py` using `asyncio.run(...)`.

- [ ] **Step 4: Run tests and manual login smoke test**

Run:

```powershell
python -m playwright install chromium
python -m pytest tests/test_config.py -v
python -m weibo_publisher doctor
```

Expected: Chromium installs, tests pass, and `doctor` reports the session as
missing until the user completes `python -m weibo_publisher login`.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/browser.py src/weibo_publisher/logging_setup.py src/weibo_publisher/config.py src/weibo_publisher/cli.py tests/test_config.py
git commit -m "feat: add persistent browser login and doctor"
```

---

### Task 6: Weibo Preview Adapter

**Files:**
- Create: `src/weibo_publisher/publisher.py`
- Create: `src/weibo_publisher/weibo.py`
- Modify: `src/weibo_publisher/cli.py`
- Test: `tests/test_weibo_selectors.py`

**Interfaces:**
- Consumes: `Job`, `AppConfig`, `persistent_context`.
- Produces:
  - `Publisher` protocol with `prepare(job, artifact_dir)`, `submit(job, artifact_dir)`, `close()`
  - `WeiboPublisher(config)`
  - `async def preview_job(config, job, artifact_dir) -> None`
  - selector lists `TEXT_SELECTORS`, `IMAGE_INPUT_SELECTORS`, `PUBLISH_SELECTORS`

- [ ] **Step 1: Write selector configuration tests**

```python
from weibo_publisher.weibo import (
    IMAGE_INPUT_SELECTORS,
    PUBLISH_SELECTORS,
    TEXT_SELECTORS,
)


def test_text_selectors_include_contenteditable() -> None:
    assert any("contenteditable" in selector for selector in TEXT_SELECTORS)


def test_image_selectors_target_file_input() -> None:
    assert any('type="file"' in selector for selector in IMAGE_INPUT_SELECTORS)


def test_publish_selectors_include_text_fallback() -> None:
    assert any("发布" in selector for selector in PUBLISH_SELECTORS)
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_weibo_selectors.py -v
```

Expected: FAIL because `weibo.py` does not exist.

- [ ] **Step 3: Implement the preview adapter**

Create `publisher.py`:

```python
from pathlib import Path
from typing import Protocol

from weibo_publisher.models import Job, PublishResult


class Publisher(Protocol):
    async def prepare(self, job: Job, artifact_dir: Path) -> None: ...

    async def submit(self, job: Job, artifact_dir: Path) -> PublishResult: ...

    async def close(self) -> None: ...
```

Create `weibo.py` with these initial selector arrays:

```python
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
    'a:has-text("发布")',
    '[class*="submit"]:has-text("发布")',
]
```

`preview_job` behavior:

1. Open `https://weibo.com/`.
2. Assert session valid.
3. Find the first visible text selector.
4. For a textarea, call `fill(job.text)`.
5. For a contenteditable, click then `page.keyboard.insert_text(job.text)`.
6. For each image, locate the first file input and call `set_input_files`.
7. Wait until upload indicators disappear or five seconds pass.
8. Screenshot to `artifact_dir / "preview.png"`.
9. Do not click any publish control.

Add a helper:

```python
async def first_visible(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() and await locator.is_visible():
            return locator
    raise RuntimeError("No matching Weibo control found")
```

Wire `preview JOB_ID` in `cli.py`.

- [ ] **Step 4: Run tests and manual preview**

Run:

```powershell
python -m pytest tests/test_weibo_selectors.py -v
python -m weibo_publisher preview <JOB_ID>
```

Expected: tests pass. Manual preview opens Weibo, fills the confirmed content,
uploads images, saves a screenshot, and does not publish.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/publisher.py src/weibo_publisher/weibo.py src/weibo_publisher/cli.py tests/test_weibo_selectors.py
git commit -m "feat: add safe Weibo preview workflow"
```

---

### Task 7: Runner State Machine with Fake Publisher

**Files:**
- Create: `src/weibo_publisher/scheduler.py`
- Modify: `src/weibo_publisher/repository.py`
- Modify: `src/weibo_publisher/cli.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Publisher`, `JobRepository`, `AppConfig`.
- Produces:
  - `JobExecutor(config, repo, publisher_factory)`
  - `async def execute(job_id: str) -> None`
  - `Runner(config, repo, publisher_factory)`
  - `async def run_forever() -> None`

- [ ] **Step 1: Write failing state-machine tests**

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from weibo_publisher.models import (
    AttemptOutcome,
    Job,
    JobStatus,
    PublishResult,
    Visibility,
)
from weibo_publisher.repository import JobRepository
from weibo_publisher.scheduler import JobExecutor


class FakePublisher:
    def __init__(self, result: PublishResult):
        self.result = result
        self.prepared = False
        self.submitted = False

    async def prepare(self, job, artifact_dir):
        self.prepared = True

    async def submit(self, job, artifact_dir):
        self.submitted = True
        return self.result

    async def close(self):
        pass


def approved_job(seconds_from_now: int = 1) -> Job:
    target = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return Job(
        id="job-1",
        text="可爱",
        images=[],
        publish_at_utc=target,
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.APPROVED,
        fingerprint="fingerprint",
        created_at=datetime.now(timezone.utc),
        confirmed_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_successful_execution_becomes_published(
    app_config, tmp_path: Path
) -> None:
    repo = JobRepository(app_config.db_path)
    repo.initialize()
    repo.create_job(approved_job())
    fake = FakePublisher(
        PublishResult(
            outcome=AttemptOutcome.SUCCESS,
            weibo_mid="123",
            weibo_url="https://weibo.com/detail/123",
        )
    )

    await JobExecutor(app_config, repo, lambda: fake).execute("job-1")

    job = repo.get_job("job-1")
    assert job.status is JobStatus.PUBLISHED
    assert job.weibo_mid == "123"
    assert fake.prepared is True
    assert fake.submitted is True


@pytest.mark.asyncio
async def test_unknown_result_is_not_retried(app_config) -> None:
    repo = JobRepository(app_config.db_path)
    repo.initialize()
    repo.create_job(approved_job())
    fake = FakePublisher(
        PublishResult(outcome=AttemptOutcome.UNKNOWN, error="timeout")
    )

    await JobExecutor(app_config, repo, lambda: fake).execute("job-1")
    await JobExecutor(app_config, repo, lambda: fake).execute("job-1")

    job = repo.get_job("job-1")
    assert job.status is JobStatus.UNKNOWN
    assert fake.submitted is True
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_scheduler.py -v
```

Expected: FAIL because `JobExecutor` does not exist.

- [ ] **Step 3: Implement `JobExecutor`**

The executor must:

1. Reject jobs not in `approved`.
2. Transition `approved -> preparing`.
3. Check that `now <= publish_at_utc + late_cutoff`.
4. Validate fingerprint and image hashes again.
5. Create artifact directory.
6. Call `publisher.prepare`.
7. Transition `preparing -> ready`.
8. Sleep until `publish_at_utc`.
9. Transition `ready -> publishing`.
10. Call `publisher.submit`.
11. Record `published`, `failed`, or `unknown`.
12. Always call `publisher.close`.

Core code:

```python
async def execute(self, job_id: str) -> None:
    job = self.repo.get_job(job_id)
    if job is None:
        return
    now = datetime.now(timezone.utc)
    if now > job.publish_at_utc + timedelta(seconds=self.config.late_cutoff_seconds):
        self.repo.transition(job_id, {JobStatus.APPROVED}, JobStatus.MISSED)
        return
    started_at = datetime.now(timezone.utc)
    if not self.repo.transition(
        job_id,
        {JobStatus.APPROVED},
        JobStatus.PREPARING,
        last_attempt_at=started_at,
    ):
        return

    publisher = self.publisher_factory()
    attempt_id = uuid4().hex
    artifact_dir = self.config.artifacts_dir / job_id / attempt_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    self.repo.record_attempt(job_id, attempt_id)
    submit_started = False
    try:
        await publisher.prepare(job, artifact_dir)
        self.repo.transition(
            job_id,
            {JobStatus.PREPARING},
            JobStatus.READY,
            artifact_dir=str(artifact_dir),
        )
        delay = (job.publish_at_utc - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        if not self.repo.transition(job_id, {JobStatus.READY}, JobStatus.PUBLISHING):
            self.repo.finish_attempt(
                attempt_id,
                AttemptOutcome.INTERRUPTED.value,
                "Job was not ready to publish",
            )
            return
        submit_started = True
        result = await publisher.submit(job, artifact_dir)
    except Exception as exc:
        if submit_started:
            self.repo.transition(
                job_id,
                {JobStatus.PUBLISHING},
                JobStatus.UNKNOWN,
                last_error=str(exc),
            )
            self.repo.finish_attempt(
                attempt_id,
                AttemptOutcome.UNKNOWN.value,
                str(exc),
            )
        else:
            self.repo.transition(
                job_id,
                {JobStatus.PREPARING, JobStatus.READY},
                JobStatus.FAILED,
                last_error=str(exc),
            )
            self.repo.finish_attempt(
                attempt_id,
                AttemptOutcome.FAILED.value,
                str(exc),
            )
        return
    finally:
        await publisher.close()

    if result.outcome is AttemptOutcome.SUCCESS:
        self.repo.transition(
            job_id,
            {JobStatus.PUBLISHING},
            JobStatus.PUBLISHED,
            published_at=datetime.now(timezone.utc).isoformat(),
            weibo_mid=result.weibo_mid,
            weibo_url=result.weibo_url,
        )
        self.repo.finish_attempt(
            attempt_id,
            AttemptOutcome.SUCCESS.value,
            None,
        )
    elif result.outcome is AttemptOutcome.UNKNOWN:
        self.repo.transition(
            job_id,
            {JobStatus.PUBLISHING},
            JobStatus.UNKNOWN,
            last_error=result.error,
        )
        self.repo.finish_attempt(
            attempt_id,
            AttemptOutcome.UNKNOWN.value,
            result.error,
        )
    else:
        self.repo.transition(
            job_id,
            {JobStatus.PUBLISHING},
            JobStatus.FAILED,
            last_error=result.error,
        )
        self.repo.finish_attempt(
            attempt_id,
            AttemptOutcome.FAILED.value,
            result.error,
        )
```

- [ ] **Step 4: Run scheduler tests**

Run:

```powershell
python -m pytest tests/test_scheduler.py -v
```

Expected: success and unknown-outcome tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/scheduler.py src/weibo_publisher/repository.py src/weibo_publisher/cli.py tests/test_scheduler.py
git commit -m "feat: execute jobs with idempotent state transitions"
```

---

### Task 8: APScheduler Scanner and Runner Lock

**Files:**
- Modify: `src/weibo_publisher/scheduler.py`
- Modify: `src/weibo_publisher/cli.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `JobExecutor`, `JobRepository`.
- Produces:
  - `Runner.run_forever()`
  - `Runner.scan_once()`
  - runner lock at `runtime/runner.lock`

- [ ] **Step 1: Write failing runner tests**

```python
@pytest.mark.asyncio
async def test_scan_ignores_draft_jobs(app_config) -> None:
    repo = JobRepository(app_config.db_path)
    repo.initialize()
    job = approved_job()
    job.status = JobStatus.DRAFT
    repo.create_job(job)
    calls = []

    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(PublishResult(outcome=AttemptOutcome.SUCCESS)),
        executor_factory=lambda job_id: calls.append(job_id),
    )
    await runner.scan_once()
    assert calls == []


@pytest.mark.asyncio
async def test_scan_schedules_approved_job_once(app_config) -> None:
    repo = JobRepository(app_config.db_path)
    repo.initialize()
    repo.create_job(approved_job(seconds_from_now=10))

    runner = Runner(
        app_config,
        repo,
        lambda: FakePublisher(PublishResult(outcome=AttemptOutcome.SUCCESS)),
        executor_factory=lambda job_id: None,
    )
    await runner.scan_once()
    await runner.scan_once()
    assert runner.scheduled == {"job-1"}
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_scheduler.py -v
```

Expected: FAIL because `Runner` does not support injected executor or scan.

- [ ] **Step 3: Implement the scanner and runner lock**

Implementation behavior:

- `AsyncIOScheduler(timezone="Asia/Shanghai")`
- Scan every five seconds.
- Keep `self._scheduled_job_ids`.
- For each approved job:
  - If already scheduled, skip.
  - If `publish_at - now > prepare_lead`, schedule preparation at
    `publish_at - prepare_lead`.
  - If already inside the preparation window but not late, schedule
    preparation five seconds from now.
  - If late, transition to `missed`.
- Remove cancelled or non-approved jobs from the scheduler.
- Use a file lock opened with `os.open(..., os.O_CREAT | os.O_EXCL)`.
- On startup, convert any `publishing` job from a prior process to `unknown`.
- On startup, convert expired `preparing` or `ready` jobs to
  `needs_attention`.

Runner skeleton:

```python
import inspect


class Runner:
    def __init__(
        self,
        config,
        repo,
        publisher_factory,
        executor_factory=None,
    ):
        self.config = config
        self.repo = repo
        self.publisher_factory = publisher_factory
        self.executor_factory = executor_factory
        self.scheduler = AsyncIOScheduler(timezone=config.default_timezone)
        self.scheduled: set[str] = set()

    async def scan_once(self) -> None:
        for job in self.repo.list_jobs(JobStatus.APPROVED):
            if job.id in self.scheduled:
                continue
            now = datetime.now(timezone.utc)
            if now > job.publish_at_utc + timedelta(
                seconds=self.config.late_cutoff_seconds
            ):
                self.repo.transition(
                    job.id,
                    {JobStatus.APPROVED},
                    JobStatus.MISSED,
                )
                continue
            run_at = max(
                now + timedelta(seconds=5),
                job.publish_at_utc
                - timedelta(seconds=self.config.prepare_lead_seconds),
            )
            execute = self._executor(job.id)
            self.scheduler.add_job(
                execute,
                "date",
                run_date=run_at,
                id=f"job:{job.id}",
                replace_existing=True,
            )
            self.scheduled.add(job.id)

    def _executor(self, job_id: str):
        if self.executor_factory:
            async def injected():
                callback = self.executor_factory(job_id)
                if inspect.isawaitable(callback):
                    await callback
            return injected
        executor = JobExecutor(self.config, self.repo, self.publisher_factory)
        return lambda: executor.execute(job_id)
```

Startup recovery code:

```python
async def recover_interrupted(self) -> None:
    for job in self.repo.list_jobs(JobStatus.PUBLISHING):
        self.repo.transition(
            job.id,
            {JobStatus.PUBLISHING},
            JobStatus.UNKNOWN,
            last_error="Runner restarted during submission",
        )
    now = datetime.now(timezone.utc)
    for status in (JobStatus.PREPARING, JobStatus.READY):
        for job in self.repo.list_jobs(status):
            if now > job.publish_at_utc + timedelta(
                seconds=self.config.late_cutoff_seconds
            ):
                self.repo.transition(
                    job.id,
                    {status},
                    JobStatus.NEEDS_ATTENTION,
                    last_error="Runner restarted after the preparation window",
                )
```

- [ ] **Step 4: Run runner tests**

Run:

```powershell
python -m pytest tests/test_scheduler.py -v
```

Expected: all scheduler scanner tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/scheduler.py src/weibo_publisher/cli.py tests/test_scheduler.py
git commit -m "feat: add rescanning scheduler and runner lock"
```

---

### Task 9: Real Weibo Submit and Success Detection

**Files:**
- Modify: `src/weibo_publisher/weibo.py`
- Modify: `src/weibo_publisher/publisher.py`
- Test: `tests/test_weibo_response.py`

**Interfaces:**
- Consumes: `Job`, `PublishResult`.
- Produces:
  - `WeiboPublisher.prepare(job, artifact_dir) -> None`
  - `WeiboPublisher.submit(job, artifact_dir) -> PublishResult`
  - `parse_publish_response(payload: dict) -> PublishResult`

- [ ] **Step 1: Write failing response parsing tests**

```python
from weibo_publisher.models import AttemptOutcome
from weibo_publisher.weibo import parse_publish_response


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
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_weibo_response.py -v
```

Expected: FAIL because `parse_publish_response` does not exist.

- [ ] **Step 3: Implement submit and response parsing**

Use `expect_response` around one and only one click:

```python
async def submit(self, job, artifact_dir):
    button = await first_visible(self.page, PUBLISH_SELECTORS)
    try:
        async with self.page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and "/ajax/statuses/update" in response.url
            ),
            timeout=30_000,
        ) as response_info:
            await button.click()
        response = await response_info.value
        payload = await response.json()
        result = parse_publish_response(payload)
    except Exception as exc:
        await self.page.screenshot(path=artifact_dir / "submit-unknown.png")
        return PublishResult(
            outcome=AttemptOutcome.UNKNOWN,
            error=f"Submit outcome unknown: {exc}",
        )

    await self.page.screenshot(path=artifact_dir / "submit-result.png")
    return result
```

Implement:

```python
def parse_publish_response(payload: dict) -> PublishResult:
    if str(payload.get("code")) == "100000":
        data = payload.get("data") or {}
        mid = str(data.get("idstr") or data.get("mid") or "")
        if not mid:
            return PublishResult(
                outcome=AttemptOutcome.UNKNOWN,
                error="Success code without post ID",
            )
        return PublishResult(
            outcome=AttemptOutcome.SUCCESS,
            weibo_mid=mid,
            weibo_url=f"https://weibo.com/detail/{mid}",
        )
    return PublishResult(
        outcome=AttemptOutcome.FAILED,
        error=str(payload.get("msg") or payload.get("message") or payload),
    )
```

On submit exception after the click, do not inspect and retry. Return
`UNKNOWN`. The user must check the account manually.

- [ ] **Step 4: Run response parsing tests**

Run:

```powershell
python -m pytest tests/test_weibo_response.py -v
```

Expected: both response tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/weibo_publisher/weibo.py src/weibo_publisher/publisher.py tests/test_weibo_response.py
git commit -m "feat: detect Weibo publish results safely"
```

---

### Task 10: Local End-to-End Publish Test

**Files:**
- Create: `tests/fixtures/mock_publish.html`
- Create: `tests/test_local_publish_flow.py`
- Modify: `src/weibo_publisher/weibo.py`

**Interfaces:**
- Consumes: `JobExecutor`, local mock publisher.
- Produces: an end-to-end test that validates preparation, waiting, submit, and success recording without contacting Weibo.

- [ ] **Step 1: Create the mock page test**

Create `tests/fixtures/mock_publish.html` with:

```html
<!doctype html>
<html lang="zh-CN">
  <body>
    <textarea id="text"></textarea>
    <input id="image" type="file" accept="image/*">
    <button id="publish">发布</button>
    <script>
      document.querySelector("#publish").addEventListener("click", async () => {
        document.body.dataset.published = "true";
      });
    </script>
  </body>
</html>
```

Create `tests/test_local_publish_flow.py` with a real Playwright
`LocalFilePublisher`. It must not use the configured Weibo profile.

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from weibo_publisher.models import (
    AttemptOutcome,
    Job,
    JobStatus,
    PublishResult,
    Visibility,
)
from weibo_publisher.repository import JobRepository
from weibo_publisher.scheduler import JobExecutor


class LocalFilePublisher:
    def __init__(self, page_url: str):
        self.page_url = page_url
        self.playwright = None
        self.browser = None
        self.page = None
        self.published = False

    async def prepare(self, job, artifact_dir):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self.page.goto(self.page_url)
        await self.page.fill("#text", job.text)
        if job.images:
            await self.page.set_input_files(
                "#image",
                [str(path) for path in job.images],
            )

    async def submit(self, job, artifact_dir):
        await self.page.click("#publish")
        await self.page.wait_for_function(
            "document.body.dataset.published === 'true'"
        )
        self.published = True
        return PublishResult(
            outcome=AttemptOutcome.SUCCESS,
            weibo_mid="mock-123",
            weibo_url="https://example.test/detail/mock-123",
        )

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


def make_approved_job(tmp_path: Path) -> Job:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"jpeg")
    return Job(
        id="job-local-1",
        text="可爱",
        images=[image],
        publish_at_utc=datetime.now(timezone.utc) + timedelta(seconds=1),
        timezone="Asia/Shanghai",
        visibility=Visibility.PUBLIC,
        status=JobStatus.APPROVED,
        fingerprint="local",
        created_at=datetime.now(timezone.utc),
        confirmed_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_prepare_does_not_publish(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "mock_publish.html"
    publisher = LocalFilePublisher(fixture.as_uri())
    job = make_approved_job(tmp_path)

    await publisher.prepare(job, tmp_path)

    assert await publisher.page.input_value("#text") == "可爱"
    assert await publisher.page.evaluate(
        "document.body.dataset.published || ''"
    ) == ""
    assert publisher.published is False
    await publisher.close()
```

Add the full executor test in the same file:

```python
@pytest.mark.asyncio
async def test_end_to_end_publish_result(app_config, tmp_path):
    repo = JobRepository(app_config.db_path)
    repo.initialize()
    job = make_approved_job(tmp_path)
    repo.create_job(job)

    publisher = LocalFilePublisher(
        (Path(__file__).parent / "fixtures" / "mock_publish.html").as_uri()
    )
    await JobExecutor(
        app_config,
        repo,
        lambda: publisher,
    ).execute(job.id)

    stored = repo.get_job(job.id)
    assert stored.status is JobStatus.PUBLISHED
    assert stored.weibo_mid == "mock-123"
    assert publisher.published is True
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
python -m pytest tests/test_local_publish_flow.py -v
```

Expected: FAIL because the fixture and test helper do not exist.

- [ ] **Step 3: Implement the mock and end-to-end assertion**

The test uses a temporary Chromium browser and a `file://` fixture. No Weibo
profile or network endpoint is loaded.

- [ ] **Step 4: Run the full suite**

Run:

```powershell
python -m pytest -v
python -m ruff check .
```

Expected: all tests and lint checks pass.

- [ ] **Step 5: Commit**

```powershell
git add tests/fixtures/mock_publish.html tests/test_local_publish_flow.py src/weibo_publisher/weibo.py
git commit -m "test: cover local publish flow"
```

---

### Task 11: Documentation, License, and GitHub Readiness

**Files:**
- Modify: `README.md`
- Create: `SECURITY.md`
- Create: `LICENSE`
- Modify: `.gitignore`
- Test: manual repository audit

**Interfaces:**
- Consumes: all implemented commands.
- Produces: a repository safe to publish under MIT.

- [ ] **Step 1: Add MIT license**

Create `LICENSE` with the standard MIT text and:

```text
Copyright (c) 2026 Weibo Publisher contributors
```

- [ ] **Step 2: Write README**

README must include:

````markdown
# Weibo Publisher

A local, single-account scheduled Weibo publisher for Windows.

## What it does

- Logs in once through a dedicated Playwright browser profile
- Stores confirmed text-and-image jobs in SQLite
- Prepares a post three minutes before its scheduled time
- Publishes unattended at the confirmed time
- Records success, failure, and uncertain outcomes

## Requirements

- Windows
- Python 3.11+
- Microsoft Edge or Playwright Chromium
- The computer must be powered on and the runner active at publish time

## Install

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

## First login

```powershell
python -m weibo_publisher login
```

Scan the QR code in the opened browser.

## Create and confirm a job

```powershell
python -m weibo_publisher add --text "可爱" --image "D:\path\image.jpg" --at "2026-11-07T17:30:00+08:00"
python -m weibo_publisher show JOB_ID
python -m weibo_publisher confirm JOB_ID
```

## Run the scheduler

```powershell
python -m weibo_publisher run
```

## Safety

This project does not guarantee that Weibo will accept a given posting volume.
It does not solve captcha, bypass verification, or retry ambiguous submissions.

## Privacy

Never commit `runtime/`, `config.toml`, browser profile data, logs, database
files, or screenshots.
````

- [ ] **Step 3: Write SECURITY.md**

Include:

```markdown
# Security Policy

## Sensitive data

The Playwright profile contains live Weibo session data. Never share or commit
`runtime/profile`, `runtime/state.db`, `runtime/logs`, or
`runtime/artifacts`.

## Reporting

Open a private security advisory instead of publishing credentials or session
data in a public issue.

## Supported behavior

This project does not support captcha bypass, credential extraction, proxy
rotation, or disguised multi-account operation.
```

- [ ] **Step 4: Audit the repository**

Run:

```powershell
git status --short
git ls-files
rg -n -i "cookie|password|token|secret" .
```

Expected:

- No `runtime/` files are tracked.
- No `config.toml` is tracked.
- Search results contain only documentation and code variable names, never
  actual credentials.

- [ ] **Step 5: Commit**

```powershell
git add README.md SECURITY.md LICENSE .gitignore
git commit -m "docs: prepare project for open-source release"
```

---

### Task 12: Manual Weibo Verification

**Files:**
- No new source files required.
- Modify: `README.md` only if observed selectors require updated instructions.

**Interfaces:**
- Consumes: completed CLI, browser, scheduler, and Weibo adapter.
- Produces: verified end-to-end behavior for the user's account.

- [ ] **Step 1: Create and activate the local environment**

Run:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Expected: installation succeeds.

- [ ] **Step 2: Complete QR login and doctor**

Run:

```powershell
python -m weibo_publisher login
python -m weibo_publisher doctor
```

Expected: login succeeds and `doctor` reports the session as valid.

- [ ] **Step 3: Preview a draft**

Run:

```powershell
python -m weibo_publisher add --text "测试预览，不发布" --at "2026-11-07T17:30:00+08:00"
python -m weibo_publisher preview <JOB_ID>
```

Expected: the composer is filled and a screenshot is saved. No post appears.

- [ ] **Step 4: Run one explicitly approved real post**

The user must approve the exact text, image, date, time, and visibility before
this step. Then run:

```powershell
python -m weibo_publisher confirm <JOB_ID>
python -m weibo_publisher run
```

Expected:

- exactly one post is created
- the job becomes `published`
- a result URL is stored
- no duplicate post exists
- failure produces an artifact and a non-published state

- [ ] **Step 5: Record findings and commit**

Update README with any selector or login observations, then:

```powershell
git add README.md
git commit -m "docs: record verified Weibo workflow"
```

---

## Plan Self-Review

### Spec Coverage

- Single-account CLI: Tasks 1, 4, and 8.
- Dedicated Playwright profile and QR login: Task 5.
- Text and image preview: Task 6.
- Real publication and result detection: Task 9.
- One-time confirmation and fingerprint: Tasks 2 and 4.
- State machine and unknown handling: Tasks 2 and 7.
- 100/day limit and 240-second gap: Tasks 1, 2, and 4.
- Scheduling three minutes before and 30-second cutoff: Tasks 7 and 8.
- Idempotency and runner lock: Tasks 3, 7, and 8.
- Logs, screenshots, and artifacts: Tasks 5, 6, 7, and 9.
- Testing without real Weibo: Tasks 2, 3, 7, 8, and 10.
- Open-source readiness: Tasks 1 and 11.
- Manual real-post verification: Task 12.

### Placeholder Scan

No `TBD`, `TODO`, deferred implementation, or unspecified code steps remain.

### Type Consistency

- `JobStatus` names match repository transitions and CLI statuses.
- `PublishResult` is used consistently by fake, local, and real publishers.
- `prepare(job, artifact_dir)` and `submit(job, artifact_dir)` signatures match
  the protocol and all implementations.
- `daily_limit`, `minimum_gap_seconds`, `prepare_lead_seconds`, and
  `late_cutoff_seconds` retain their spec names.
