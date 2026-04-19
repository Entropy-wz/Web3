from __future__ import annotations

import importlib.util
import logging
from decimal import Decimal
from pathlib import Path

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
