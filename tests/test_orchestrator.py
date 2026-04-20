from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.mitigation import ExecutionCircuitBreaker
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator


def new_engine(tmp_path, **kwargs) -> ACE_Engine:
    db_path = tmp_path / "orchestrator_test.sqlite3"
    return ACE_Engine(db_path=db_path, **kwargs)


def test_submit_event_pushes_fast_loop_immediately(tmp_path):
    engine = new_engine(tmp_path)
    orchestrator = Simulation_Orchestrator(engine)
    received = []
    orchestrator.register_event_subscriber(lambda event: received.append(event.event_id))

    event_id = orchestrator.submit_event(
        agent_id="speaker",
        action_type="SPEAK",
        params={"target": "forum", "message": "panic"},
    )

    assert len(orchestrator.event_bus) == 0
    assert received == [event_id]


def test_swap_slippage_is_wrapped_to_min_amount_out(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="1000")
    orchestrator = Simulation_Orchestrator(engine)

    tx_id = orchestrator.submit_transaction(
        agent_id="alice",
        action_type="SWAP",
        params={
            "pool_name": "Pool_A",
            "token_in": "UST",
            "amount": "100",
            "slippage_tolerance": "0.05",
        },
        gas_price="1",
    )

    assert len(orchestrator.mempool) == 1
    tx = orchestrator.mempool[0]
    assert tx.tx_id == tx_id
    assert tx.resolved_min_amount_out is not None
    assert tx.estimated_amount_out is not None
    assert tx.resolved_min_amount_out == tx.estimated_amount_out * Decimal("0.95")


def test_precheck_failure_does_not_charge_gas(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="100")
    orchestrator = Simulation_Orchestrator(engine)

    orchestrator.submit_transaction(
        agent_id="alice",
        action_type="SWAP",
        params={
            "pool_name": "Pool_A",
            "token_in": "UST",
            "amount": "70",
            "slippage_tolerance": "0.5",
        },
        gas_price="20",
    )
    orchestrator.submit_transaction(
        agent_id="alice",
        action_type="SWAP",
        params={
            "pool_name": "Pool_A",
            "token_in": "UST",
            "amount": "10",
            "slippage_tolerance": "0.5",
        },
        gas_price="1",
    )

    report = orchestrator.step_tick()

    assert report.receipts[0].status == "success"
    assert report.receipts[1].status == "failed"
    assert report.receipts[1].error_code == "InsufficientBalanceError"
    assert orchestrator.protocol_fee_vault["UST"] == Decimal("20")
    assert engine.get_account_balance("alice", "UST") == Decimal("10")


