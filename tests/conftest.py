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
