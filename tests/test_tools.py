"""M3 tools: summaries, the registry's error handling, record/replay, the evidence store."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.tools import ALL_TOOLS, ToolContext, ToolRegistry
from app.tools.base import Tool, ToolArgs, canonical_json, sha256_hex
from app.tools.compress import bound, template
from app.tools.evidence import MemoryEvidenceStore, PgEvidenceStore, load
from app.tools.loki import LogsSearchArgs, logql
from app.tools.sources import Breaker, Cassette, LiveSource, RecordingSource, ReplaySource
from tests.helpers import fake_backends
from tests.helpers.fake_backends import NOW, FakeBackends

CTX = ToolContext(tenant_id="lab")
FIXED_NOW = datetime.fromtimestamp(NOW, UTC)


async def _no_sleep(_: float) -> None:
    return None


def _registry(fake: FakeBackends | None = None, **live: Any) -> tuple[ToolRegistry, MemoryEvidenceStore]:
    fake = fake or FakeBackends()
    source = LiveSource(fake.clients(), now=lambda: FIXED_NOW, sleep=_no_sleep, **live)
    store = MemoryEvidenceStore()
    return ToolRegistry(ALL_TOOLS, source, store), store


async def _call(name: str, args: dict[str, Any], fake: FakeBackends | None = None) -> dict[str, Any]:
    registry, _ = _registry(fake)
    outcome = await registry.call(name, args, CTX)
    assert outcome.ok, outcome.error
    assert outcome.summary is not None
    return outcome.summary


# --- definitions -----------------------------------------------------------------------------


def test_every_tool_is_read_only_and_has_a_strict_schema() -> None:
    names = [t.name for t in ALL_TOOLS]
    assert len(names) == len(set(names)) == 9
    for tool in ALL_TOOLS:
        assert tool.risk == "R0"
        spec = tool.spec()
        assert spec["input_schema"]["additionalProperties"] is False
        assert "title" not in spec["input_schema"]
        assert len(spec["description"]) > 40


# --- summaries -------------------------------------------------------------------------------


async def test_prom_query_sorts_series_and_drops_nan() -> None:
    s = await _call("prom_query", {"query": "service:sli_error_ratio:rate5m"})
    assert [x["labels"]["service_name"] for x in s["top"]] == ["payment", "checkout", "cart"]
    assert s["top"][2]["value"] is None


async def test_prom_range_reports_peak_and_shape() -> None:
    s = await _call("prom_range", {"query": "service:sli_error_ratio:rate5m", "minutes": 10})
    payment = s["series"][0]
    assert payment["labels"] == {"service_name": "payment"}
    assert (payment["first"], payment["max"], payment["last"]) == (0.0, 0.5, 0.1)
    assert len(payment["shape"]) == 12 and payment["shape"][0] == 0.0 and max(payment["shape"]) == 0.5


async def test_logs_search_groups_messages_and_keeps_error_details() -> None:
    fake = FakeBackends()
    s = await _call("logs_search", {"service": "frontend", "level": "error"}, fake)
    assert s["lines"] == 3 and s["by_level"] == {"error": 2, "info": 1}
    top = s["groups"][0]
    assert top["count"] == 2
    assert "ECONNREFUSED <ip>" in top["template"]
    assert top["attributes"]["http_response_status_code"] == "500"
    assert "trace_id" not in top["attributes"]
    sent = fake.requests[0]
    assert sent.headers["X-Loki-Response-Encoding-Flags"] == "categorize-labels"
    assert sent.url.params["query"].startswith('{service_name="frontend"} | detected_level=~')


def test_logql_escapes_text_and_rejects_injection() -> None:
    q = logql(LogsSearchArgs(service="checkout", contains="port 9999 (payment)"))
    assert q == '{service_name="checkout"} |~ `(?i)port 9999 \\(payment\\)`'
    with pytest.raises(ValueError):
        LogsSearchArgs(service='x"} or {job=~".+', contains=None)
    with pytest.raises(ValueError):
        LogsSearchArgs(service="x", contains="`")
    # "This level or worse": a warn search must not miss the errors.
    assert "error" in logql(LogsSearchArgs(service="x", level="warn"))


async def test_traces_search_groups_error_spans() -> None:
    fake = FakeBackends()
    s = await _call("traces_search", {"service": "checkout", "errors_only": True}, fake)
    assert (s["traces"], s["with_errors"]) == (2, 1)
    (err,) = s["error_spans"]
    assert (err["service"], err["operation"], err["count"]) == ("payment", "oteldemo.PaymentService/Charge", 1)
    assert err["message"] == "failed to charge card: invalid token"
    assert err["status"] == "rpc.grpc.status_code=13"
    assert s["slowest"][0]["root"] == {"service": "frontend", "operation": "POST /api/checkout"}
    assert json.loads(fake.requests[0].url.params["tags"]) == {"error": "true"}


async def test_trace_get_ranks_spans_by_self_time() -> None:
    s = await _call("trace_get", {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"})
    assert s["services"] == ["checkout", "frontend", "payment"]
    assert s["orphan_spans"] == 0
    slow = s["slowest_spans"][0]
    # PlaceOrder took 850 ms, of which 100 ms in its child: 750 ms of its own.
    assert (slow["operation"], slow["self_ms"]) == ("oteldemo.CheckoutService/PlaceOrder", 750.0)
    assert s["errors"][0]["service"] == "payment"


async def test_k8s_events_hide_chaos_mesh_and_put_warnings_first() -> None:
    registry, store = _registry()
    outcome = await registry.call("k8s_events", {"minutes": 60}, CTX)
    s = outcome.summary
    assert s is not None
    assert s["groups"][0]["reason"] == "BackOff"
    reasons = {g["reason"] for g in s["groups"]}
    assert "Applied" not in reasons  # the NetworkChaos event never reaches the agent
    assert "Pulled" not in reasons  # outside the window
    raw = store.evidence[outcome.evidence_id or ""].result
    assert all("chaos" not in json.dumps(e) for e in raw["items"])  # nor the stored evidence
    assert all("managedFields" not in e["metadata"] for e in raw["items"])


async def test_rollout_history_and_diff_show_the_bad_release() -> None:
    h = await _call("rollout_history", {"deployment": "checkout"})
    assert [r["revision"] for r in h["revisions"]] == [2, 1]  # the other owner's ReplicaSet is not ours
    assert h["revisions"][0]["current"] is True
    assert "port 9999" in h["revisions"][0]["change_cause"]

    d = await _call("deploy_diff", {"deployment": "checkout"})
    assert (d["from"]["revision"], d["to"]["revision"]) == (1, 2)
    assert d["changes"] == [
        {"path": "containers.checkout.env.PAYMENT_ADDR", "from": "payment:8080", "to": "payment:9999"}
    ]


async def test_flag_state_lists_flags_that_are_on() -> None:
    fake = FakeBackends(flag_variants={"paymentFailure": "50%"})
    s = await _call("flag_state", {}, fake)
    assert s["on"] == [{"flag": "paymentFailure", "variant": "50%", "value": 0.5, "targeting": False}]
    registry, _ = _registry(fake)
    missing = await registry.call("flag_state", {"flag": "nope"}, CTX)
    assert missing.error is not None and missing.error.code == "not_found"


# --- registry: errors a model can act on -----------------------------------------------------


async def test_invalid_args_and_unknown_tools_are_structured_errors() -> None:
    registry, store = _registry()
    bad = await registry.call("prom_range", {"query": "up", "minutes": 9999, "colour": "red"}, CTX)
    assert bad.error is not None and bad.error.code == "invalid_args"
    assert "minutes" in bad.error.message and "colour" in bad.error.message
    unknown = await registry.call("kubectl_delete", {}, CTX)
    assert unknown.error is not None and unknown.error.code == "unknown_tool"
    assert "prom_query" in unknown.error.message
    assert json.loads(unknown.for_model())["error"]["code"] == "unknown_tool"
    assert [c.status for c in store.calls] == ["error", "error"] and not store.evidence


async def test_bad_query_returns_the_backends_message_without_retrying() -> None:
    fake = FakeBackends()
    fake.overrides[("prom.test", "/api/v1/query")] = lambda r: httpx.Response(
        400, json={"status": "error", "error": 'parse error: unexpected "}"'}
    )
    registry, _ = _registry(fake)
    out = await registry.call("prom_query", {"query": "sum(}"}, CTX)
    assert out.error is not None and out.error.code == "bad_query"
    assert "parse error" in out.error.message
    assert len(fake.requests) == 1


async def test_transient_errors_are_retried() -> None:
    fake = FakeBackends()
    answers = iter([httpx.Response(503, text="overloaded"), None])

    def flaky(request: httpx.Request) -> httpx.Response:
        a = next(answers)
        if a is not None:
            return a
        del fake.overrides[("prom.test", "/api/v1/query")]
        return fake.handler(request)

    fake.overrides[("prom.test", "/api/v1/query")] = flaky
    registry, _ = _registry(fake)
    out = await registry.call("prom_query", {"query": "up"}, CTX)
    assert out.ok and out.attempts == 2


async def test_timeouts_give_up_after_the_retries() -> None:
    slow = Tool(
        name="slow",
        description="never answers in time",
        args=ToolArgs,
        backend="slow",
        fetch=lambda c, a, now: asyncio.sleep(10),  # type: ignore[arg-type,return-value]
        summarize=lambda raw, a: {},
        timeout_s=0.01,
    )
    source = LiveSource(FakeBackends().clients(), sleep=_no_sleep, retries=1)
    out = await ToolRegistry([slow], source, MemoryEvidenceStore()).call("slow", {}, CTX)
    assert out.error is not None and (out.error.code, out.attempts) == ("timeout", 2)


async def test_circuit_breaker_opens_per_backend_and_recovers() -> None:
    clock = [0.0]
    fake = FakeBackends()
    fake.overrides[("loki.test", "/loki/api/v1/query_range")] = lambda r: httpx.Response(502)
    source = LiveSource(
        fake.clients(),
        retries=0,
        sleep=_no_sleep,
        now=lambda: FIXED_NOW,
        breakers={"loki": Breaker(clock=lambda: clock[0])},
    )
    registry = ToolRegistry(ALL_TOOLS, source, MemoryEvidenceStore())
    codes = [(await registry.call("logs_search", {"service": "cart"}, CTX)).error.code for _ in range(6)]  # type: ignore[union-attr]
    assert codes == ["backend_error"] * 5 + ["circuit_open"]
    assert len(fake.requests) == 5  # the sixth call never left the process
    assert (await registry.call("prom_query", {"query": "up"}, CTX)).ok  # other backends unaffected
    clock[0] = 31.0
    del fake.overrides[("loki.test", "/loki/api/v1/query_range")]
    assert (await registry.call("logs_search", {"service": "cart"}, CTX)).ok  # half open, then closed


async def test_oversized_results_are_refused_with_a_hint() -> None:
    small = Tool(
        name="big",
        description="returns too much",
        args=ToolArgs,
        backend="x",
        fetch=lambda c, a, now: asyncio.sleep(0, result=["x" * 100] * 100),  # type: ignore[arg-type,return-value]
        summarize=lambda raw, a: {},
        max_result_bytes=1000,
    )
    source = LiveSource(FakeBackends().clients(), sleep=_no_sleep)
    out = await ToolRegistry([small], source, MemoryEvidenceStore()).call("big", {}, CTX)
    assert out.error is not None and out.error.code == "too_large" and "narrow" in out.error.message


def test_bound_halves_the_largest_list_first() -> None:
    summary = {"count": 500, "groups": [{"msg": "m" * 50, "n": i} for i in range(500)], "few": [1, 2]}
    out, truncated = bound(summary, max_chars=2000)
    assert truncated and out["truncated"] is True
    assert len(canonical_json(out)) <= 2000
    assert out["groups"][0]["n"] == 0 and out["few"] == [1, 2] and out["count"] == 500


def test_templates_collapse_variable_parts_but_not_words() -> None:
    a = template("connect ECONNREFUSED 10.96.174.148:8080 for HQTGWGPNH4 after 12.5 ms")
    b = template("connect ECONNREFUSED 10.96.183.222:8080 for 9SIQT8TOJO after 3 ms")
    assert a == b == "connect ECONNREFUSED <ip> for <id> after <n> ms"
    assert template("CONNECTION refused") == "CONNECTION refused"


# --- record / replay -------------------------------------------------------------------------

CALLS = [
    ("prom_query", {"query": "service:sli_error_ratio:rate5m"}),
    ("prom_range", {"query": "service:sli_error_ratio:rate5m", "minutes": 10}),
    ("logs_search", {"service": "frontend", "level": "error"}),
    ("traces_search", {"service": "checkout", "errors_only": True}),
    ("trace_get", {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}),
    ("k8s_events", {}),
    ("rollout_history", {"deployment": "checkout"}),
    ("deploy_diff", {"deployment": "checkout"}),
    ("flag_state", {"flag": "paymentFailure"}),
    ("flag_state", {"flag": "nope"}),  # an error is recorded and replayed too
]


@pytest.mark.parametrize("name", ["lab.jsonl", "lab.jsonl.gz"])
async def test_replay_is_byte_identical_to_the_recording(tmp_path: Path, name: str) -> None:
    cassette = Cassette(meta={"scenario": "test"})
    live = LiveSource(FakeBackends().clients(), now=lambda: FIXED_NOW, sleep=_no_sleep)
    recorder = ToolRegistry(ALL_TOOLS, RecordingSource(live, cassette), MemoryEvidenceStore())
    recorded = [await recorder.call(name, args, CTX) for name, args in CALLS]
    path = tmp_path / name
    cassette.dump(path)
    cassette.dump(tmp_path / f"again-{name}")
    assert path.read_bytes() == (tmp_path / f"again-{name}").read_bytes()  # gzip without a timestamp

    replay_store = MemoryEvidenceStore()
    replayer = ToolRegistry(ALL_TOOLS, ReplaySource(Cassette.load(path)), replay_store)
    replayed = [await replayer.call(name, args, CTX) for name, args in CALLS]

    for a, b in zip(recorded, replayed, strict=True):
        assert a.for_model() == b.for_model()  # what the model sees, evidence id included
        assert a.meta.get("result_hash") == b.meta.get("result_hash")
    assert {e.source for e in replay_store.evidence.values()} == {"replay"}
    assert {e.fetched_at for e in replay_store.evidence.values()} == {FIXED_NOW}

    miss = await replayer.call("prom_query", {"query": "never recorded"}, CTX)
    assert miss.error is not None and miss.error.code == "replay_miss"


async def test_replay_serves_repeated_calls_in_order_then_the_last() -> None:
    cassette = Cassette(
        entries=[
            {"tool": "flag_state", "args": {"flag": None}, "at": "2025-10-15T03:00:00+00:00", "raw": {"flags": {}}},
            {
                "tool": "flag_state",
                "args": {"flag": None},
                "at": "2025-10-15T03:05:00+00:00",
                "raw": {"flags": {"adFailure": {"defaultVariant": "on", "variants": {"on": True}}}},
            },
        ]
    )
    registry = ToolRegistry(ALL_TOOLS, ReplaySource(cassette), MemoryEvidenceStore())
    seen = [len((await registry.call("flag_state", {}, CTX)).summary["on"]) for _ in range(3)]  # type: ignore[index]
    assert seen == [0, 1, 1]


# --- evidence in Postgres --------------------------------------------------------------------


async def test_a_nul_character_in_a_result_is_replaced_before_it_is_hashed_and_stored(engine, monkeypatch) -> None:
    # Lab run 40458701488: a log or span with a NUL byte made Postgres refuse the evidence row
    # ("unsupported Unicode escape sequence"), and the whole investigation job failed and restarted.
    plain = fake_backends.loki_streams

    def with_nul(service: str = "frontend") -> dict[str, Any]:
        body = plain(service)
        for stream in body["data"]["result"]:
            stream["values"] = [[ts, line + "\x00binary", *rest] for ts, line, *rest in stream["values"]]
        return body

    monkeypatch.setattr(fake_backends, "loki_streams", with_nul)
    live = LiveSource(FakeBackends().clients(), now=lambda: FIXED_NOW, sleep=_no_sleep)
    registry = ToolRegistry(ALL_TOOLS, live, PgEvidenceStore(engine))
    outcome = await registry.call("logs_search", {"service": "payment", "minutes": 10}, CTX)
    assert outcome.ok, outcome.error

    row = await load(engine, outcome.evidence_id or "", tenant_id="lab")
    assert row is not None
    stored = canonical_json(row["result"])
    assert "\x00" not in stored and "\ufffdbinary" in stored
    assert sha256_hex(stored) == row["result_hash"]
    assert "\x00" not in json.dumps(outcome.summary, ensure_ascii=False)


async def test_evidence_is_stored_immutable_and_verifiable(engine) -> None:
    store = PgEvidenceStore(engine)
    live = LiveSource(FakeBackends().clients(), now=lambda: FIXED_NOW, sleep=_no_sleep)
    registry = ToolRegistry(ALL_TOOLS, live, store)
    first = await registry.call("rollout_history", {"deployment": "checkout"}, CTX)
    again = await registry.call("rollout_history", {"deployment": "checkout"}, CTX)
    failed = await registry.call("prom_range", {"query": "up", "minutes": 0}, CTX)
    assert first.evidence_id == again.evidence_id  # same data, same scope: one row
    assert failed.error is not None

    row = await load(engine, first.evidence_id or "", tenant_id="lab")
    assert row is not None
    assert sha256_hex(canonical_json(row["result"])) == row["result_hash"]  # snapshot unchanged
    assert row["summary"] == first.summary and row["source"] == "live"
    assert await load(engine, first.evidence_id or "", tenant_id="other") is None

    async with engine.connect() as conn:
        calls = (await conn.execute(text("SELECT status, error_code, evidence_id FROM tool_calls ORDER BY id"))).all()
        assert [tuple(c) for c in calls] == [
            ("ok", None, first.evidence_id),
            ("ok", None, first.evidence_id),
            ("error", "invalid_args", None),
        ]
    for statement in ("UPDATE evidence SET summary = '{}'", "DELETE FROM evidence"):
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="immutable"):
                await conn.execute(text(statement))
