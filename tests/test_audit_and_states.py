"""Audit hash chain and incident state machine."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import audit, incidents
from app.db.incidents import IllegalTransition, StaleVersion, check_transition


async def append_events(engine, n: int) -> None:
    for i in range(n):
        async with engine.begin() as conn:
            await audit.append(conn, actor="tester", event="test.event", subject=f"s{i}", payload={"i": i, "x": 0.5})


async def test_chain_verifies_and_survives_concurrent_appends(engine):
    async def one(i: int) -> None:
        async with engine.begin() as conn:
            await audit.append(conn, actor=f"a{i}", event="test.event", subject="s", tenant_id="t1")

    await asyncio.gather(*(one(i) for i in range(30)))
    async with engine.connect() as conn:
        result = await audit.verify(conn)
    assert result.ok and result.checked == 30


async def test_rows_cannot_be_updated_or_deleted(engine):
    await append_events(engine, 1)
    for statement in ("UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement))


@pytest.mark.parametrize(
    "tamper",
    [
        'UPDATE audit_log SET payload = \'{"i": 99, "x": 0.5}\' WHERE seq = 3',
        "UPDATE audit_log SET actor = 'mallory' WHERE seq = 3",
        "DELETE FROM audit_log WHERE seq = 3",
    ],
)
async def test_tampering_by_a_privileged_user_is_detected(engine, tamper):
    """B9: the prototype truncated hashes to 16 hex chars and never verified them."""
    await append_events(engine, 5)
    async with engine.begin() as conn:
        # Someone with owner rights switches the guard off first.
        await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete"))
        await conn.execute(text(tamper))
        await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update_delete"))
    async with engine.connect() as conn:
        result = await audit.verify(conn)
    # Changing row 3 breaks row 3; deleting it breaks the link from row 4.
    assert not result.ok and result.first_bad_seq in (3, 4) and result.checked == 2


@pytest.mark.parametrize(
    ("src", "dst"),
    [("RECEIVED", "INVESTIGATING"), ("TRIAGED", "RESOLVED"), ("RESOLVED", "ESCALATED"), ("ESCALATED", "TRIAGED")],
)
def test_illegal_transitions(src, dst):
    with pytest.raises(IllegalTransition):
        check_transition(src, dst)


def test_any_open_state_can_escalate():
    for state in incidents.TRANSITIONS:
        check_transition(state, "ESCALATED")


async def open_incident(engine) -> UUID:
    async with engine.begin() as conn:
        incident_id, _ = await incidents.open_or_merge(
            conn,
            tenant_id="t1",
            fingerprint="fp",
            title="HighErrorRate",
            service="checkout",
            environment="prod",
            priority=2,
            labels={},
        )
    return incident_id


async def test_transition_uses_optimistic_locking(engine):
    incident_id = await open_incident(engine)
    async with engine.begin() as conn:
        version = await incidents.transition(
            conn, incident_id, from_state="RECEIVED", to_state="TRIAGED", expected_version=1, actor="w1"
        )
    assert version == 2
    # A second writer that read version 1 loses.
    with pytest.raises(StaleVersion):
        async with engine.begin() as conn:
            await incidents.transition(
                conn, incident_id, from_state="RECEIVED", to_state="TRIAGED", expected_version=1, actor="w2"
            )


async def test_terminal_state_closes_the_incident_and_frees_the_fingerprint(engine):
    first = await open_incident(engine)
    async with engine.begin() as conn:
        await incidents.transition(
            conn, first, from_state="RECEIVED", to_state="ESCALATED", expected_version=1, actor="w1"
        )
        row = await incidents.get(conn, first, tenant_id="t1")
        assert row is not None and row["closed_at"] is not None
    # The same alert after closure opens a new incident instead of merging into the closed one.
    assert await open_incident(engine) != first
