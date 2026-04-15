from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ace_sim.engine.ace_engine import ACE_Engine, InsufficientFundsError


def d(value: Any) -> Decimal:
    return Decimal(str(value))


def parse_snapshot_totals(snapshot: dict[str, Any]) -> dict[str, Decimal]:
    accounts = snapshot["accounts"]
    pools = snapshot["pools"]
    fee_vault = snapshot.get("fee_vault", {})

    acc_ust = sum(d(v["UST"]) for v in accounts.values())
    acc_luna = sum(d(v["LUNA"]) for v in accounts.values())
    acc_usdc = sum(d(v["USDC"]) for v in accounts.values())

    total_ust = acc_ust + d(pools["Pool_A"]["reserve_x"])
    total_luna = acc_luna + d(pools["Pool_B"]["reserve_x"])
    total_usdc = (
        acc_usdc
        + d(pools["Pool_A"]["reserve_y"])
        + d(pools["Pool_B"]["reserve_y"])
    )
    total_ust += d(fee_vault.get("UST", "0"))
    total_luna += d(fee_vault.get("LUNA", "0"))
    total_usdc += d(fee_vault.get("USDC", "0"))
    return {"UST": total_ust, "LUNA": total_luna, "USDC": total_usdc}


def parse_snapshot_expected(snapshot: dict[str, Any]) -> dict[str, Decimal]:
    genesis = {k: d(v) for k, v in snapshot["genesis_totals"].items()}
    counters = {k: d(v) for k, v in snapshot["counters"].items()}

    expected_ust = (
        genesis["UST"]
        + counters["total_ust_minted"]
        - counters["total_ust_burned_for_luna"]
    )
    expected_luna = (
        genesis["LUNA"]
        + counters["total_luna_minted"]
        - counters["total_luna_burned_for_ust"]
    )
    expected_usdc = genesis["USDC"]
    return {"UST": expected_ust, "LUNA": expected_luna, "USDC": expected_usdc}


def run_trace(
    db_path: Path,
    agents: int,
    rounds: int,
    seed: int,
    sample_interval: int,
    daily_mint_cap: Decimal | None,
) -> list[dict[str, Any]]:
    random.seed(seed)

    engine_config: dict[str, Any] = {"minting_allowed": True, "swap_fee": Decimal("0")}
    if daily_mint_cap is not None:
        engine_config["daily_mint_cap"] = daily_mint_cap

    engine = ACE_Engine(db_path=db_path, engine_config=engine_config)
    for i in range(agents):
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

    success_count = agents  # create_account actions are all successful
    failure_count = 0
    rows: list[dict[str, Any]] = []

    for step in range(1, rounds + 1):
        actor = f"agent_{random.randint(0, agents - 1)}"
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
            success_count += 1
        except (InsufficientFundsError, ValueError, PermissionError):
            failure_count += 1

        if step % sample_interval != 0 and step != rounds:
            continue

        engine.check_global_invariants()
        snap = engine.get_state_snapshot()
        totals = parse_snapshot_totals(snap)
        expected = parse_snapshot_expected(snap)
        deltas = {
            "UST": totals["UST"] - expected["UST"],
            "LUNA": totals["LUNA"] - expected["LUNA"],
            "USDC": totals["USDC"] - expected["USDC"],
        }
        max_abs_delta = max(abs(v) for v in deltas.values())

        rows.append(
            {
                "step": step,
                "success_count": success_count,
                "failure_count": failure_count,
                "oracle_price": d(snap["oracle_price_usdc_per_luna"]),
                "total_ust": totals["UST"],
                "expected_ust": expected["UST"],
                "delta_ust": deltas["UST"],
                "total_luna": totals["LUNA"],
                "expected_luna": expected["LUNA"],
                "delta_luna": deltas["LUNA"],
                "total_usdc": totals["USDC"],
                "expected_usdc": expected["USDC"],
                "delta_usdc": deltas["USDC"],
                "max_abs_delta": max_abs_delta,
            }
        )

    engine.check_global_invariants()
    engine.close()
    return rows


def write_csv(rows: list[dict[str, Any]], csv_path: Path) -> None:
    fields = [
        "step",
        "success_count",
        "failure_count",
        "oracle_price",
        "total_ust",
        "expected_ust",
        "delta_ust",
        "total_luna",
        "expected_luna",
        "delta_luna",
        "total_usdc",
        "expected_usdc",
        "delta_usdc",
        "max_abs_delta",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: str(v) for k, v in row.items()})


