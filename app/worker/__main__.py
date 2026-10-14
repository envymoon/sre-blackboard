"""``python -m app.worker``: run one worker process until SIGTERM or SIGINT."""

from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.db.engine import make_engine

from .handlers import default_handlers
from .runner import Handler, Worker


async def serve(settings: Settings, handlers: dict[str, Handler]) -> None:
    engine = make_engine(settings)
    try:
        # Fail fast if the database is unreachable instead of logging claim errors forever.
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await Worker(engine, settings, handlers).run(stop)
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(get_settings(), default_handlers()))


if __name__ == "__main__":
    main()
