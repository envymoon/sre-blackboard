"""LLM backends (M4): the Claude CLI on a subscription, the Anthropic API, and replay.

All three answer the same question, "next step for this transcript?", so the agent loop does
not know or care which one it talks to.
"""

from __future__ import annotations

from app.llm.types import LLMBackend, LLMError, LLMRequest, LLMResult, Usage

__all__ = ["LLMBackend", "LLMError", "LLMRequest", "LLMResult", "Usage"]
