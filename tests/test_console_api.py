"""What the console reads: the agent call tree and the change stream."""

from __future__ import annotations

import httpx
from sqlalchemy import text

from app.api.app import create_app
from app.api.stream import Cursor, changes, now
from app.console.export import export
from tests.helpers.payment_scripts import payment_scripts
from tests.helpers.scripted_llm import ScriptedLLM
from tests.test_investigation import auth, fire, run_until
from tests.test_worker import fast


async def test_the_call_tree_puts_each_tool_call_under_the_step_that_asked_for_it(engine, settings, client):
    ids = await fire(client, settings, "checkout", "payment")
    await run_until(engine, fast(settings), ScriptedLLM(payment_scripts()), done={"DIAGNOSED"})
    checkout = ids["checkout"]

    resp = await client.get(f"/api/v1/incidents/{checkout}/agents", headers=auth(settings, "viewer"))
    assert resp.status_code == 200, resp.text
    tree = resp.json()
    assert tree[0]["agent"] == "coordinator" and tree[-1]["agent"] == "synthesizer"
    by_agent = {r["agent"]: r for r in tree}
    assert {a: len(r["llm_calls"]) for a, r in by_agent.items()} == {
        "coordinator": 1,
        "investigator.metrics": 2,
        "investigator.logs_traces": 2,
        "investigator.changes": 2,
        "synthesizer": 1,
    }
    for run in tree:
        steps = run["llm_calls"]
        assert [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))
        assert steps[-1]["final"] and not any(s["tools"] for s in steps if s["final"])
        assert run["usage"]["tool_calls"] == sum(len(s["tools"]) for s in steps)
    for name in ("investigator.metrics", "investigator.logs_traces", "investigator.changes"):
        first = by_agent[name]["llm_calls"][0]
        assert first["tools"] and all(t["evidence_id"].startswith("ev_") for t in first["tools"] if t["status"] == "ok")

    async with engine.connect() as conn:
        stored = await conn.scalar(text("SELECT count(*) FROM tool_calls WHERE incident_id = :i"), {"i": checkout})
    assert sum(r["usage"]["tool_calls"] for r in tree) == stored
    assert sum(r["usage"]["prompt_tokens"] + r["usage"]["output_tokens"] for r in tree) == 8000

    other = await client.get(f"/api/v1/incidents/{checkout}/agents", headers=auth(settings, "viewer", tenant="other"))
    assert other.status_code == 404


async def test_changes_come_in_order_resume_from_any_event_and_stay_in_the_tenant(engine, settings, client):
    start = await now(engine, "lab")
    assert start == Cursor()
    ids = await fire(client, settings, "checkout")
    found, after = await changes(engine, "lab", start)
    audit = [d for e, d, _ in found if e == "audit"]
    assert [d["event"] for d in audit] == ["incident.created"]
    assert audit[0]["subject"] == str(ids["checkout"])
    assert after == await now(engine, "lab")
    assert (await changes(engine, "other", Cursor()))[0] == []

    await run_until(engine, fast(settings), ScriptedLLM(payment_scripts()), done={"DIAGNOSED"})
    found, end = await changes(engine, "lab", after)
    events = [e for e, _, _ in found]
    assert "activity" in events and events.index("activity") > max(i for i, e in enumerate(events) if e == "audit")
    activity = next(d for e, d, _ in found if e == "activity")
    assert activity["incident_id"] == str(ids["checkout"]) and "synthesizer" in activity["agents"]
    moves = [(d["payload"]["from"], d["payload"]["to"]) for e, d, _ in found if d.get("event") == "incident.transition"]
    assert moves[-1] == ("INVESTIGATING", "DIAGNOSED")

    # Resuming from the cursor of any audit event yields exactly the audit rows after it.
    second = [c for e, _, c in found if e == "audit"][1]
    again, _ = await changes(engine, "lab", second)
    assert [d["seq"] for e, d, _ in again if e == "audit"] == [d["seq"] for e, d, _ in found if e == "audit"][2:]
    assert (await changes(engine, "lab", end))[0] == []


async def test_the_stream_sends_events_from_the_last_event_id(engine, settings, client):
    await fire(client, settings, "checkout")
    app = create_app(settings.model_copy(update={"stream_max_s": 0.05, "stream_poll_s": 0.01}))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        assert (await c.get("/api/v1/stream")).status_code == 401
        resp = await c.get("/api/v1/stream", headers={**auth(settings, "viewer"), "Last-Event-ID": "0.0.0"})
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
        frames = [f for f in resp.text.split("\n\n") if f.strip()]
        assert frames[0].startswith("retry: 2000\nid: 0.0.0\nevent: ready")
        assert any("event: audit" in f and "incident.created" in f for f in frames)

        # Without an id the stream starts from now: nothing old is sent.
        resp = await c.get("/api/v1/stream", headers=auth(settings, "viewer"))
        assert "event: audit" not in resp.text and "event: ready" in resp.text


async def test_an_exported_case_holds_every_response_the_incident_page_reads(engine, settings, client):
    ids = await fire(client, settings, "checkout", "payment")
    await run_until(engine, fast(settings), ScriptedLLM(payment_scripts()), done={"DIAGNOSED"})
    checkout = ids["checkout"]
    doc = await export(settings, checkout)
    base = f"/api/v1/incidents/{checkout}"
    responses = doc["responses"]
    viewer = auth(settings, "viewer")
    for path in (base, f"{base}/diagnosis", f"{base}/agents", f"{base}/plans", f"{base}/audit"):
        assert responses[path] == (await client.get(path, headers=viewer)).json(), path
    assert responses["/api/v1/incidents"] == [responses[base]]
    evidence = [p for p in responses if p.startswith("/api/v1/evidence/")]
    produced = {t["evidence_id"] for r in responses[f"{base}/agents"] for s in r["llm_calls"] for t in s["tools"]}
    assert {p.rsplit("/", 1)[1] for p in evidence} == produced - {None}
