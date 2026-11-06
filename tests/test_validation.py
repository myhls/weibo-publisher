from datetime import UTC, datetime
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
    publish_at = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)

    first = build_fingerprint("可爱", [image], publish_at, Visibility.PUBLIC)
    image.write_bytes(b"second")
    second = build_fingerprint("可爱", [image], publish_at, Visibility.PUBLIC)

    assert first != second


def test_parse_publish_time_requires_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_publish_time("2026-11-07T17:30:00", "Asia/Shanghai")


def test_parse_publish_time_converts_to_utc() -> None:
    result = parse_publish_time("2026-11-07T17:30:00+08:00", "Asia/Shanghai")
    assert result == datetime(2026, 11, 7, 9, 30, tzinfo=UTC)


def test_validate_images_rejects_unsupported_extension(tmp_path: Path) -> None:
    image = tmp_path / "note.txt"
    image.write_text("not an image", encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported image"):
        validate_images([image])
