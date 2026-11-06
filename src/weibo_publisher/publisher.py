from pathlib import Path
from typing import Protocol

from weibo_publisher.models import Job, PublishResult


class Publisher(Protocol):
    async def prepare(self, job: Job, artifact_dir: Path) -> None: ...

    async def submit(self, job: Job, artifact_dir: Path) -> PublishResult: ...

    async def close(self) -> None: ...
