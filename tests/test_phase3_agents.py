from __future__ import annotations

import sqlite3

import pytest

from ace_sim.agents.base_agent import RetailAgent
from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.guardrails.secretary_auditor import UnauthorizedActionError
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator


def new_stack(tmp_path) -> tuple[ACE_Engine, Simulation_Orchestrator]:
    db_path = tmp_path / "phase3_agents.sqlite3"
    engine = ACE_Engine(db_path=db_path)
    orchestrator = Simulation_Orchestrator(engine)
    return engine, orchestrator


def test_agent_execute_action_logs_thought_and_hides_it_from_broadcast(tmp_path):
    _, orchestrator = new_stack(tmp_path)
    orchestrator.register_agent("alice", role="retail", community_id="c1")
    orchestrator.register_agent("bob", role="retail", community_id="c1")
    orchestrator.connect_agents("alice", "bob")

    def mock_llm(_prompt: str):
        return {
            "thought": "这条是内部推理，不应广播。",
            "speak": {"target": "forum", "message": "大家小心波动。", "mode": "new"},
            "action": None,
        }

    alice = RetailAgent(agent_id="alice", community_id="c1", llm_callable=mock_llm)
    result = alice.execute_action(orchestrator=orchestrator)

    assert len(result["event_ids"]) == 1
    bob_inbox = orchestrator.read_inbox("bob")
    assert len(bob_inbox) == 1
    assert "内部推理" not in bob_inbox[0]["message"]
    assert "大家小心波动" in bob_inbox[0]["message"]

    conn = sqlite3.connect(orchestrator.engine.get_db_path())
    row = conn.execute(
        """
        SELECT thought, audit_status
        FROM thought_log
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "这条是内部推理，不应广播。"
    assert row[1] == "success"


def test_agent_role_permission_guardrail_blocks_forbidden_action(tmp_path):
    engine, orchestrator = new_stack(tmp_path)
    orchestrator.register_agent("alice", role="retail", community_id="c1")
    engine.create_account("alice", ust="100")

    def mock_llm(_prompt: str):
        return {
            "thought": "尝试执行越权动作。",
            "speak": None,
            "action": {
                "action_type": "SWAP",
                "params": {
                    "pool_name": "Pool_A",
                    "token_in": "UST",
                    "amount": "1",
                    "slippage_tolerance": "0.1",
                },
                "gas_price": "1",
            },
        }

    # Force a role not in the matrix to trigger strict guardrail.
    bad_agent = RetailAgent(agent_id="alice", community_id="c1", llm_callable=mock_llm)
    bad_agent.role = "outsider"

    with pytest.raises(UnauthorizedActionError):
        bad_agent.execute_action(orchestrator=orchestrator)

    conn = sqlite3.connect(orchestrator.engine.get_db_path())
    row = conn.execute(
        """
        SELECT audit_status, audit_error
        FROM thought_log
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "failed"
    assert "unknown agent role" in row[1]
