from __future__ import annotations

import importlib.util
import json
import logging
from decimal import Decimal
from pathlib import Path
import sqlite3

from ace_sim.agents.agent_profile import build_luna_crash_bootstrap


def _load_visualizer_module():
    repo_root = Path(__file__).resolve().parents[1]
    target = repo_root / "scripts" / "visualization" / "phase5_governance_visualizer.py"
    spec = importlib.util.spec_from_file_location("phase5_visualizer", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VIS = _load_visualizer_module()


def test_parse_pool_reserves_supports_common_separators():
    assert VIS._parse_pool_reserves("10000000,10000000") == (
        Decimal("10000000"),
        Decimal("10000000"),
    )
    assert VIS._parse_pool_reserves("10000000:9000000") == (
        Decimal("10000000"),
        Decimal("9000000"),
    )
    assert VIS._parse_pool_reserves("10000000 8000000") == (
        Decimal("10000000"),
        Decimal("8000000"),
    )


def test_staircase_black_swan_schedule_ticks_and_amounts():
    schedule = VIS._build_black_swan_schedule(
        scenario=VIS.SCENARIO_DEFAULT,
        enabled=True,
        shock_t1=Decimal("1000000"),
        shock_t3=Decimal("500000"),
        shock_t6=Decimal("300000"),
    )
    assert sorted(schedule.keys()) == [1, 3, 6]
    assert schedule[1][0]["agent_id"] == "whale_0"
    assert str(schedule[1][0]["params"]["amount"]) == "1000000"
    assert schedule[3][0]["agent_id"] == "whale_0"
    assert str(schedule[3][0]["params"]["amount"]) == "500000"
    assert schedule[6][0]["agent_id"] == "whale_1"
    assert str(schedule[6][0]["params"]["amount"]) == "300000"


def test_apply_retail_ust_cap_scales_only_retail():
    bootstrap = build_luna_crash_bootstrap(retail_count=21)
    by_id_before = {item.agent_id: (item.initial_ust, item.initial_usdc) for item in bootstrap}

    info = VIS._apply_retail_ust_cap(
        bootstrap=bootstrap,
        cap=Decimal("100000"),
        logger=logging.getLogger("test.phase5"),
    )
    assert info["scaled"] is True

    retail_total = sum(
        (item.initial_ust for item in bootstrap if item.role == "retail"),
        Decimal("0"),
    )
    assert retail_total <= Decimal("100000")

    by_id_after = {item.agent_id: (item.initial_ust, item.initial_usdc) for item in bootstrap}
    assert by_id_before["whale_0"] == by_id_after["whale_0"]
    assert by_id_before["whale_1"] == by_id_after["whale_1"]
    assert by_id_before["project_0"] == by_id_after["project_0"]


def test_curve_quality_flags_early_near_zero():
    ust_price_by_tick = {
        1: Decimal("0.82"),
        3: Decimal("0.71"),
        6: Decimal("0.55"),
        10: Decimal("0.21"),
        12: VIS.EARLY_NEAR_ZERO_PRICE / Decimal("2"),
    }
    quality = VIS._evaluate_curve_quality(ust_price_by_tick, total_ticks=20)
    assert quality["early_near_zero"] is True
    assert quality["early_near_zero_first_tick"] == 12
    assert "1" in quality["key_ticks"]
    assert "6" in quality["key_ticks"]
    assert "20" not in quality["key_ticks"]


def test_overload_count_aligns_settlement_tick_to_previous_read_tick(tmp_path: Path):
    db_path = tmp_path / "overload.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE inbox_overload_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                tick INTEGER NOT NULL,
                total_pending INTEGER NOT NULL,
                returned_count INTEGER NOT NULL,
                dropped_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO inbox_overload_log (
                agent_id, tick, total_pending, returned_count, dropped_count, created_at
            ) VALUES ('retail_0', 2, 8, 1, 7, '2026-01-01T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO inbox_overload_log (
                agent_id, tick, total_pending, returned_count, dropped_count, created_at
            ) VALUES ('retail_1', 2, 9, 1, 8, '2026-01-01T00:00:01Z')
            """
        )
        conn.commit()

        # settlement tick 3 should read overload rows from read tick 2
        assert VIS._read_tick_for_settlement_tick(3) == 2
        assert VIS._count_overload_people_for_settlement_tick(conn, 3) == 2

        # settlement tick 1 maps to read tick 0, and no overload rows exist there
        assert VIS._read_tick_for_settlement_tick(1) == 0
        assert VIS._count_overload_people_for_settlement_tick(conn, 1) == 0
    finally:
        conn.close()


def test_apply_prompt_profile_overrides_updates_target_agent_only(tmp_path: Path):
    bootstrap = build_luna_crash_bootstrap(retail_count=21)
    by_id = {item.agent_id: item for item in bootstrap}
    whale_0_before = by_id["whale_0"].profile.strategy_prompt

    profile_path = tmp_path / "prompt_profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "version": "1",
                "overrides": {
                    "whale_1": {
                        "strategy_prompt": "ECLIPSE: spread panic and dump.",
                        "social_policy": "Use extreme FUD in forum.",
                        "hidden_goals": ["jam retail execution with congestion"],
                    },
                    "ghost_agent": {
                        "strategy_prompt": "should be ignored",
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    report = VIS.apply_prompt_profile_overrides(
        bootstrap=bootstrap,
        prompt_profile_path=profile_path,
        logger=logging.getLogger("test.phase5.prompt"),
    )

    assert report["enabled"] is True
    assert "whale_1" in report["applied_agents"]
    assert "ghost_agent" in report["ignored_agents"]
    assert by_id["whale_1"].profile.strategy_prompt == "ECLIPSE: spread panic and dump."
    assert by_id["whale_1"].profile.social_policy == "Use extreme FUD in forum."
    assert by_id["whale_1"].profile.hidden_goals == ["jam retail execution with congestion"]
    assert by_id["whale_0"].profile.strategy_prompt == whale_0_before


def test_generate_social_eclipse_comparison_contains_asymmetry_fields(tmp_path: Path):
    baseline_dir = tmp_path / "baseline"
    attack_dir = tmp_path / "attack"
    out_dir = tmp_path / "paper_data"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    attack_dir.mkdir(parents=True, exist_ok=True)

    baseline_summary = {
        "social_eclipse": {
            "window_start_tick": 1,
            "window_end_tick": 5,
            "attacker_tx_success_rate_window": "0.20",
            "retail_tx_success_rate_window": "0.40",
            "avg_gas_paid_attacker_window": "2",
            "avg_gas_paid_retail_window": "3",
            "max_gas_bid_in_window": "8",
            "max_gas_bid_attacker_in_window": "6",
            "max_gas_bid_retail_in_window": "8",
        }
    }
    attack_summary = {
        "social_eclipse": {
            "window_start_tick": 1,
            "window_end_tick": 5,
            "attacker_id": "whale_1",
            "triggered": True,
            "attacker_tx_success_rate_window": "1.0",
            "retail_tx_success_rate_window": "0.1",
            "avg_gas_paid_attacker_window": "12",
            "avg_gas_paid_retail_window": "4",
            "max_gas_bid_in_window": "20",
            "max_gas_bid_attacker_in_window": "20",
            "max_gas_bid_retail_in_window": "11",
        }
    }
    (baseline_dir / "summary.json").write_text(
        json.dumps(baseline_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (attack_dir / "summary.json").write_text(
        json.dumps(attack_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (baseline_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        f.write("tick,tx_failed,mempool_congestion\n1,2,20\n2,3,30\n3,2,25\n4,4,35\n5,3,32\n")
    with (attack_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as f:
        f.write("tick,tx_failed,mempool_congestion\n1,5,60\n2,7,80\n3,6,75\n4,8,90\n5,7,88\n")

    outputs = VIS.generate_social_eclipse_comparison(
        baseline_dir=baseline_dir,
        attack_dir=attack_dir,
        output_dir=out_dir,
        logger=logging.getLogger("test.phase5.compare"),
    )
    assert outputs["csv"].exists()
    assert outputs["json"].exists()

    payload = json.loads(outputs["json"].read_text(encoding="utf-8"))
    metrics = payload["metrics"]
    assert "attacker_tx_success_rate_window" in metrics
    assert "retail_tx_success_rate_window" in metrics
    assert "avg_gas_paid_attacker_window" in metrics
    assert "avg_gas_paid_retail_window" in metrics
    assert "max_gas_bid_in_window" in metrics
    assert "max_gas_bid_attacker_in_window" in metrics
    assert "max_gas_bid_retail_in_window" in metrics
