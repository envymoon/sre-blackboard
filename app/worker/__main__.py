"""``python -m app.worker``: run one worker process until SIGTERM or SIGINT."""

from __future__ import annotations

import asyncio
import signal

from sqlalchemy import text

from app.agents import variants
from app.core.config import Settings, get_settings
from app.db.engine import make_engine
from app.obs import metrics, tracing

from .handlers import default_handlers
from .investigate import AgentServices
from .runner import Handler, Worker, default_worker_id


async def serve(
    settings: Settings, services: AgentServices | None = None, extra_handlers: dict[str, Handler] | None = None
) -> None:
    for name in settings.eval_tenant_variants.values():
        variants.get(name)  # an unknown variant stops the worker here, not each investigation later
    worker_id = default_worker_id()
    flush_traces = tracing.setup("sre-blackboard-worker", settings, instance=worker_id)
    metrics.serve(settings.metrics_port)
    engine = make_engine(settings)
    services = services or AgentServices.from_settings(settings)
    try:
        # Fail fast if the database is unreachable instead of logging claim errors forever.
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        handlers = {**default_handlers(services), **(extra_handlers or {})}
        await Worker(engine, settings, handlers, worker_id=worker_id).run(stop)
    finally:
        await services.aclose()
        await engine.dispose()
        flush_traces()


def main() -> None:
    tracing.configure_logging()
    asyncio.run(serve(get_settings()))


if __name__ == "__main__":
    main()
