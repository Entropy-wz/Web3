from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.action_registry.actions import ActionValidationError
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator
from ace_sim.social.perception_filter import PerceptionFilter


def new_orchestrator(tmp_path, **kwargs) -> tuple[ACE_Engine, Simulation_Orchestrator]:
    db_path = tmp_path / "phase3_test.sqlite3"
    engine = ACE_Engine(db_path=db_path)
    orchestrator = Simulation_Orchestrator(engine=engine, **kwargs)
    return engine, orchestrator


def test_inbox_overload_inserts_system_notice(tmp_path):
    engine, orchestrator = new_orchestrator(tmp_path)
    orchestrator.register_agent("target", role="retail", community_id="c1")

    for i in range(10):
        sender = f"sender_{i}"
        orchestrator.register_agent(sender, role="retail", community_id="c1")
        orchestrator.connect_agents(sender, "target")
        orchestrator.submit_event(
            sender,
            "SPEAK",
            {"target": "forum", "message": f"panic-{i}", "mode": "new"},
        )

    inbox = orchestrator.read_inbox("target", max_inbox_size=5)
    assert len(inbox) == 5
    assert any(msg["is_overload_notice"] for msg in inbox)
    assert any("missed a large batch" in msg["message"] for msg in inbox)

    conn = sqlite3.connect(engine.get_db_path())
    row = conn.execute(
        """
        SELECT total_pending, returned_count, dropped_count
        FROM inbox_overload_log
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    assert row == (10, 5, 6)


def test_inbox_overload_with_max1_only_returns_notice(tmp_path):
    _, orchestrator = new_orchestrator(tmp_path)
    orchestrator.register_agent("target", role="retail", community_id="c1")

    for i in range(3):
        sender = f"sender_{i}"
        orchestrator.register_agent(sender, role="retail", community_id="c1")
        orchestrator.connect_agents(sender, "target")
        orchestrator.submit_event(
            sender,
            "SPEAK",
            {"target": "forum", "message": f"rumor-{i}", "mode": "new"},
        )

    inbox = orchestrator.read_inbox("target", max_inbox_size=1)
    assert len(inbox) == 1
    assert inbox[0]["is_overload_notice"] is True


def test_parent_event_id_required_for_relay_reply(tmp_path):
    _, orchestrator = new_orchestrator(tmp_path)
    orchestrator.register_agent("a", role="retail", community_id="c1")
    orchestrator.register_agent("b", role="retail", community_id="c1")
    orchestrator.connect_agents("a", "b")
    orchestrator.connect_agents("b", "a")

    with pytest.raises(ActionValidationError):
        orchestrator.submit_event(
            "b",
            "SPEAK",
            {"target": "forum", "message": "forwarding", "mode": "relay"},
        )

    parent_event_id = orchestrator.submit_event(
        "a",
        "SPEAK",
        {"target": "forum", "message": "origin", "mode": "new"},
    )
    relay_event_id = orchestrator.submit_event(
        "b",
        "SPEAK",
        {
            "target": "forum",
            "message": "forwarding",
            "mode": "relay",
            "parent_event_id": parent_event_id,
        },
    )

    conn = sqlite3.connect(orchestrator.engine.get_db_path())
    row = conn.execute(
        """
        SELECT event_id, parent_event_id
        FROM semantic_delivery_log
        WHERE event_id = ?
        LIMIT 1
        """,
        (relay_event_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == relay_event_id
    assert row[1] == parent_event_id


def test_cross_community_message_has_decay_and_delay(tmp_path):
    filter_obj = PerceptionFilter(seed=7)
    _, orchestrator = new_orchestrator(tmp_path, perception_filter=filter_obj)
    orchestrator.register_agent("sender", role="retail", community_id="c1")
    orchestrator.register_agent("receiver", role="retail", community_id="c2")
    orchestrator.connect_agents("sender", "receiver")

    orchestrator.submit_event(
        "sender",
        "SPEAK",
        {
            "target": "forum",
            "message": "UST 0.98, whale sold 1000 LUNA in 1 minute",
            "mode": "new",
        },
    )
    assert orchestrator.read_inbox("receiver") == []

    orchestrator.step_tick()
    assert orchestrator.read_inbox("receiver") == []

    report = orchestrator.step_tick()
    assert len(report.semantic_deliveries) == 1

    inbox = orchestrator.read_inbox("receiver")
    assert len(inbox) == 1
    assert "[PRICE_SHOCK]" in inbox[0]["message"]
    assert "rule" in inbox[0]["transform_tag"]


def test_prefix_probability_is_close_to_30_percent_under_fixed_seed():
    pf = PerceptionFilter(seed=123, prefix_probability=0.3)
    total = 1000
    prefixed = 0
    for _ in range(total):
        out = pf.transform(
            message="UST 0.95",
            sender="a",
            receiver="b",
            channel="FORUM",
            is_cross_community=True,
            current_tick=0,
        )
        if out.message.startswith("[RUMOR]") or out.message.startswith("[PANIC]"):
            prefixed += 1
    ratio = prefixed / total
    assert 0.25 <= ratio <= 0.35


def test_semantic_path_does_not_break_gas_ordering(tmp_path):
    engine, orchestrator = new_orchestrator(tmp_path)
    engine.create_account("alice", ust="1000")
    engine.create_account("bob", ust="1000")

    orchestrator.register_agent("alice", role="retail", community_id="c1")
    orchestrator.register_agent("bob", role="retail", community_id="c1")
    orchestrator.connect_agents("alice", "bob")

    orchestrator.submit_event(
        "alice",
        "SPEAK",
        {"target": "forum", "message": "news", "mode": "new"},
    )
    orchestrator.submit_transaction(
        "alice",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="1",
    )
    orchestrator.submit_transaction(
        "bob",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="9",
    )

    report = orchestrator.step_tick()
    assert [r.agent_id for r in report.receipts] == ["bob", "alice"]
    assert report.receipts[0].status == "success"
    assert report.receipts[1].status == "success"
    assert orchestrator.protocol_fee_vault["UST"] == Decimal("10")
