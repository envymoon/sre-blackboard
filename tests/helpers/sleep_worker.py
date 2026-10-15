"""A real worker process for tests: the default handlers plus a ``sleep`` job kind.

Run with ``python -m tests.helpers.sleep_worker`` from the repository root.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.worker.__main__ import serve
from app.worker.runner import JobContext


async def sleep(ctx: JobContext) -> None:
    await asyncio.sleep(float(ctx.job.payload.get("seconds", 1)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(get_settings(), extra_handlers={"sleep": sleep}))
