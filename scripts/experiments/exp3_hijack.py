from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
VIS = ROOT / "scripts" / "visualization" / "phase5_governance_visualizer.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run governance hijack matrix (baseline vs hijack).")
    parser.add_argument("--seeds", type=str, default="42,77,101,131")
    parser.add_argument("--ticks", type=int, default=40)
    parser.add_argument("--retail", type=int, default=24)
    parser.add_argument("--scenario", type=str, default="staircase_formal_run")
    parser.add_argument("--traffic-profile", type=str, default="stress")
    parser.add_argument("--voting-window-ticks", type=int, default=8)
    parser.add_argument("--output-root", type=str, default="artifacts/paper_runs/exp3")
    parser.add_argument("--paper-output", type=str, default="paper/data")
    parser.add_argument("--prompt-profile-path", type=str, default="configs/prompt_profiles/whale_eclipse_extreme.json")
    parser.add_argument("--hijack-attacker-id", type=str, default="whale_1")
    parser.add_argument("--hijack-attacker-luna", type=str, default="4000")
    parser.add_argument(
        "--hijack-proposal-text",
        type=str,
        default="Lower swap fee to 0.0001 to maximize holder returns",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offline-rules", action="store_true")
    return parser.parse_args()


def parse_seed_list(raw: str) -> list[int]:
    items = [x.strip() for x in str(raw).split(",")]
    values = [int(x) for x in items if x]
    seen: set[int] = set()
    out: list[int] = []
    for v in values:
        if v in seen:
            continue
        out.append(v)
        seen.add(v)
    return out


def run_cmd(args: list[str], *, dry_run: bool) -> None:
    cmd = [sys.executable, str(VIS), *args]
    print("[RUN]", " ".join(cmd))
    if dry_run:
        return
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: returncode={result.returncode}")


def load_summary(run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"summary not found: {summary_path}")
    return json.loads(summary_path.read_text(encoding="utf-8"))


def max_peg_deviation(metrics_path: Path) -> Decimal:
    max_val = Decimal("0")
    with metrics_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            value = Decimal(str(row.get("peg_deviation", "0")))
            if value > max_val:
                max_val = value
    return max_val


def checkpoint_tvl_and_fee(checkpoint_dir: Path) -> tuple[Decimal, Decimal, str]:
    files = sorted(checkpoint_dir.glob("tick_*.json"))
    if not files:
        return Decimal("0"), Decimal("0"), ""

    min_tvl: Decimal | None = None
    final_tvl = Decimal("0")
    swap_fee_end = ""
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshot = payload.get("engine_snapshot", payload)
        pools = snapshot.get("pools", {})
        pool_a = pools.get("Pool_A", {})
        pool_b = pools.get("Pool_B", {})
        # USDC side TVL proxy for paper comparison
        tvl = Decimal(str(pool_a.get("reserve_y", "0"))) + Decimal(str(pool_b.get("reserve_y", "0")))
        final_tvl = tvl
        swap_fee_end = str(snapshot.get("engine_config", {}).get("swap_fee", swap_fee_end))
        if min_tvl is None or tvl < min_tvl:
            min_tvl = tvl
    return final_tvl, min_tvl or final_tvl, swap_fee_end


def governance_concentration_max(summary: dict[str, Any]) -> Decimal:
    proposals = summary.get("governance", {}).get("proposals", [])
    max_val = Decimal("0")
    for p in proposals:
        value = Decimal(str(p.get("governance_concentration", "0")))
        if value > max_val:
            max_val = value
    return max_val


def main() -> None:
    args = parse_args()
    seeds = parse_seed_list(args.seeds)
    if not seeds:
        raise ValueError("no valid seeds provided")

    output_root = Path(args.output_root).resolve()
    paper_output = Path(args.paper_output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    paper_output.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for seed in seeds:
        common = [
            "--ticks",
            str(args.ticks),
            "--retail",
            str(args.retail),
            "--scenario",
            str(args.scenario),
            "--seed",
            str(seed),
            "--traffic-profile",
            str(args.traffic_profile),
            "--voting-window-ticks",
            str(args.voting_window_ticks),
            "--social-eclipse-attack",
            "--eclipse-attacker-id",
            "whale_1",
            "--eclipse-trigger-tick",
            "1",
            "--eclipse-window-ticks",
            "5",
            "--eclipse-sell-ust",
            "150000",
            "--prompt-profile-path",
            str(args.prompt_profile_path),
            "--no-paper-charts",
        ]
        if args.offline_rules:
            common.append("--offline-rules")

        cases = [
            ("baseline", []),
            (
                "hijack",
                [
                    "--governance-hijack-attack",
                    "--hijack-attacker-id",
                    str(args.hijack_attacker_id),
                    "--hijack-trigger-tick",
                    "2",
                    "--hijack-force-approve",
                    "--hijack-attacker-luna",
                    str(args.hijack_attacker_luna),
                    "--hijack-proposal-text",
                    str(args.hijack_proposal_text),
                ],
            ),
        ]

        for label, extra in cases:
            run_dir = output_root / f"s{seed}_{label}"
            run_args = [*common, *extra, "--output-dir", str(run_dir)]
            run_cmd(run_args, dry_run=bool(args.dry_run))
            if args.dry_run:
                continue
            summary = load_summary(run_dir)
            final_tvl, min_tvl, swap_fee_end = checkpoint_tvl_and_fee(run_dir / "checkpoints")
            row = {
                "seed": seed,
                "run_type": label,
                "output_dir": str(run_dir),
                "hijack_enabled": bool(summary.get("governance_hijack", {}).get("enabled", False)),
                "hijack_proposal_submitted": bool(
                    summary.get("governance_hijack", {}).get("proposal_submitted", False)
                ),
                "hijack_proposal_rejected": bool(
                    summary.get("governance_hijack", {}).get("proposal_rejected", False)
                ),
                "hijack_votes_submitted": int(
                    summary.get("governance_hijack", {}).get("votes_submitted", 0)
                ),
                "swap_fee_end": str(swap_fee_end),
                "governance_concentration_max": str(governance_concentration_max(summary)),
                "peg_deviation_max": str(
                    max_peg_deviation(Path(summary.get("metrics_csv", run_dir / "metrics.csv")))
                ),
                "final_tvl_usdc_proxy": str(final_tvl),
                "min_tvl_usdc_proxy": str(min_tvl),
            }
            rows.append(row)

    if args.dry_run:
        print("[DONE] dry-run only.")
        return

    csv_path = paper_output / "exp3_results.csv"
    json_path = paper_output / "exp3_results.json"
    fields = [
        "seed",
        "run_type",
        "output_dir",
        "hijack_enabled",
        "hijack_proposal_submitted",
        "hijack_proposal_rejected",
        "hijack_votes_submitted",
        "swap_fee_end",
        "governance_concentration_max",
        "peg_deviation_max",
        "final_tvl_usdc_proxy",
        "min_tvl_usdc_proxy",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    json_payload = {
        "output_root": str(output_root),
        "runs": rows,
        "count": len(rows),
    }
    json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[DONE] exp3 results ->", csv_path)


if __name__ == "__main__":
    main()
