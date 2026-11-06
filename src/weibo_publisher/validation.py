from __future__ import annotations

from datetime import UTC, datetime
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
    return parsed.astimezone(UTC)


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
    digest.update(publish_at_utc.astimezone(UTC).isoformat().encode("ascii"))
    digest.update(visibility.value.encode("ascii"))
    return digest.hexdigest()
