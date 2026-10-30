"""``python -m app.executor``: run the executor until SIGTERM or SIGINT.

A worker that claims only ``execute`` jobs, with the executor's own Kubernetes identity
(SRE_EXECUTOR_K8S_TOKEN). It refuses to start without one. It has no model and no tools.
"""

from __future__ import annotations

import asyncio
import signal

from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.engine import make_engine
from app.obs import metrics, tracing
from app.worker.runner import Worker, default_worker_id

from .job import make_execute
from .k8s import DeploymentWriter


async def serve(settings: Settings) -> None:
    writer = DeploymentWriter.from_settings(settings)
    worker_id = f"executor-{default_worker_id()}"
    flush_traces = tracing.setup("sre-blackboard-executor", settings, instance=worker_id)
    metrics.serve(settings.metrics_port)
    engine = make_engine(settings)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await Worker(engine, settings, {"execute": make_execute(writer)}, worker_id=worker_id).run(stop)
    finally:
        await writer.aclose()
        await engine.dispose()
        flush_traces()


def main() -> None:
    tracing.configure_logging()
    asyncio.run(serve(get_settings()))


if __name__ == "__main__":
    main()
