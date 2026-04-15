from __future__ import annotations

import random
from decimal import Decimal

import pytest

from ace_sim.engine.ace_engine import ACE_Engine, InsufficientFundsError


def new_engine(tmp_path, **kwargs) -> ACE_Engine:
    db_path = tmp_path / "ace_engine_test.sqlite3"
    return ACE_Engine(db_path=db_path, **kwargs)


def test_create_account_and_balances(tmp_path):
    engine = new_engine(tmp_path)
    alice = engine.create_account("alice", ust="1000", luna="10", usdc="500")

    assert alice.address == "alice"
    assert alice.UST == Decimal("1000")
    assert alice.LUNA == Decimal("10")
    assert alice.USDC == Decimal("500")
    assert engine.accounts["alice"].UST == Decimal("1000")

    totals = engine.get_token_totals()
    assert totals["UST"] > 0
    assert totals["LUNA"] > 0
    assert totals["USDC"] > 0


def test_swap_pool_a_and_slippage(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="2000", usdc="1000")
    totals_before = engine.get_token_totals()

    result = engine.swap("alice", "Pool_A", "UST", "100")

    assert result["token_out"] == "USDC"
    assert Decimal(result["amount_out"]) > 0
    assert Decimal(result["slippage"]) >= 0

    totals_after = engine.get_token_totals()
    assert totals_after == totals_before


def test_swap_pool_b_and_slippage(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("trader", luna="100", usdc="1000")
    totals_before = engine.get_token_totals()

    result = engine.swap("trader", "Pool_B", "LUNA", "10")

    assert result["token_out"] == "USDC"
    assert Decimal(result["amount_out"]) > 0
    assert Decimal(result["slippage"]) >= 0

    totals_after = engine.get_token_totals()
    assert totals_after == totals_before


def test_insufficient_funds_raises_and_is_logged(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="1")

    with pytest.raises(InsufficientFundsError):
        engine.swap("alice", "Pool_A", "UST", "5")

    stats = engine.get_ledger_success_failure()
    assert stats["failure"] >= 1


def test_oracle_is_endogenous_to_pool_b(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="1000")
    engine.create_account("bob", usdc="50000")

    price_before = engine.get_oracle_price()
    mint_before = engine.ust_to_luna("alice", "100")
    luna_minted_before = Decimal(mint_before["luna_minted"])

    # Buy LUNA with USDC in Pool_B, which raises USDC/LUNA oracle price.
    engine.swap("bob", "Pool_B", "USDC", "25000")

    price_after = engine.get_oracle_price()
    mint_after = engine.ust_to_luna("alice", "100")
    luna_minted_after = Decimal(mint_after["luna_minted"])

    assert price_after > price_before
    assert luna_minted_after < luna_minted_before


def test_bidirectional_mint_burn_roundtrip(tmp_path):
    engine = new_engine(tmp_path)
    engine.create_account("alice", ust="1000")

    minted = engine.ust_to_luna("alice", "120")
    minted_luna = Decimal(minted["luna_minted"])

    redeemed = engine.luna_to_ust("alice", minted_luna)
    redeemed_ust = Decimal(redeemed["ust_minted"])

    assert redeemed_ust == Decimal("120")


def test_triangle_arbitrage_path_exists(tmp_path):
    engine = new_engine(
        tmp_path,
        pool_a_reserves=("1000000", "800000"),  # UST de-pegged to 0.8 USDC
        pool_b_reserves=("1000000", "1000000"),  # LUNA at 1 USDC
    )
    engine.create_account("arb", usdc="2000")

    start_usdc = engine.accounts["arb"].USDC
    buy_ust = engine.swap("arb", "Pool_A", "USDC", "800")
    ust_bought = Decimal(buy_ust["amount_out"])

    minted = engine.ust_to_luna("arb", ust_bought)
    luna_minted = Decimal(minted["luna_minted"])

    engine.swap("arb", "Pool_B", "LUNA", luna_minted)
    end_usdc = engine.accounts["arb"].USDC

    assert end_usdc > start_usdc
    assert engine.get_oracle_price() < Decimal("1")


def test_minting_disabled_blocks_both_directions(tmp_path):
    engine = new_engine(
        tmp_path,
        engine_config={"minting_allowed": False, "swap_fee": Decimal("0.0")},
    )
    engine.create_account("alice", ust="1000", luna="100")

    with pytest.raises(PermissionError):
        engine.ust_to_luna("alice", "10")

    with pytest.raises(PermissionError):
        engine.luna_to_ust("alice", "10")


def test_daily_mint_cap_and_config_update(tmp_path):
    engine = new_engine(
        tmp_path,
        engine_config={
            "minting_allowed": True,
            "swap_fee": Decimal("0.0"),
            "daily_mint_cap": Decimal("1000"),
        },
    )
    engine.create_account("alice", ust="5000")

    engine.ust_to_luna("alice", "700")
    with pytest.raises(PermissionError):
        engine.ust_to_luna("alice", "400")

    updated = engine.update_engine_config({"daily_mint_cap": "2000"})
    assert updated["daily_mint_cap"] == Decimal("2000")

    engine.ust_to_luna("alice", "400")


def test_extreme_numbers_low_price_and_huge_supply(tmp_path):
    engine = new_engine(
        tmp_path,
        pool_a_reserves=("1000000", "1000000"),
        pool_b_reserves=("1e12", "1e6"),  # oracle price = 0.000001 USDC/LUNA
        engine_config={"daily_mint_cap": Decimal("1e15")},
    )
    engine.create_account("whale", ust="1e12")

    price = engine.get_oracle_price()
    assert price == Decimal("0.000001")

    minted = engine.ust_to_luna("whale", "1e12")
    luna_minted = Decimal(minted["luna_minted"])
    assert luna_minted == Decimal("1e18")

    assert engine.total_luna_supply >= Decimal("1e18")
    engine.check_global_invariants()


def test_stress_100_accounts_1000_rounds(tmp_path):
    random.seed(42)
    engine = new_engine(tmp_path)

    for i in range(100):
        engine.create_account(
            f"agent_{i}",
            ust=Decimal("10000"),
            luna=Decimal("100"),
            usdc=Decimal("10000"),
        )

    actions = [
        "swap_a_ust",
        "swap_a_usdc",
        "swap_b_luna",
        "swap_b_usdc",
        "ust_to_luna",
        "luna_to_ust",
    ]

    attempts = 1000
    for i in range(attempts):
        actor = f"agent_{random.randint(0, 99)}"
        amount = Decimal(random.randint(1, 200))
        action = random.choice(actions)

        try:
            if action == "swap_a_ust":
                engine.swap(actor, "Pool_A", "UST", amount)
            elif action == "swap_a_usdc":
                engine.swap(actor, "Pool_A", "USDC", amount)
            elif action == "swap_b_luna":
                engine.swap(actor, "Pool_B", "LUNA", amount)
            elif action == "swap_b_usdc":
                engine.swap(actor, "Pool_B", "USDC", amount)
            elif action == "ust_to_luna":
                engine.ust_to_luna(actor, amount)
            else:
                engine.luna_to_ust(actor, amount)
        except (InsufficientFundsError, ValueError, PermissionError):
            pass

        if i % 50 == 0:
            engine.check_global_invariants()

    engine.check_global_invariants()

    expected_ledger_rows = 100 + attempts
    assert engine.get_ledger_count() == expected_ledger_rows

    stats = engine.get_ledger_success_failure()
    assert stats["success"] + stats["failure"] == expected_ledger_rows
