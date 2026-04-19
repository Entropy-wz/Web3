from __future__ import annotations

from decimal import Decimal

import pytest

from ace_sim.engine.ace_engine import ACE_Engine, InsufficientFundsError
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator
from ace_sim.governance.compiler_agent import CompilerAgent, CompilerValidationError
from ace_sim.governance.governance import GovernanceModule, ProposalLimitError
from ace_sim.governance.logger_metrics import LoggerMetrics


def _new_stack(
    tmp_path,
    *,
    proposal_fee_luna: str = "1000",
    voting_window_ticks: int = 1,
    max_open_proposals: int = 3,
    max_open_per_agent: int = 1,
    max_tx_per_tick: int = 50,
    metrics: LoggerMetrics | None = None,
) -> tuple[ACE_Engine, Simulation_Orchestrator]:
    db_path = tmp_path / "phase5.sqlite3"
    engine = ACE_Engine(db_path=db_path)
    governance = GovernanceModule(
        db_path=engine.get_db_path(),
        proposal_fee_luna=proposal_fee_luna,
        voting_window_ticks=voting_window_ticks,
        max_open_proposals=max_open_proposals,
        max_open_per_agent=max_open_per_agent,
    )
    orchestrator = Simulation_Orchestrator(
        engine=engine,
        governance=governance,
        max_tx_per_tick=max_tx_per_tick,
        metrics_logger=metrics,
    )
    return engine, orchestrator


def test_governance_weight_uses_luna_snapshot_only(tmp_path):
    engine, orchestrator = _new_stack(tmp_path)
    engine.create_account("alice", luna="5000", usdc="10")
    engine.create_account("bob", luna="100", usdc="1000000", ust="1000000")

    proposal_id = orchestrator.submit_event(
        "alice",
        "PROPOSE",
        {"proposal_text": "disable minting"},
    )
    orchestrator.submit_event(
        "alice",
        "VOTE",
        {"proposal_id": proposal_id, "decision": "approve"},
    )
    orchestrator.submit_event(
        "bob",
        "VOTE",
        {"proposal_id": proposal_id, "decision": "reject"},
    )

    report = orchestrator.step_tick()
    settlement = report.governance_settlements[0]

    # Alice paid proposal fee first: 5000 -> 4000 snapshot voting weight.
    assert settlement.approve_weight == Decimal("4000")
    assert settlement.reject_weight == Decimal("100")


def test_proposal_fee_and_anti_spam_limits(tmp_path):
    engine, orchestrator = _new_stack(tmp_path, voting_window_ticks=20)
    engine.create_account("alice", luna="1500")
    engine.create_account("bob", luna="3000")
    engine.create_account("carol", luna="3000")
    engine.create_account("dave", luna="3000")
    engine.create_account("eve", luna="500")

    orchestrator.submit_event("alice", "PROPOSE", {"proposal_text": "disable minting"})
    with pytest.raises(ProposalLimitError):
        orchestrator.submit_event("alice", "PROPOSE", {"proposal_text": "enable minting"})

    with pytest.raises(InsufficientFundsError):
        orchestrator.submit_event("eve", "PROPOSE", {"proposal_text": "disable minting"})

    orchestrator.submit_event("bob", "PROPOSE", {"proposal_text": "set swap fee 0.01"})
    orchestrator.submit_event("carol", "PROPOSE", {"proposal_text": "set max inbox size 4"})

    with pytest.raises(ProposalLimitError):
        orchestrator.submit_event("dave", "PROPOSE", {"proposal_text": "set ticks per day 90"})

    assert engine.fee_vault["LUNA"] == Decimal("3000")


def test_governance_dos_blocks_project_rescue_proposal(tmp_path):
    engine, orchestrator = _new_stack(
        tmp_path,
        voting_window_ticks=20,
        max_open_proposals=3,
        max_open_per_agent=3,
    )
    engine.create_account("whale_1", luna="4000", ust="1000000")
    engine.create_account("project_0", luna="8000")

    orchestrator.submit_event(
        "whale_1",
        "PROPOSE",
        {"proposal_text": "Update protocol logo style guidelines for social media banners."},
    )
    orchestrator.submit_event(
        "whale_1",
        "PROPOSE",
        {"proposal_text": "Allocate a symbolic community meme budget with no economic impact."},
    )
    orchestrator.submit_event(
        "whale_1",
        "PROPOSE",
        {"proposal_text": "Start a low-priority ecosystem slogan contest without parameter changes."},
    )

    with pytest.raises(ProposalLimitError, match="open proposal limit reached"):
        orchestrator.submit_event(
            "project_0",
            "PROPOSE",
            {"proposal_text": "Disable minting and set swap fee to 0.01"},
        )


