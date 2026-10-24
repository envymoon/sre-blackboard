"""``python -m app.executor``: run the executor until SIGTERM or SIGINT.

A worker that claims only ``execute`` jobs, with the executor's own Kubernetes identity
(SRE_EXECUTOR_K8S_TOKEN). It refuses to start without one. It has no model and no tools.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.engine import make_engine
from app.worker.runner import Worker, default_worker_id

from .job import make_execute
from .k8s import DeploymentWriter


async def serve(settings: Settings) -> None:
    writer = DeploymentWriter.from_settings(settings)
    engine = make_engine(settings)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        worker = Worker(
            engine, settings, {"execute": make_execute(writer)}, worker_id=f"executor-{default_worker_id()}"
        )
        await worker.run(stop)
    finally:
        await writer.aclose()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(get_settings()))


if __name__ == "__main__":
    main()