def test_slippage_failure_still_charges_gas(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("bob", ust="2000")
    engine.create_account("alice", ust="1000")
    orchestrator = Simulation_Orchestrator(engine)

    bob_tx = orchestrator.submit_transaction(
        agent_id="bob",
        action_type="SWAP",
        params={
            "pool_name": "Pool_A",
            "token_in": "UST",
            "amount": "700",
            "slippage_tolerance": "0.5",
        },
        gas_price="10",
    )
    alice_tx = orchestrator.submit_transaction(
        agent_id="alice",
        action_type="SWAP",
        params={
            "pool_name": "Pool_A",
            "token_in": "UST",
            "amount": "700",
            "slippage_tolerance": "0",
        },
        gas_price="1",
    )

    report = orchestrator.step_tick()
    receipts = {r.tx_id: r for r in report.receipts}

    assert receipts[bob_tx].status == "success"
    assert receipts[alice_tx].status == "failed"
    assert receipts[alice_tx].error_code == "SlippageExceededError"
    assert receipts[alice_tx].gas_paid == Decimal("1")
    assert orchestrator.protocol_fee_vault["UST"] == Decimal("11")
    assert engine.get_account_balance("alice", "UST") == Decimal("999")


def test_step_tick_orders_by_gas_then_fifo(tmp_path):
    engine = new_engine(tmp_path)
    for agent in ("alice", "bob", "carol"):
        engine.create_account(agent, ust="1000")
    orchestrator = Simulation_Orchestrator(engine)

    orchestrator.submit_transaction(
        "alice",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="5",
    )
    orchestrator.submit_transaction(
        "bob",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="10",
    )
    orchestrator.submit_transaction(
        "carol",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="10",
    )

    report = orchestrator.step_tick()
    order = [r.agent_id for r in report.receipts]
    assert order == ["bob", "carol", "alice"]


def test_simulation_tick_drives_daily_mint_cap_reset(tmp_path):
    engine = new_engine(
        tmp_path,
        engine_config={
            "minting_allowed": True,
            "swap_fee": Decimal("0"),
            "daily_mint_cap": Decimal("100"),
        },
    )
    engine.create_account("alice", ust="1000")
    orchestrator = Simulation_Orchestrator(engine, ticks_per_day=1)

    orchestrator.submit_transaction(
        "alice",
        "UST_TO_LUNA",
        {"amount_ust": "80"},
        gas_price="1",
    )
    orchestrator.submit_transaction(
        "alice",
        "UST_TO_LUNA",
        {"amount_ust": "30"},
        gas_price="1",
    )
    report_tick_1 = orchestrator.step_tick()
    assert report_tick_1.receipts[0].status == "success"
    assert report_tick_1.receipts[1].status == "failed"
    assert report_tick_1.receipts[1].error_code == "PermissionError"

    orchestrator.submit_transaction(
        "alice",
        "UST_TO_LUNA",
        {"amount_ust": "30"},
        gas_price="1",
    )
    report_tick_2 = orchestrator.step_tick()
    assert report_tick_2.receipts[0].status == "success"
    assert engine.get_simulation_clock()["current_tick"] == 2


def test_mitigation_b_crisis_caps_gas_and_logs_tx_id(tmp_path, caplog):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="60")
    mitigation_logger_name = "tests.aecb"
    mitigation = ExecutionCircuitBreaker(
        panic_threshold=Decimal("0.5"),
        crisis_gas_cap=Decimal("10"),
        logger=logging.getLogger(mitigation_logger_name),
    )
    orchestrator = Simulation_Orchestrator(engine, execution_mitigation=mitigation)
    orchestrator._last_tick_panic_word_freq = Decimal("0.9")

    tx_id = orchestrator.submit_transaction(
        "alice",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="100",
    )

    caplog.set_level("INFO", logger=mitigation_logger_name)
    report = orchestrator.step_tick()
    receipt = report.receipts[0]

    assert receipt.tx_id == tx_id
    assert receipt.status == "success"
    assert receipt.gas_bid == Decimal("100")
    assert receipt.gas_effective == Decimal("10")
    assert receipt.gas_paid == Decimal("10")
    assert orchestrator.protocol_fee_vault["UST"] == Decimal("10")
    assert engine.get_account_balance("alice", "UST") == Decimal("0")
    log_text = "\n".join(item.message for item in caplog.records)
    assert "[GAS-CAPPED]" in log_text
    assert tx_id in log_text


def test_mitigation_b_not_triggered_keeps_gas_order(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="1000")
    engine.create_account("bob", ust="1000")
    mitigation = ExecutionCircuitBreaker(panic_threshold=Decimal("0.5"), crisis_gas_cap=Decimal("10"))
    orchestrator = Simulation_Orchestrator(engine, execution_mitigation=mitigation)
    orchestrator._last_tick_panic_word_freq = Decimal("0.1")

    orchestrator.submit_transaction(
        "alice",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="5",
    )
    orchestrator.submit_transaction(
        "bob",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "50", "slippage_tolerance": "0.9"},
        gas_price="10",
    )

    report = orchestrator.step_tick()
    assert [item.agent_id for item in report.receipts] == ["bob", "alice"]
    assert [item.gas_effective for item in report.receipts] == [Decimal("10"), Decimal("5")]


def test_mitigation_b_fair_sort_prefers_retail_in_crisis(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("whale_0", ust="1000")
    engine.create_account("retail_0", ust="1000")
    mitigation = ExecutionCircuitBreaker(
        panic_threshold=Decimal("0.5"),
        crisis_gas_cap=Decimal("50"),
        gas_weight=Decimal("0.2"),
        age_weight=Decimal("0.8"),
        role_bias={
            "retail": Decimal("1.0"),
            "project": Decimal("0.6"),
            "whale": Decimal("0.2"),
        },
    )
    orchestrator = Simulation_Orchestrator(engine, execution_mitigation=mitigation)
    orchestrator.register_agent("whale_0", role="whale", community_id="c1")
    orchestrator.register_agent("retail_0", role="retail", community_id="c0")
    orchestrator._last_tick_panic_word_freq = Decimal("0.9")

    orchestrator.submit_transaction(
        "whale_0",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "10", "slippage_tolerance": "0.9"},
        gas_price="10",
    )
    orchestrator.submit_transaction(
        "retail_0",
        "SWAP",
        {"pool_name": "Pool_A", "token_in": "UST", "amount": "10", "slippage_tolerance": "0.9"},
        gas_price="10",
    )

    report = orchestrator.step_tick()
    assert [item.agent_id for item in report.receipts] == ["retail_0", "whale_0"]