def test_settlement_rules_quorum_and_majority(tmp_path):
    # quorum fail
    engine1, orch1 = _new_stack(tmp_path / "q1", proposal_fee_luna="10", voting_window_ticks=1)
    engine1.create_account("a", luna="100")
    engine1.create_account("b", luna="100")
    engine1.create_account("c", luna="1000")
    p1 = orch1.submit_event("a", "PROPOSE", {"proposal_text": "disable minting"})
    orch1.submit_event("a", "VOTE", {"proposal_id": p1, "decision": "approve"})
    s1 = orch1.step_tick().governance_settlements[0]
    assert s1.status == "rejected"

    # majority fail (quorum satisfied)
    engine2, orch2 = _new_stack(tmp_path / "q2", proposal_fee_luna="10", voting_window_ticks=1)
    engine2.create_account("a", luna="100")
    engine2.create_account("b", luna="100")
    engine2.create_account("c", luna="1000")
    p2 = orch2.submit_event("c", "PROPOSE", {"proposal_text": "disable minting"})
    orch2.submit_event("a", "VOTE", {"proposal_id": p2, "decision": "approve"})
    orch2.submit_event("c", "VOTE", {"proposal_id": p2, "decision": "reject"})
    s2 = orch2.step_tick().governance_settlements[0]
    assert s2.status == "rejected"

    # pass
    engine3, orch3 = _new_stack(tmp_path / "q3", proposal_fee_luna="10", voting_window_ticks=1)
    engine3.create_account("a", luna="100")
    engine3.create_account("b", luna="100")
    engine3.create_account("c", luna="1000")
    p3 = orch3.submit_event("c", "PROPOSE", {"proposal_text": "disable minting"})
    orch3.submit_event("a", "VOTE", {"proposal_id": p3, "decision": "approve"})
    orch3.submit_event("c", "VOTE", {"proposal_id": p3, "decision": "approve"})
    s3 = orch3.step_tick().governance_settlements[0]
    assert s3.status == "passed_pending_apply"


def test_next_tick_apply_and_whitelist_compiler(tmp_path):
    engine, orchestrator = _new_stack(tmp_path, proposal_fee_luna="100", voting_window_ticks=1)
    engine.create_account("whale", luna="5000")
    engine.create_account("voter", luna="1500")

    proposal_id = orchestrator.submit_event(
        "whale",
        "PROPOSE",
        {
            "proposal_text": "disable minting, set swap fee 0.02, max inbox size 3, ticks per day 88"
        },
    )
    orchestrator.submit_event(
        "whale",
        "VOTE",
        {"proposal_id": proposal_id, "decision": "approve"},
    )
    orchestrator.submit_event(
        "voter",
        "VOTE",
        {"proposal_id": proposal_id, "decision": "approve"},
    )

    report_tick_1 = orchestrator.step_tick()
    assert report_tick_1.governance_settlements[0].status == "passed_pending_apply"
    assert engine.get_engine_config()["minting_allowed"] is True
    assert engine.get_engine_config()["swap_fee"] == Decimal("0")

    orchestrator.step_tick()
    cfg = engine.get_engine_config()
    assert cfg["minting_allowed"] is False
    assert cfg["swap_fee"] == Decimal("0.02")
    assert orchestrator.default_max_inbox_size == 3
    assert orchestrator.ticks_per_day == 88


def test_mempool_congestion_and_metrics_fields(tmp_path):
    metrics_csv = tmp_path / "metrics.csv"
    metrics = LoggerMetrics(metrics_csv)
    engine, orchestrator = _new_stack(
        tmp_path,
        proposal_fee_luna="10",
        voting_window_ticks=1,
        max_tx_per_tick=2,
        metrics=metrics,
    )
    engine.create_account("alice", ust="1000")

    for _ in range(5):
        orchestrator.submit_transaction(
            "alice",
            "SWAP",
            {
                "pool_name": "Pool_A",
                "token_in": "UST",
                "amount": "10",
                "slippage_tolerance": "0.5",
            },
            gas_price="1",
        )

    report = orchestrator.step_tick()
    assert report.mempool_processed == 2
    assert report.mempool_congestion == 3

    row = metrics.rows[-1]
    assert row.mempool_processed == 2
    assert row.mempool_congestion == 3

    pool_a = report.end_snapshot["pools"]["Pool_A"]
    ust_price = Decimal(str(pool_a["reserve_y"])) / Decimal(str(pool_a["reserve_x"]))
    assert row.peg_deviation == abs(Decimal("1") - ust_price)


def test_governance_concentration_metric_top3(tmp_path):
    metrics_csv = tmp_path / "concentration.csv"
    metrics = LoggerMetrics(metrics_csv)
    engine, orchestrator = _new_stack(
        tmp_path,
        proposal_fee_luna="100",
        voting_window_ticks=1,
        metrics=metrics,
    )
    engine.create_account("p", luna="5000")
    engine.create_account("a", luna="1000")
    engine.create_account("b", luna="800")
    engine.create_account("c", luna="500")
    engine.create_account("d", luna="200")
    engine.create_account("e", luna="100")

    proposal_id = orchestrator.submit_event(
        "p",
        "PROPOSE",
        {"proposal_text": "disable minting"},
    )
    orchestrator.submit_event("p", "VOTE", {"proposal_id": proposal_id, "decision": "approve"})
    orchestrator.submit_event("a", "VOTE", {"proposal_id": proposal_id, "decision": "approve"})
    orchestrator.submit_event("b", "VOTE", {"proposal_id": proposal_id, "decision": "approve"})
    orchestrator.submit_event("c", "VOTE", {"proposal_id": proposal_id, "decision": "approve"})
    orchestrator.submit_event("d", "VOTE", {"proposal_id": proposal_id, "decision": "abstain"})
    orchestrator.submit_event("e", "VOTE", {"proposal_id": proposal_id, "decision": "reject"})

    report = orchestrator.step_tick()
    settlement = report.governance_settlements[0]

    top3 = Decimal("4900") + Decimal("1000") + Decimal("800")
    participated = (
        Decimal("4900")
        + Decimal("1000")
        + Decimal("800")
        + Decimal("500")
        + Decimal("200")
        + Decimal("100")
    )
    expected = top3 / participated

    assert settlement.governance_concentration == expected
    assert metrics.rows[-1].governance_concentration == expected


def test_compiler_rejects_out_of_scope_patch():
    compiler = CompilerAgent()
    with pytest.raises(CompilerValidationError):
        compiler.validate_patch(
            {
                "scope": "engine",
                "parameter": "unknown_key",
                "new_value": "1",
                "reason": "hack",
            }
        )
