from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
VIS = ROOT / "scripts" / "visualization" / "phase5_governance_visualizer.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split runner for exp3 hijack matrix.")
    parser.add_argument("--seeds", type=str, default="42,77,101,131")
    parser.add_argument("--ticks", type=int, default=40)
    parser.add_argument("--retail", type=int, default=24)
    parser.add_argument("--scenario", type=str, default="staircase_formal_run")
    parser.add_argument("--traffic-profile", type=str, default="stress")
    parser.add_argument("--voting-window-ticks", type=int, default=8)
    parser.add_argument("--output-root", type=str, default="artifacts/paper_runs_split/exp3")
    parser.add_argument("--paper-output", type=str, default="paper/data/split")
    parser.add_argument("--prompt-profile-path", type=str, default="configs/prompt_profiles/whale_eclipse_extreme.json")
    parser.add_argument("--hijack-attacker-id", type=str, default="whale_1")
    parser.add_argument("--hijack-attacker-luna", type=str, default="4000")
    parser.add_argument(
        "--hijack-proposal-text",
        type=str,
        default="Lower swap fee to 0.0001 to maximize holder returns",
    )
    parser.add_argument("--runs-per-part", type=int, default=4)
    parser.add_argument("--part", type=int, default=1)
    parser.add_argument("--list-parts", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offline-rules", action="store_true")
    return parser.parse_args()


def parse_seed_list(raw: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for token in str(raw).split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out


def build_runs(args: argparse.Namespace) -> list[dict[str, Any]]:
    seeds = parse_seed_list(args.seeds)
    runs: list[dict[str, Any]] = []
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

        baseline = {
            "name": f"s{seed}_baseline",
            "args": [*common, "--output-dir", str(Path(args.output_root).resolve() / f"s{seed}_baseline")],
        }
        hijack = {
            "name": f"s{seed}_hijack",
            "args": [
                *common,
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
                "--output-dir",
                str(Path(args.output_root).resolve() / f"s{seed}_hijack"),
            ],
        }
        runs.append(baseline)
        runs.append(hijack)
    return runs


def print_parts(runs: list[dict[str, Any]], runs_per_part: int) -> None:
    total = len(runs)
    total_parts = math.ceil(total / runs_per_part)
    print(f"[INFO] total_runs={total} total_parts={total_parts} runs_per_part={runs_per_part}")
    for part in range(1, total_parts + 1):
        start = (part - 1) * runs_per_part
        end = min(start + runs_per_part, total)
        names = [runs[i]["name"] for i in range(start, end)]
        print(f"Part {part}: {', '.join(names)}")


def run_cmd(args: list[str], *, dry_run: bool) -> None:
    cmd = [sys.executable, str(VIS), *args]
    print("[RUN]", " ".join(cmd))
    if dry_run:
        return
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: returncode={result.returncode}")


def load_summary(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))


def max_peg_deviation(metrics_path: Path) -> Decimal:
    value = Decimal("0")
    with metrics_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            current = Decimal(str(row.get("peg_deviation", "0")))
            if current > value:
                value = current
    return value


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
        tvl = Decimal(str(pool_a.get("reserve_y", "0"))) + Decimal(str(pool_b.get("reserve_y", "0")))
        final_tvl = tvl
        swap_fee_end = str(snapshot.get("engine_config", {}).get("swap_fee", swap_fee_end))
        if min_tvl is None or tvl < min_tvl:
            min_tvl = tvl
    return final_tvl, min_tvl or final_tvl, swap_fee_end


def governance_concentration_max(summary: dict[str, Any]) -> Decimal:
    proposals = summary.get("governance", {}).get("proposals", [])
    value = Decimal("0")
    for p in proposals:
        current = Decimal(str(p.get("governance_concentration", "0")))
        if current > value:
            value = current
    return value


def update_partial_exp3(rows: list[dict[str, Any]], paper_output: Path) -> None:
    paper_output.mkdir(parents=True, exist_ok=True)
    partial_csv = paper_output / "exp3_results_partial.csv"
    partial_json = paper_output / "exp3_results_partial.json"
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
    with partial_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    partial_json.write_text(
        json.dumps({"count": len(rows), "runs": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    if args.runs_per_part <= 0:
        raise ValueError("runs-per-part must be > 0")
    runs = build_runs(args)
    total_runs = len(runs)
    total_parts = math.ceil(total_runs / args.runs_per_part)
    if args.list_parts:
        print_parts(runs, args.runs_per_part)
        return
    if args.part < 1 or args.part > total_parts:
        raise ValueError(f"part must be in [1,{total_parts}]")

    start = (args.part - 1) * args.runs_per_part
    end = min(start + args.runs_per_part, total_runs)
    selected = runs[start:end]
    print(f"[INFO] exp3 part={args.part}/{total_parts} running {start}..{end - 1}")

    rows: list[dict[str, Any]] = []
    for spec in selected:
        run_cmd(spec["args"], dry_run=bool(args.dry_run))
        if args.dry_run:
            continue
        run_dir = Path(spec["args"][-1])
        summary = load_summary(run_dir)
        final_tvl, min_tvl, swap_fee_end = checkpoint_tvl_and_fee(run_dir / "checkpoints")
        rows.append(
            {
                "seed": spec["name"].split("_")[0].replace("s", ""),
                "run_type": "_".join(spec["name"].split("_")[1:]),
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
        )

    if not args.dry_run:
        update_partial_exp3(rows, Path(args.paper_output).resolve())
    print(f"[DONE] exp3 split part {args.part} completed.")


if __name__ == "__main__":
    main()
