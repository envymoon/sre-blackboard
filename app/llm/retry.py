"""Retries for the errors that go away by themselves: rate limits, overload, timeouts, crashes."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from app.llm.types import LLMBackend, LLMError, LLMRequest, LLMResult

log = logging.getLogger(__name__)


class RetryingBackend:
    def __init__(
        self,
        inner: LLMBackend,
        *,
        retries: int = 3,
        base_s: float = 2.0,
        max_wait_s: float = 120.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.retries = retries
        self.base_s = base_s
        # A subscription's usage window can be hours away. Waiting that long inside a job would
        # hold its lease; past this cap the error goes up and the job queue retries later.
        self.max_wait_s = max_wait_s
        self.sleep = sleep

    async def step(self, request: LLMRequest) -> LLMResult:
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await self.inner.step(request)
            except LLMError as e:
                e.attempts = attempt
                if not e.retryable or attempt > self.retries:
                    raise
                wait = self.base_s * 2 ** (attempt - 1) * (1 + random.random() / 2)
                if e.retry_after_s is not None:
                    if e.retry_after_s > self.max_wait_s:
                        raise
                    wait = max(wait, e.retry_after_s)
                log.warning("%s: %s; retry %d in %.1fs", request.agent, e, attempt, wait)
                await self.sleep(min(wait, self.max_wait_s))
                continue
            if attempt > 1:
                return LLMResult(**{**result.__dict__, "attempts": attempt})
            return result
