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

from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator


def d(value: Any) -> Decimal:
    return Decimal(str(value))


def seeded_accounts_setup(orchestrator: Simulation_Orchestrator, num_retail: int) -> None:
    engine = orchestrator.engine
    engine.create_account("whale", ust="200000", luna="10000", usdc="200000")
    for i in range(num_retail):
        engine.create_account(
            f"retail_{i}",
            ust="3000",
            luna="300",
            usdc="3000",
        )


def submit_tick_workload(orchestrator: Simulation_Orchestrator, tick_idx: int, num_retail: int) -> None:
    # Fast loop semantic events
    speaker = f"retail_{tick_idx % num_retail}"
    orchestrator.submit_event(
        agent_id=speaker,
        action_type="SPEAK",
        params={"target": "forum", "message": f"tick-{tick_idx}-market update"},
    )
    if tick_idx % 7 == 0:
        orchestrator.submit_event(
            agent_id=speaker,
            action_type="SPEAK",
            params={"target": "forum", "message": f"governance sentiment tick-{tick_idx}"},
        )

    # Deterministic MEV pair:
    # high-gas whale first (loose slippage), low-gas retail second (tight slippage -> often fails)
    orchestrator.submit_transaction(
        agent_id="whale",
        action_type="SWAP",
        params={
            "pool_name": "Pool_A",
            "token_in": "UST",
            "amount": "800",
            "slippage_tolerance": "0.9",
        },
        gas_price="35",
    )
    orchestrator.submit_transaction(
        agent_id=speaker,
        action_type="SWAP",
        params={
            "pool_name": "Pool_A",
            "token_in": "UST",
            "amount": "700",
            "slippage_tolerance": "0",
        },
        gas_price="2",
    )

    # Additional random economic actions
    for _ in range(3):
        agent = f"retail_{random.randint(0, num_retail - 1)}"
        action = random.choice(["SWAP", "UST_TO_LUNA", "LUNA_TO_UST"])
        gas = Decimal(random.randint(0, 12))
        if action == "SWAP":
            pool_name = random.choice(["Pool_A", "Pool_B"])
            token_in = "UST" if pool_name == "Pool_A" else random.choice(["LUNA", "USDC"])
            orchestrator.submit_transaction(
                agent_id=agent,
                action_type="SWAP",
                params={
                    "pool_name": pool_name,
                    "token_in": token_in,
                    "amount": str(random.randint(10, 120)),
                    "slippage_tolerance": str(Decimal(random.choice(["0.05", "0.1", "0.2"]))),
                },
                gas_price=gas,
            )
        elif action == "UST_TO_LUNA":
            orchestrator.submit_transaction(
                agent_id=agent,
                action_type="UST_TO_LUNA",
                params={"amount_ust": str(random.randint(10, 100))},
                gas_price=gas,
            )
        else:
            orchestrator.submit_transaction(
                agent_id=agent,
                action_type="LUNA_TO_UST",
                params={"amount_luna": str(random.randint(1, 30))},
                gas_price=gas,
            )


def ordering_check_from_batch(batch_meta: list[tuple[str, Decimal, int]], receipt_ids: list[str]) -> bool:
    expected = [tx_id for tx_id, _, _ in sorted(batch_meta, key=lambda x: (-x[1], x[2]))]
    return expected == receipt_ids


def run_trace(
    output_db: Path,
    ticks: int,
    num_retail: int,
    seed: int,
    ticks_per_day: int,
) -> list[dict[str, Any]]:
    random.seed(seed)
    if output_db.exists():
        output_db.unlink()

    engine = ACE_Engine(db_path=output_db)
    orchestrator = Simulation_Orchestrator(engine=engine, ticks_per_day=ticks_per_day)
    seeded_accounts_setup(orchestrator, num_retail=num_retail)

    rows: list[dict[str, Any]] = []
    events_seen = 0

    for tick in range(1, ticks + 1):
        submit_tick_workload(orchestrator, tick_idx=tick, num_retail=num_retail)

        # Capture batch meta before step, for ordering verification.
        batch_meta = [(tx.tx_id, tx.gas_price, tx.enqueue_seq) for tx in orchestrator.mempool]
        report = orchestrator.step_tick()

        receipt_ids = [r.tx_id for r in report.receipts]
        ordering_pass = ordering_check_from_batch(batch_meta, receipt_ids)

        success_count = sum(1 for r in report.receipts if r.status == "success")
        failed_count = sum(1 for r in report.receipts if r.status == "failed")
        fatal_count = sum(1 for r in report.receipts if r.status == "fatal")
        slippage_fail_count = sum(
            1 for r in report.receipts if r.error_code == "SlippageExceededError"
        )

        tick_gas_ust = sum(
            r.gas_paid for r in report.receipts if r.gas_token == "UST" and r.gas_paid > 0
        )
        tick_gas_luna = sum(
            r.gas_paid for r in report.receipts if r.gas_token == "LUNA" and r.gas_paid > 0
        )
        tick_gas_usdc = sum(
            r.gas_paid for r in report.receipts if r.gas_token == "USDC" and r.gas_paid > 0
        )

        events_this_tick = len(orchestrator.event_bus)
        events_seen += events_this_tick
        orchestrator.event_bus.clear()

        rows.append(
            {
                "tick": tick,
                "tx_count": len(report.receipts),
                "success_count": success_count,
                "failed_count": failed_count,
                "fatal_count": fatal_count,
                "slippage_fail_count": slippage_fail_count,
                "ordering_pass": int(ordering_pass),
                "oracle_price": d(report.end_snapshot["oracle_price_usdc_per_luna"]),
                "tick_gas_ust": tick_gas_ust,
                "tick_gas_luna": tick_gas_luna,
                "tick_gas_usdc": tick_gas_usdc,
                "vault_ust": report.fee_vault_snapshot["UST"],
                "vault_luna": report.fee_vault_snapshot["LUNA"],
                "vault_usdc": report.fee_vault_snapshot["USDC"],
                "events_seen_this_tick": events_this_tick,
                "events_seen_total": events_seen,
            }
        )

    engine.check_global_invariants()
    engine.close()
    return rows


