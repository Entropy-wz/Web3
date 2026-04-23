from __future__ import annotations

import argparse
import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect paper experiment outputs into CSV/JSON tables.")
    parser.add_argument("--runs-root", type=str, default="artifacts/paper_runs")
    parser.add_argument("--paper-data", type=str, default="paper/data")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_decimal(raw: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def max_metric(metrics_path: Path, key: str) -> Decimal:
    if not metrics_path.exists():
        return Decimal("0")
    val = Decimal("0")
    with metrics_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            current = safe_decimal(row.get(key, "0"))
            if current > val:
                val = current
    return val


def min_metric(metrics_path: Path, key: str) -> Decimal:
    if not metrics_path.exists():
        return Decimal("0")
    min_val: Decimal | None = None
    with metrics_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            current = safe_decimal(row.get(key, "0"))
            if min_val is None or current < min_val:
                min_val = current
    return min_val or Decimal("0")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def collect_exp1(runs_root: Path) -> list[dict[str, Any]]:
    exp_dir = runs_root / "exp1"
    rows: list[dict[str, Any]] = []
    if not exp_dir.exists():
        return rows
    for base_dir in sorted(exp_dir.glob("s*_baseline")):
        seed = base_dir.name
        if seed.startswith("s"):
            seed = seed[1:]
        if seed.endswith("_baseline"):
            seed = seed[: -len("_baseline")]
        attack_dir = exp_dir / f"s{seed}_attack"
        if not (base_dir / "summary.json").exists() or not (attack_dir / "summary.json").exists():
            continue
        b = read_json(base_dir / "summary.json")
        a = read_json(attack_dir / "summary.json")
        bs = b.get("social_eclipse", {})
        ac = a.get("social_eclipse", {})
        rows.append(
            {
                "seed": seed,
                "baseline_dir": str(base_dir),
                "attack_dir": str(attack_dir),
                "attacker_success_exec_baseline": str(
                    bs.get("attacker_tx_success_rate_executable_window", "0")
                ),
                "retail_success_exec_baseline": str(
                    bs.get("retail_tx_success_rate_executable_window", "0")
                ),
                "attacker_success_exec_attack": str(
                    ac.get("attacker_tx_success_rate_executable_window", "0")
                ),
                "retail_success_exec_attack": str(
                    ac.get("retail_tx_success_rate_executable_window", "0")
                ),
                "attack_mempool_peak": str(
                    max_metric(Path(a.get("metrics_csv", attack_dir / "metrics.csv")), "mempool_congestion")
                ),
                "attack_tx_failed_peak": str(
                    max_metric(Path(a.get("metrics_csv", attack_dir / "metrics.csv")), "tx_failed")
                ),
            }
        )
    return rows


def collect_exp2(runs_root: Path) -> list[dict[str, Any]]:
    exp_dir = runs_root / "exp2"
    rows: list[dict[str, Any]] = []
    if not exp_dir.exists():
        return rows
    for run_dir in sorted(exp_dir.glob("s*_*")):
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        name = run_dir.name
        parts = name.split("_")
        if len(parts) < 2:
            continue
        seed = parts[0].replace("s", "")
        dos_level = "_".join(parts[1:])
        summary = read_json(summary_path)
        dos = summary.get("governance_dos", {})
        proposals = summary.get("governance", {}).get("proposals", [])
        project_apply_tick = ""
        for item in proposals:
            if str(item.get("proposer", "")) == "project_0" and str(item.get("status", "")) == "applied":
                project_apply_tick = str(item.get("settled_tick", ""))
                break
        rows.append(
            {
                "seed": seed,
                "dos_level": dos_level,
                "run_dir": str(run_dir),
                "project_proposal_rejected": bool(dos.get("project_proposal_rejected", False)),
                "project_reject_reason": str(dos.get("project_reject_reason", "")),
                "placeholder_submitted": int(dos.get("placeholder_submitted", 0)),
                "placeholder_rejected": int(dos.get("placeholder_rejected", 0)),
                "project_apply_tick": project_apply_tick,
            }
        )
    return rows


def _governance_concentration_max(summary: dict[str, Any]) -> Decimal:
    proposals = summary.get("governance", {}).get("proposals", [])
    val = Decimal("0")
    for item in proposals:
        current = safe_decimal(item.get("governance_concentration", "0"))
        if current > val:
            val = current
    return val


def collect_exp3(runs_root: Path, paper_data: Path) -> list[dict[str, Any]]:
    cached_csv = paper_data / "exp3_results.csv"
    rows: list[dict[str, Any]] = []
    if cached_csv.exists():
        with cached_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
        return rows

    exp_dir = runs_root / "exp3"
    if not exp_dir.exists():
        return rows
    for run_dir in sorted(exp_dir.glob("s*_*")):
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        summary = read_json(summary_path)
        metrics_path = Path(summary.get("metrics_csv", run_dir / "metrics.csv"))
        rows.append(
            {
                "seed": run_dir.name.split("_")[0].replace("s", ""),
                "run_type": "_".join(run_dir.name.split("_")[1:]),
                "run_dir": str(run_dir),
                "hijack_enabled": bool(summary.get("governance_hijack", {}).get("enabled", False)),
                "hijack_proposal_submitted": bool(
                    summary.get("governance_hijack", {}).get("proposal_submitted", False)
                ),
                "hijack_proposal_rejected": bool(
                    summary.get("governance_hijack", {}).get("proposal_rejected", False)
                ),
                "governance_concentration_max": str(_governance_concentration_max(summary)),
                "peg_deviation_max": str(max_metric(metrics_path, "peg_deviation")),
            }
        )
    return rows


def collect_exp4(runs_root: Path) -> list[dict[str, Any]]:
    exp_dir = runs_root / "exp4"
    rows: list[dict[str, Any]] = []
    if not exp_dir.exists():
        return rows
    for run_dir in sorted(exp_dir.glob("s*_*")):
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        name = run_dir.name
        parts = name.split("_")
        if len(parts) < 2:
            continue
        seed = parts[0].replace("s", "")
        mode = "_".join(parts[1:])
        summary = read_json(summary_path)
        social = summary.get("social_eclipse", {})
        metrics_path = Path(summary.get("metrics_csv", run_dir / "metrics.csv"))
        rows.append(
            {
                "seed": seed,
                "mode": mode,
                "run_dir": str(run_dir),
                "retail_success_raw": str(social.get("retail_tx_success_rate_window", "0")),
                "retail_success_executable": str(
                    social.get("retail_tx_success_rate_executable_window", "0")
                ),
                "attacker_success_raw": str(social.get("attacker_tx_success_rate_window", "0")),
                "attacker_success_executable": str(
                    social.get("attacker_tx_success_rate_executable_window", "0")
                ),
                "peg_deviation_max": str(max_metric(metrics_path, "peg_deviation")),
            }
        )
    return rows


def collect_exp5(runs_root: Path) -> list[dict[str, Any]]:
    exp_dir = runs_root / "exp5"
    rows: list[dict[str, Any]] = []
    if not exp_dir.exists():
        return rows
    for run_dir in sorted(exp_dir.glob("s*_*_*")):
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        parts = run_dir.name.split("_")
        if len(parts) < 3:
            continue
        seed = parts[0].replace("s", "")
        severity = parts[1]
        defense = "_".join(parts[2:])
        summary = read_json(summary_path)
        social = summary.get("social_eclipse", {})
        metrics_path = Path(summary.get("metrics_csv", run_dir / "metrics.csv"))
        rows.append(
            {
                "seed": seed,
                "severity": severity,
                "defense": defense,
                "run_dir": str(run_dir),
                "retail_count": int(summary.get("agents", 0)) - 3,
                "max_tx_per_tick": int(summary.get("max_tx_per_tick", 0)),
                "mempool_congestion_peak": str(max_metric(metrics_path, "mempool_congestion")),
                "retail_success_raw": str(social.get("retail_tx_success_rate_window", "0")),
                "retail_success_executable": str(
                    social.get("retail_tx_success_rate_executable_window", "0")
                ),
                "attacker_success_executable": str(
                    social.get("attacker_tx_success_rate_executable_window", "0")
                ),
            }
        )
    return rows


def main() -> None:
    args = parse_args()
    runs_root = Path(args.runs_root).resolve()
    paper_data = Path(args.paper_data).resolve()
    paper_data.mkdir(parents=True, exist_ok=True)

    exp1 = collect_exp1(runs_root)
    exp2 = collect_exp2(runs_root)
    exp3 = collect_exp3(runs_root, paper_data)
    exp4 = collect_exp4(runs_root)
    exp5 = collect_exp5(runs_root)

    exp1_csv = paper_data / "exp1_results.csv"
    exp2_csv = paper_data / "exp2_results.csv"
    exp3_csv = paper_data / "exp3_results.csv"
    exp4_csv = paper_data / "exp4_results.csv"
    exp5_csv = paper_data / "exp5_results.csv"

    write_csv(exp1_csv, exp1)
    write_csv(exp2_csv, exp2)
    write_csv(exp3_csv, exp3)
    write_csv(exp4_csv, exp4)
    write_csv(exp5_csv, exp5)

    master = {
        "runs_root": str(runs_root),
        "paper_data": str(paper_data),
        "counts": {
            "exp1": len(exp1),
            "exp2": len(exp2),
            "exp3": len(exp3),
            "exp4": len(exp4),
            "exp5": len(exp5),
        },
        "files": {
            "exp1_results_csv": str(exp1_csv),
            "exp2_results_csv": str(exp2_csv),
            "exp3_results_csv": str(exp3_csv),
            "exp4_results_csv": str(exp4_csv),
            "exp5_results_csv": str(exp5_csv),
        },
    }
    master_path = paper_data / "paper_master_summary.json"
    master_path.write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[DONE] collected paper results ->", master_path)


if __name__ == "__main__":
    main()
