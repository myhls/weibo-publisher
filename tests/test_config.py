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


def test_runtime_directories_are_created(tmp_path: Path) -> None:
    from weibo_publisher.config import ensure_runtime_dirs

    config = load_config(root=tmp_path)
    ensure_runtime_dirs(config)

    assert config.runtime_dir.is_dir()
    assert config.artifacts_dir.is_dir()
    assert config.logs_dir.is_dir()
