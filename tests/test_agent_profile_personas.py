from __future__ import annotations

from collections import Counter

import pytest

from ace_sim.agents.agent_profile import (
    build_luna_crash_bootstrap,
    default_black_swan_tick0_actions,
)


def test_luna_crash_bootstrap_role_counts_and_models():
    cohort = build_luna_crash_bootstrap(retail_count=21)
    assert len(cohort) == 24

    role_counts = Counter(item.role for item in cohort)
    assert role_counts["project"] == 1
    assert role_counts["whale"] == 2
    assert role_counts["retail"] == 21

    by_id = {item.agent_id: item for item in cohort}
    assert by_id["project_0"].profile.llm_model == "gpt-4o"
    assert by_id["whale_0"].profile.llm_model == "gpt-4o"
    assert by_id["whale_1"].profile.llm_model == "gpt-4o"

    retail_models = {
        item.profile.llm_model for item in cohort if item.role == "retail"
    }
    assert retail_models == {"gpt-4o-mini"}


def test_luna_crash_bootstrap_retail_split_442():
    cohort = build_luna_crash_bootstrap(retail_count=21)
    retail_persona = Counter(
        item.profile.persona_type for item in cohort if item.role == "retail"
    )

    assert retail_persona["retail_panic_prone"] == 8
    assert retail_persona["retail_follower"] == 8
    assert retail_persona["retail_lunatic"] == 5


def test_luna_crash_bootstrap_key_allocations():
    cohort = build_luna_crash_bootstrap(retail_count=21)
    by_id = {item.agent_id: item for item in cohort}

    assert str(by_id["project_0"].initial_ust) == "10000000"
    assert str(by_id["project_0"].initial_usdc) == "100000000"
    assert str(by_id["project_0"].initial_luna) == "10000000"

    assert str(by_id["whale_0"].initial_ust) == "50000000"
    assert str(by_id["whale_0"].initial_usdc) == "20000000"
    assert str(by_id["whale_1"].initial_ust) == "20000000"
    assert str(by_id["whale_1"].initial_usdc) == "50000000"


def test_whale_1_profile_contains_governance_dos_intent():
    cohort = build_luna_crash_bootstrap(retail_count=21)
    by_id = {item.agent_id: item for item in cohort}
    whale_1 = by_id["whale_1"].profile

    policy = str(whale_1.governance_policy).lower()
    strategy = str(whale_1.strategy_prompt).lower()
    goals = " ".join(str(g).lower() for g in whale_1.hidden_goals)

    assert "placeholder" in policy
    assert "jam governance" in strategy
    assert "occupying governance proposal slots" in goals


def test_black_swan_tick0_actions_shape():
    actions = default_black_swan_tick0_actions()
    assert len(actions) == 2
    assert actions[0]["kind"] == "transaction"
    assert actions[0]["agent_id"] == "whale_0"
    assert actions[0]["action_type"] == "SWAP"
    assert str(actions[0]["params"]["amount"]) == "30000000"
    assert actions[1]["kind"] == "event"
    assert actions[1]["action_type"] == "SPEAK"


def test_retail_count_guardrail():
    with pytest.raises(ValueError):
        build_luna_crash_bootstrap(retail_count=20)
    with pytest.raises(ValueError):
        build_luna_crash_bootstrap(retail_count=28)
