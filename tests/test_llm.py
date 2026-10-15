"""LLM layer: rendering, the CLI and API backends against fakes, retries, record/replay, routing."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.llm.api import FINAL_TOOL, AnthropicAPIBackend, render_messages
from app.llm.cli import ClaudeCLIBackend
from app.llm.render import inline_refs, step_schema, transcript_text
from app.llm.replay import LLMCassette, RecordingBackend, ReplayBackend
from app.llm.retry import RetryingBackend
from app.llm.router import model_for
from app.llm.types import LLMError, LLMRequest, LLMResult, Usage, normalize_step
from app.tools import ALL_TOOLS

FINAL = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
TASK = {"type": "user", "text": "Alert: ErrorBudgetBurn on checkout."}


def request(*turns: dict[str, Any], tools: bool = True, agent: str = "investigator.metrics") -> LLMRequest:
    return LLMRequest(
        agent=agent,
        model="claude-haiku-5-5",
        prompt_version="v1",
        system="You investigate.",
        turns=turns or (TASK,),
        tools=tuple(t.spec() for t in ALL_TOOLS[:2]) if tools else (),
        final_schema=FINAL,
    )


STEP = {
    "type": "step",
    "reasoning": "Check errors.",
    "actions": [{"id": "c1_1", "tool": "prom_query", "args": {"query": "up"}}],
    "final": None,
}
RESULTS = {"type": "results", "results": [{"id": "c1_1", "tool": "prom_query", "content": '{"evidence_id":"ev_1"}'}]}


# --- request, step shape, rendering ---


def test_request_hash_covers_what_the_model_sees_and_nothing_else() -> None:
    a, b = request(TASK), request(TASK)
    assert a.hash() == b.hash()
    assert request(TASK, STEP, RESULTS).hash() != a.hash()
    assert LLMRequest(**{**a.__dict__, "system": "other"}).hash() != a.hash()
    # The agent name and prompt version are part of the replay key, not of what the model sees.
    assert LLMRequest(**{**a.__dict__, "agent": "x", "prompt_version": "v9"}).hash() == a.hash()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"reasoning": "r", "actions": [{"tool": "t", "args": {"a": 1}}]}, ([{"tool": "t", "args": {"a": 1}}], None)),
        ({"reasoning": "r", "final": {"answer": "x"}}, ([], {"answer": "x"})),
        # More data first: a final next to actions waits for the next step.
        (
            {"reasoning": "r", "actions": [{"tool": "t", "args": {}}], "final": {"answer": "x"}},
            ([{"tool": "t", "args": {}}], None),
        ),
    ],
)
def test_normalize_step(raw: dict[str, Any], expected: tuple[Any, Any]) -> None:
    step = normalize_step(raw, request())
    assert (step["actions"], step["final"]) == expected


@pytest.mark.parametrize("raw", ["text", {"reasoning": "r"}, {"actions": [{"args": {}}]}, {"actions": "x"}])
def test_normalize_step_rejects_shapes_that_are_not_a_step(raw: Any) -> None:
    with pytest.raises(LLMError) as e:
        normalize_step(raw, request())
    assert e.value.code == "invalid_output" and e.value.retryable


def test_step_schema_puts_each_data_source_in_its_own_branch() -> None:
    schema = step_schema(request())
    branches = schema["properties"]["actions"]["items"]["anyOf"]
    assert [b["properties"]["tool"] for b in branches] == [{"const": "prom_query"}, {"const": "prom_range"}]
    assert branches[0]["properties"]["args"]["required"] == ["query"]
    assert schema["properties"]["actions"]["maxItems"] == 3
    assert schema["required"] == ["reasoning"]
    assert "$ref" not in json.dumps(schema)
    no_tools = step_schema(request(tools=False))
    assert "actions" not in no_tools["properties"] and no_tools["required"] == ["reasoning", "final"]


def test_inline_refs() -> None:
    schema = {
        "title": "X",
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}}},
        "$defs": {"Item": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    }
    assert inline_refs(schema) == {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"}}}}
        },
    }


def test_transcript_text_keeps_results_in_tags() -> None:
    text = transcript_text([TASK, STEP, RESULTS], "Go.")
    assert text.index("<message>") < text.index("<your_step n=1>") < text.index('<result id="c1_1" tool="prom_query">')
    assert text.endswith("Go.")


# --- CLI backend against a fake claude ---

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "prompt": prompt, "claudecode": os.environ.get("CLAUDECODE")}) + "\\n")
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "ok":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
        "structured_output": {"reasoning": "r", "actions": [{"tool": "prom_query", "args": {"query": "up"}}]},
        "usage": {"input_tokens": 3, "output_tokens": 40, "cache_read_input_tokens": 100,
                  "cache_creation_input_tokens": 900},
        "modelUsage": {"claude-haiku-5-5": {}}, "total_cost_usd": 0.0012}))
elif mode == "429":
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "Rate limited"}))
    sys.exit(1)
elif mode == "usage_limit":
    print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|%d" % (time.time() + 7200)}))
    sys.exit(1)
elif mode == "404":
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 404, "result": "no such model"}))
    sys.exit(1)
elif mode == "crash":
    print("Segmentation fault", file=sys.stderr)
    sys.exit(139)
elif mode == "prose":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "I cannot."}))
elif mode == "slow":
    time.sleep(30)
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("CLAUDECODE", "1")
    return str(binary), log


def _calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_cli_backend_runs_an_isolated_claude_and_reads_the_structured_step(
    fake_claude: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, log = fake_claude
    monkeypatch.setenv("FAKE_MODE", "ok")
    req = request(TASK, STEP, RESULTS)
    result = asyncio.run(ClaudeCLIBackend(binary=binary).step(req))
    assert result.step == {
        "reasoning": "r",
        "actions": [{"tool": "prom_query", "args": {"query": "up"}}],
        "final": None,
    }
    assert result.usage == Usage(3, 40, 100, 900) and result.usage.prompt_tokens == 1003
    assert (result.cost_usd, result.model, result.backend, result.request_hash) == (
        0.0012,
        "claude-haiku-5-5",
        "cli",
        req.hash(),
    )
    (call,) = _calls(log)
    args = call["args"]
    for flag, value in [
        ("--tools", ""),
        ("--setting-sources", ""),
        ("--output-format", "json"),
        ("--model", "claude-haiku-5-5"),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert json.loads(args[args.index("--json-schema") + 1]) == step_schema(req)
    assert '<result id="c1_1" tool="prom_query">' in call["prompt"]
    assert call["claudecode"] is None  # a nested claude must not think it runs inside Claude Code


@pytest.mark.parametrize(
    ("mode", "code", "retryable"),
    [
        ("429", "rate_limited", True),
        ("usage_limit", "rate_limited", True),
        ("404", "bad_request", False),
        ("crash", "unavailable", True),
        ("prose", "invalid_output", True),
    ],
)
def test_cli_backend_classifies_failures(
    fake_claude: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, mode: str, code: str, retryable: bool
) -> None:
    binary, _ = fake_claude
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(LLMError) as e:
        asyncio.run(ClaudeCLIBackend(binary=binary).step(request()))
    assert (e.value.code, e.value.retryable) == (code, retryable)
    if mode == "usage_limit":
        assert e.value.retry_after_s is not None and 7000 < e.value.retry_after_s <= 7200


def test_cli_backend_kills_a_hung_process(fake_claude: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    binary, _ = fake_claude
    monkeypatch.setenv("FAKE_MODE", "slow")
    with pytest.raises(LLMError) as e:
        asyncio.run(ClaudeCLIBackend(binary=binary, timeout_s=0.5).step(request()))
    assert e.value.code == "timeout"


def test_cli_backend_limits_concurrent_processes(
    fake_claude: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, _ = fake_claude
    monkeypatch.setenv("FAKE_MODE", "ok")
    backend = ClaudeCLIBackend(binary=binary, concurrency=2)
    peak = 0
    original = asyncio.create_subprocess_exec

    async def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal peak
        peak = max(peak, 2 - backend.semaphore._value)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting)

    async def run() -> None:
        await asyncio.gather(*(backend.step(request()) for _ in range(5)))

    asyncio.run(run())
    assert peak == 2


def test_cli_backend_reports_a_missing_binary() -> None:
    with pytest.raises(LLMError) as e:
        asyncio.run(ClaudeCLIBackend(binary="/nonexistent/claude").step(request()))
    assert e.value.code == "unavailable"


# --- API backend against a mock transport ---


def _api(handler: Any) -> AnthropicAPIBackend:
    return AnthropicAPIBackend("sk-test", transport=httpx.MockTransport(handler))


def test_api_backend_sends_native_tools_with_cache_breakpoints() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        assert req.headers["x-api-key"] == "sk-test" and req.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "Latency next."},
                    {"type": "tool_use", "id": "toolu_1", "name": "prom_range", "input": {"query": "x"}},
                ],
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 30,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 0,
                },
            },
        )

    req = request(TASK, STEP, RESULTS)
    result = asyncio.run(_api(handler).step(req))
    assert result.step == {
        "reasoning": "Latency next.",
        "actions": [{"tool": "prom_range", "args": {"query": "x"}}],
        "final": None,
    }
    assert result.usage.cache_read_tokens == 2000 and result.cost_usd is None and result.backend == "api"
    body = seen[0]
    assert [t["name"] for t in body["tools"]] == ["prom_query", "prom_range", FINAL_TOOL]
    assert body["tool_choice"]["type"] == "any"
    assert json.dumps(body).count('"cache_control"') == 3  # tools, system, transcript so far
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "c1_1",
        "name": "prom_query",
        "input": {"query": "up"},
    }
    assert body["messages"][2]["content"][0]["tool_use_id"] == "c1_1"


def test_api_backend_forces_the_final_tool_when_there_are_no_tools() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["tool_choice"] == {"type": "tool", "name": FINAL_TOOL}
        return httpx.Response(
            200,
            json={
                "content": [{"type": "tool_use", "id": "t", "name": FINAL_TOOL, "input": {"answer": "payment"}}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    result = asyncio.run(_api(handler).step(request(tools=False)))
    assert result.step["final"] == {"answer": "payment"}


def test_api_rendering_answers_a_rejected_final_with_a_tool_result() -> None:
    final_step = {"type": "step", "reasoning": "Done.", "actions": [], "final": {"answer": "x"}}
    feedback = {"type": "user", "text": "ev_9 does not exist"}
    messages = render_messages([TASK, STEP, RESULTS, final_step, feedback])
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    use = messages[3]["content"][-1]
    assert use["name"] == FINAL_TOOL
    assert messages[4]["content"] == [
        {"type": "tool_result", "tool_use_id": use["id"], "content": "ev_9 does not exist", "is_error": True}
    ]


@pytest.mark.parametrize(
    ("status", "headers", "code", "retryable", "after"),
    [
        (429, {"retry-after": "12"}, "rate_limited", True, 12.0),
        (529, {}, "overloaded", True, None),
        (500, {}, "unavailable", True, None),
        (401, {}, "auth", False, None),
        (400, {}, "bad_request", False, None),
    ],
)
def test_api_backend_classifies_http_errors(
    status: int, headers: dict[str, str], code: str, retryable: bool, after: float | None
) -> None:
    backend = _api(lambda _: httpx.Response(status, headers=headers, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMError) as e:
        asyncio.run(backend.step(request()))
    assert (e.value.code, e.value.retryable, e.value.retry_after_s) == (code, retryable, after)
    assert "nope" in e.value.message


# --- retries ---


class Scripted:
    name = "cli"

    def __init__(self, *outcomes: LLMError | dict[str, Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def step(self, req: LLMRequest) -> LLMResult:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, LLMError):
            raise outcome
        return LLMResult(outcome, Usage(1, 1), 0.001, 5, req.model, "cli", req.hash())


def _no_sleep() -> tuple[list[float], Any]:
    waits: list[float] = []

    async def sleep(s: float) -> None:
        waits.append(s)

    return waits, sleep


def test_retry_recovers_from_transient_errors() -> None:
    waits, sleep = _no_sleep()
    inner = Scripted(
        LLMError("overloaded", "x", retryable=True),
        LLMError("rate_limited", "y", retryable=True, retry_after_s=9),
        {"reasoning": "", "actions": [], "final": {}},
    )
    result = asyncio.run(RetryingBackend(inner, base_s=1, sleep=sleep).step(request()))
    assert inner.calls == 3 and result.attempts == 3
    assert waits[1] >= 9


@pytest.mark.parametrize(
    "error",
    [LLMError("auth", "x"), LLMError("rate_limited", "window resets in 2h", retryable=True, retry_after_s=7200)],
)
def test_retry_gives_up_at_once_when_waiting_cannot_help(error: LLMError) -> None:
    waits, sleep = _no_sleep()
    inner = Scripted(error)
    with pytest.raises(LLMError):
        asyncio.run(RetryingBackend(inner, max_wait_s=120, sleep=sleep).step(request()))
    assert inner.calls == 1 and waits == []


def test_retry_stops_after_the_last_attempt() -> None:
    _, sleep = _no_sleep()
    inner = Scripted(*[LLMError("timeout", "t", retryable=True)] * 3)
    with pytest.raises(LLMError) as e:
        asyncio.run(RetryingBackend(inner, retries=2, sleep=sleep).step(request()))
    assert inner.calls == 3 and e.value.attempts == 3


# --- record / replay ---


def test_record_then_replay_gives_the_same_steps(tmp_path: Path) -> None:
    cassette = LLMCassette(meta={"run": "test"})
    recorder = RecordingBackend(
        Scripted(
            {"reasoning": "a", "actions": [{"tool": "prom_query", "args": {}}]},
            {"reasoning": "b", "final": {"answer": "x"}},
        ),
        cassette,
    )
    first, second = request(TASK), request(TASK, STEP, RESULTS)
    live = [asyncio.run(recorder.step(r)) for r in (first, second)]
    path = tmp_path / "llm.jsonl.gz"
    cassette.dump(path)
    data = path.read_bytes()
    cassette.dump(path)
    assert path.read_bytes() == data  # gzip without a timestamp: same recording, same bytes

    replay = ReplayBackend(LLMCassette.load(path))
    # Order does not matter: the key is the request, so parallel agents replay fine.
    replayed = [asyncio.run(replay.step(r)) for r in (second, first)][::-1]
    assert [r.step for r in replayed] == [r.step for r in live]
    assert [r.usage for r in replayed] == [r.usage for r in live]
    assert {r.backend for r in replayed} == {"replay"}


def test_replay_misses_loudly_when_anything_the_model_saw_changed(tmp_path: Path) -> None:
    cassette = LLMCassette()
    asyncio.run(RecordingBackend(Scripted({"reasoning": "a", "final": {"answer": "x"}}), cassette).step(request()))
    replay = ReplayBackend(cassette)
    changed = LLMRequest(**{**request().__dict__, "system": "You investigate carefully."})
    for req in (changed, LLMRequest(**{**request().__dict__, "prompt_version": "v2"})):
        with pytest.raises(LLMError) as e:
            asyncio.run(replay.step(req))
        assert e.value.code == "replay_miss" and not e.value.retryable


# --- routing ---


def test_model_for_role_then_family_then_default() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_role_models={"investigator": "claude-sonnet-5-5", "investigator.changes": "claude-opus-5-5"},
    )
    assert model_for("investigator.changes", settings) == "claude-opus-5-5"
    assert model_for("investigator.metrics", settings) == "claude-sonnet-5-5"
    assert model_for("synthesizer", settings) == "claude-haiku-5-5"


@pytest.mark.skipif(not os.environ.get("SRE_LLM_LIVE"), reason="set SRE_LLM_LIVE=1 to call the real claude -p")
def test_live_cli_step() -> None:
    result = asyncio.run(ClaudeCLIBackend().step(request()))
    assert result.step["actions"] or result.step["final"]
