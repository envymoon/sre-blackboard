"""A real worker process running investigations with the scripted model and recorded tools.

    python -m tests.helpers.agent_worker            # from the repository root

AGENT_WORKER_HANG=<agent> makes that agent's model call hang, so a test can kill the
process in the middle of an investigation.
"""

from __future__ import annotations

import asyncio
import logging
import os

from app.core.config import get_settings
from app.llm.types import LLMRequest, LLMResult
from app.tools.sources import ReplaySource
from app.worker.__main__ import serve
from app.worker.investigate import AgentServices
from tests.helpers.payment_scripts import payment_cassette, payment_scripts
from tests.helpers.scripted_llm import ScriptedLLM


class HangingLLM(ScriptedLLM):
    def __init__(self, hang: str | None) -> None:
        super().__init__(payment_scripts())
        self.hang = hang

    async def step(self, request: LLMRequest) -> LLMResult:
        if request.agent == self.hang:
            logging.info("hanging in %s", request.agent)
            await asyncio.sleep(3600)
        return await super().step(request)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    services = AgentServices(HangingLLM(os.environ.get("AGENT_WORKER_HANG")), ReplaySource(payment_cassette()))
    asyncio.run(serve(get_settings(), services))