def write_summary(
    rows: list[dict[str, Any]],
    summary_path: Path,
    agents: int,
    rounds: int,
    seed: int,
    sample_interval: int,
) -> None:
    max_abs_delta = max((r["max_abs_delta"] for r in rows), default=Decimal("0"))
    final = rows[-1] if rows else None
    conservation_tolerance = Decimal("1e-24")
    summary = {
        "agents": agents,
        "rounds": rounds,
        "seed": seed,
        "sample_interval": sample_interval,
        "samples": len(rows),
        "max_abs_delta": str(max_abs_delta),
        "conservation_tolerance": str(conservation_tolerance),
        "final_success_count": int(final["success_count"]) if final else 0,
        "final_failure_count": int(final["failure_count"]) if final else 0,
        "final_oracle_price": str(final["oracle_price"]) if final else "0",
        "conservation_pass": max_abs_delta <= conservation_tolerance,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def plot_rows(rows: list[dict[str, Any]], plot_path: Path) -> None:
    if not rows:
        raise ValueError("no rows to plot")

    steps = [int(r["step"]) for r in rows]
    total_ust = [float(r["total_ust"]) for r in rows]
    expected_ust = [float(r["expected_ust"]) for r in rows]
    total_luna = [float(r["total_luna"]) for r in rows]
    expected_luna = [float(r["expected_luna"]) for r in rows]
    total_usdc = [float(r["total_usdc"]) for r in rows]
    expected_usdc = [float(r["expected_usdc"]) for r in rows]
    epsilon = 1e-30
    delta_ust = [max(abs(float(r["delta_ust"])), epsilon) for r in rows]
    delta_luna = [max(abs(float(r["delta_luna"])), epsilon) for r in rows]
    delta_usdc = [max(abs(float(r["delta_usdc"])), epsilon) for r in rows]

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    ax1, ax2 = axes[0]
    ax3, ax4 = axes[1]

    ax1.plot(steps, total_ust, label="total UST", linewidth=1.8)
    ax1.plot(steps, expected_ust, label="expected UST", linewidth=1.2, linestyle="--")
    ax1.set_title("UST: total vs expected")
    ax1.legend()
    ax1.grid(alpha=0.3)

    ax2.plot(steps, total_luna, label="total LUNA", linewidth=1.8)
    ax2.plot(steps, expected_luna, label="expected LUNA", linewidth=1.2, linestyle="--")
    ax2.set_title("LUNA: total vs expected")
    ax2.legend()
    ax2.grid(alpha=0.3)

    ax3.plot(steps, total_usdc, label="total USDC", linewidth=1.8)
    ax3.plot(steps, expected_usdc, label="expected USDC", linewidth=1.2, linestyle="--")
    ax3.set_title("USDC: total vs expected")
    ax3.legend()
    ax3.grid(alpha=0.3)

    ax4.plot(steps, delta_ust, label="|delta UST|", linewidth=1.5)
    ax4.plot(steps, delta_luna, label="|delta LUNA|", linewidth=1.5)
    ax4.plot(steps, delta_usdc, label="|delta USDC|", linewidth=1.5)
    ax4.set_yscale("log")
    ax4.set_title("Absolute conservation error (log scale)")
    ax4.legend()
    ax4.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate conservation trace and plot.")
    parser.add_argument("--agents", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-interval", type=int, default=10)
    parser.add_argument("--daily-mint-cap", type=str, default="1000000")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="artifacts/conservation",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = output_dir / "conservation.sqlite3"
    if db_path.exists():
        db_path.unlink()

    cap = None if args.daily_mint_cap.lower() == "none" else d(args.daily_mint_cap)
    rows = run_trace(
        db_path=db_path,
        agents=args.agents,
        rounds=args.rounds,
        seed=args.seed,
        sample_interval=args.sample_interval,
        daily_mint_cap=cap,
    )

    csv_path = output_dir / "conservation_trace.csv"
    summary_path = output_dir / "summary.json"
    plot_path = output_dir / "conservation_plot.png"

    write_csv(rows, csv_path)
    write_summary(
        rows=rows,
        summary_path=summary_path,
        agents=args.agents,
        rounds=args.rounds,
        seed=args.seed,
        sample_interval=args.sample_interval,
    )
    plot_rows(rows, plot_path)

    print(f"db: {db_path}")
    print(f"csv: {csv_path}")
    print(f"summary: {summary_path}")
    print(f"plot: {plot_path}")


if __name__ == "__main__":
    main()
