from pathlib import Path
from typing import Protocol

from weibo_publisher.models import Job, PublishResult


class PublisherSafetyStop(RuntimeError):
    """The account needs human attention before any more posts are published."""


class Publisher(Protocol):
    async def prepare(self, job: Job, artifact_dir: Path) -> None: ...

    async def submit(self, job: Job, artifact_dir: Path) -> PublishResult: ...

    async def close(self) -> None: ...