def write_csv(rows: list[dict[str, Any]], csv_path: Path) -> None:
    fields = [
        "tick",
        "tx_count",
        "success_count",
        "failed_count",
        "fatal_count",
        "slippage_fail_count",
        "ordering_pass",
        "oracle_price",
        "tick_gas_ust",
        "tick_gas_luna",
        "tick_gas_usdc",
        "vault_ust",
        "vault_luna",
        "vault_usdc",
        "events_seen_this_tick",
        "events_seen_total",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: str(v) for k, v in row.items()})


def write_summary(
    rows: list[dict[str, Any]],
    summary_path: Path,
    ticks: int,
    num_retail: int,
    seed: int,
    ticks_per_day: int,
) -> None:
    ordering_pass_rate = (
        sum(r["ordering_pass"] for r in rows) / len(rows) if rows else Decimal("0")
    )
    summary = {
        "ticks": ticks,
        "num_retail": num_retail,
        "seed": seed,
        "ticks_per_day": ticks_per_day,
        "total_txs": int(sum(r["tx_count"] for r in rows)),
        "total_success": int(sum(r["success_count"] for r in rows)),
        "total_failed": int(sum(r["failed_count"] for r in rows)),
        "total_slippage_failed": int(sum(r["slippage_fail_count"] for r in rows)),
        "ordering_pass_rate": float(ordering_pass_rate),
        "final_vault": {
            "UST": str(rows[-1]["vault_ust"]) if rows else "0",
            "LUNA": str(rows[-1]["vault_luna"]) if rows else "0",
            "USDC": str(rows[-1]["vault_usdc"]) if rows else "0",
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def plot_rows(rows: list[dict[str, Any]], plot_path: Path) -> None:
    if not rows:
        raise ValueError("no rows to plot")

    ticks = [int(r["tick"]) for r in rows]
    tx_count = [int(r["tx_count"]) for r in rows]
    success_count = [int(r["success_count"]) for r in rows]
    failed_count = [int(r["failed_count"]) for r in rows]
    slippage_fail_count = [int(r["slippage_fail_count"]) for r in rows]
    ordering_pass = [int(r["ordering_pass"]) for r in rows]

    oracle_price = [float(r["oracle_price"]) for r in rows]
    vault_ust = [float(r["vault_ust"]) for r in rows]
    vault_luna = [float(r["vault_luna"]) for r in rows]
    vault_usdc = [float(r["vault_usdc"]) for r in rows]

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    ax1, ax2 = axes[0]
    ax3, ax4 = axes[1]

    ax1.plot(ticks, tx_count, label="tx count", linewidth=1.6)
    ax1.plot(ticks, success_count, label="success", linewidth=1.4)
    ax1.plot(ticks, failed_count, label="failed", linewidth=1.4)
    ax1.plot(ticks, slippage_fail_count, label="slippage failed", linewidth=1.4)
    ax1.set_title("Tick settlement outcomes")
    ax1.legend()
    ax1.grid(alpha=0.3)

    ax2.plot(ticks, oracle_price, color="#2f7ed8", linewidth=1.8)
    ax2.set_title("Oracle price (USDC/LUNA)")
    ax2.grid(alpha=0.3)

    ax3.plot(ticks, vault_ust, label="vault UST", linewidth=1.6)
    ax3.plot(ticks, vault_luna, label="vault LUNA", linewidth=1.6)
    ax3.plot(ticks, vault_usdc, label="vault USDC", linewidth=1.6)
    ax3.set_title("Protocol fee vault accumulation")
    ax3.legend()
    ax3.grid(alpha=0.3)

    ax4.plot(ticks, ordering_pass, label="ordering pass (1=pass)", linewidth=1.8)
    ax4.set_ylim(-0.05, 1.05)
    ax4.set_title("Gas ordering check per tick")
    ax4.legend()
    ax4.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase2 orchestrator visualization")
    parser.add_argument("--ticks", type=int, default=80)
    parser.add_argument("--num-retail", type=int, default=20)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--ticks-per-day", type=int, default=100)
    parser.add_argument("--output-dir", type=str, default="artifacts/phase2")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = output_dir / "phase2_trace.sqlite3"
    csv_path = output_dir / "phase2_trace.csv"
    summary_path = output_dir / "phase2_summary.json"
    plot_path = output_dir / "phase2_plot.png"

    rows = run_trace(
        output_db=db_path,
        ticks=args.ticks,
        num_retail=args.num_retail,
        seed=args.seed,
        ticks_per_day=args.ticks_per_day,
    )
    write_csv(rows, csv_path)
    write_summary(
        rows=rows,
        summary_path=summary_path,
        ticks=args.ticks,
        num_retail=args.num_retail,
        seed=args.seed,
        ticks_per_day=args.ticks_per_day,
    )
    plot_rows(rows, plot_path)

    print(f"db: {db_path}")
    print(f"csv: {csv_path}")
    print(f"summary: {summary_path}")
    print(f"plot: {plot_path}")


if __name__ == "__main__":
    main()
