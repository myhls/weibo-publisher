from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
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


def ensure_runtime_dirs(config: AppConfig) -> None:
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
