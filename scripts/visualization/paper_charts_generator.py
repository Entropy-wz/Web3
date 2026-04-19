from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import seaborn as sns

REQUIRED_METRIC_COLUMNS = {
    "tick",
    "gini",
    "tx_success",
    "tx_failed",
    "panic_word_freq",
    "peg_deviation",
    "governance_concentration",
    "mempool_congestion",
}

DEFAULT_FORMATS = ("png", "pdf")
SUPPORTED_FORMATS = {"png", "pdf"}

COLOR_RISK = "#d62728"
COLOR_GINI = "#1f77b4"
COLOR_SUCCESS = "#2ca02c"
COLOR_FAILED = "#8f8f8f"
COLOR_CONGESTION = "#ff7f0e"
COLOR_OLIGARCHY = "#7b3fb6"
COLOR_THRESHOLD = "#c62828"

SHAPE_NEAR_FLOOR_PRICE = 0.01
SHAPE_FLOOR_RATIO_THRESHOLD = 0.90
CONGESTION_WARN_RATIO = 10.0


@dataclass
class ProposalEvent:
    proposal_id: str
    created_tick: int
    voting_end_tick: int
    settled_tick: int | None
    status: str
    apply_tick: int | None
    apply_is_approx: bool


@dataclass
class InsolvencyPoint:
    tick: int
    ust_price: float
    gini: float
    method: str


@dataclass
class ShapeGateResult:
    is_l_shape: bool
    floor_ratio_after_tick1: float
    min_price_after_tick1: float
    warning: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate paper-grade 2x2 risk dashboard and single charts."
    )
    parser.add_argument("--metrics", type=str, required=True, help="Path to metrics.csv")
    parser.add_argument("--summary", type=str, required=True, help="Path to summary.json")
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Optional sqlite path for exact applied_tick lookup",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Optional output directory for chart files. If omitted, save next to metrics.csv.",
    )
    parser.add_argument("--dpi", type=int, default=300, help="PNG export DPI (default: 300)")
    parser.add_argument("--style", type=str, default="whitegrid", help="Seaborn style")
    parser.add_argument(
        "--formats",
        type=str,
        default="png,pdf",
        help="Output formats, comma-separated. Supported: png,pdf",
    )
    parser.add_argument("--font-size", type=int, default=14, help="Global font size")
    parser.add_argument(
        "--congestion-scale",
        type=str,
        default="log",
        choices=("linear", "log"),
        help="Scale for mempool congestion line",
    )
    parser.add_argument(
        "--strict-shape-check",
        action="store_true",
        help="Fail fast if L-shape cliff is detected",
    )
    parser.add_argument(
        "--shape-report-json",
        type=str,
        default=None,
        help="Optional explicit path for diagnostics json",
    )
    return parser.parse_args()


def apply_style(style: str, font_size: int) -> None:
    sns.set_theme(style=style, context="talk")
    plt.rcParams.update(
        {
            "font.size": font_size,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.titlesize": font_size + 1,
            "axes.labelsize": font_size,
            "legend.fontsize": max(font_size - 1, 10),
            "xtick.labelsize": max(font_size - 2, 10),
            "ytick.labelsize": max(font_size - 2, 10),
        }
    )


def parse_formats(raw: str) -> tuple[str, ...]:
    values = tuple(part.strip().lower() for part in str(raw).split(",") if part.strip())
    if not values:
        return DEFAULT_FORMATS
    bad = sorted(set(values) - SUPPORTED_FORMATS)
    if bad:
        raise ValueError(f"unsupported format(s): {bad}. supported={sorted(SUPPORTED_FORMATS)}")
    seen: list[str] = []
    for item in values:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def load_metrics_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"metrics csv not found: {path}")

    rows = list(csv.DictReader(path.open("r", encoding="utf-8")))
    if not rows:
        raise ValueError(f"metrics csv is empty: {path}")

    header = set(rows[0].keys())
    missing = sorted(REQUIRED_METRIC_COLUMNS - header)
    if missing:
        raise ValueError(f"metrics csv missing columns: {missing}")

    parsed: list[dict[str, Any]] = []
    for row in rows:
        parsed.append(
            {
                "tick": int(row["tick"]),
                "gini": float(row["gini"]),
                "tx_success": float(row["tx_success"]),
                "tx_failed": float(row["tx_failed"]),
                "panic_word_freq": float(row["panic_word_freq"]),
                "peg_deviation": float(row["peg_deviation"]),
                "governance_concentration": float(row["governance_concentration"]),
                "mempool_congestion": float(row["mempool_congestion"]),
            }
        )
    parsed.sort(key=lambda item: item["tick"])
    return parsed


def load_summary_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"summary json not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("summary json root must be an object")
    return data


def _fetch_applied_ticks_from_db(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT proposal_id, MIN(applied_tick) AS applied_tick
            FROM governance_pending_updates
            WHERE status = 'applied' AND applied_tick IS NOT NULL
            GROUP BY proposal_id
            """
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _status_needs_apply_tick(status: str) -> bool:
    return str(status).strip().lower() in {"applied", "passed_pending_apply", "apply_failed"}


def resolve_proposal_events(summary: dict[str, Any], db_path: Path | None) -> list[ProposalEvent]:
    gov = summary.get("governance", {})
    proposals = gov.get("proposals", [])
    if not isinstance(proposals, list):
        return []

    db_applied = _fetch_applied_ticks_from_db(db_path) if db_path else {}
    events: list[ProposalEvent] = []
    for item in proposals:
        if not isinstance(item, dict):
            continue
        proposal_id = str(item.get("proposal_id", "")).strip()
        if not proposal_id:
            continue

        created = int(item.get("created_tick", 0))
        voting_end = int(item.get("voting_end_tick", created))
        settled_raw = item.get("settled_tick")
        settled = int(settled_raw) if settled_raw is not None else None
        status = str(item.get("status", "unknown"))

        apply_tick: int | None = None
        apply_is_approx = False
        if proposal_id in db_applied:
            apply_tick = db_applied[proposal_id]
        elif _status_needs_apply_tick(status):
            apply_tick = voting_end + 1
            apply_is_approx = True

        events.append(
            ProposalEvent(
                proposal_id=proposal_id,
                created_tick=created,
                voting_end_tick=voting_end,
                settled_tick=settled,
                status=status,
                apply_tick=apply_tick,
                apply_is_approx=apply_is_approx,
            )
        )
    events.sort(key=lambda x: (x.created_tick, x.proposal_id))
    return events


def compute_shape_gate(rows: list[dict[str, Any]]) -> ShapeGateResult:
    if len(rows) <= 1:
        return ShapeGateResult(
            is_l_shape=False,
            floor_ratio_after_tick1=0.0,
            min_price_after_tick1=1.0,
            warning=None,
        )

    post = rows[1:]
    prices = [1.0 - r["peg_deviation"] for r in post]
    near_floor = [p for p in prices if p <= SHAPE_NEAR_FLOOR_PRICE]
    floor_ratio = len(near_floor) / len(prices) if prices else 0.0
    min_price = min(prices) if prices else 1.0
    is_l_shape = floor_ratio >= SHAPE_FLOOR_RATIO_THRESHOLD
    warning = None
    if is_l_shape:
        warning = (
            f"L-shape cliff detected: {floor_ratio:.2%} of post-tick1 prices <= "
            f"{SHAPE_NEAR_FLOOR_PRICE:.4f}, min_price={min_price:.6f}"
        )

    return ShapeGateResult(
        is_l_shape=is_l_shape,
        floor_ratio_after_tick1=floor_ratio,
        min_price_after_tick1=min_price,
        warning=warning,
    )


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    v_min = min(values)
    v_max = max(values)
    if math.isclose(v_min, v_max):
        return [0.5 for _ in values]
    return [(v - v_min) / (v_max - v_min) for v in values]


def find_insolvency_point(rows: list[dict[str, Any]]) -> InsolvencyPoint:
    ticks = [r["tick"] for r in rows]
    prices = [1.0 - r["peg_deviation"] for r in rows]
    gini = [r["gini"] for r in rows]

    for tick, price, g in zip(ticks, prices, gini):
        if price <= 0.5:
            return InsolvencyPoint(tick=tick, ust_price=price, gini=g, method="price<=0.5")

    p_norm = _normalize(prices)
    g_norm = _normalize(gini)
    best_idx = min(range(len(rows)), key=lambda i: abs(p_norm[i] - g_norm[i]))
    return InsolvencyPoint(
        tick=ticks[best_idx],
        ust_price=prices[best_idx],
        gini=gini[best_idx],
        method="normalized_curve_nearest",
    )


def _congestion_warning(rows: list[dict[str, Any]]) -> str | None:
    max_congestion = max((r["mempool_congestion"] for r in rows), default=0.0)
    max_failed = max((r["tx_failed"] for r in rows), default=0.0)
    if max_failed <= 0:
        if max_congestion > 0:
            return "tx_failed is near zero while congestion exists; line may dominate bars"
        return None
    ratio = max_congestion / max_failed
    if ratio >= CONGESTION_WARN_RATIO:
        return f"high congestion-to-failed ratio ({ratio:.2f}); consider --congestion-scale log"
    return None


def draw_chart1_death_spiral(
    ax: plt.Axes,
    rows: list[dict[str, Any]],
    insolvency: InsolvencyPoint,
) -> None:
    ticks = [r["tick"] for r in rows]
    prices = [1.0 - r["peg_deviation"] for r in rows]
    gini = [r["gini"] for r in rows]

    line_price = ax.plot(
        ticks,
        prices,
        color=COLOR_RISK,
        linewidth=2.4,
        label="UST Price (1 - peg_deviation)",
    )[0]
    ax.set_xlabel("Tick")
    ax.set_ylabel("UST Price", color=COLOR_RISK)
    ax.tick_params(axis="y", colors=COLOR_RISK)
    ax.set_title("Death Spiral & Wealth Transfer")
    ax.grid(alpha=0.25, linestyle="--", linewidth=0.6)

    ax2 = ax.twinx()
    line_gini = ax2.plot(
        ticks,
        gini,
        color=COLOR_GINI,
        linestyle="--",
        linewidth=2.2,
        label="Gini",
    )[0]
    ax2.set_ylabel("Gini", color=COLOR_GINI)
    ax2.tick_params(axis="y", colors=COLOR_GINI)

    ax.annotate(
        "Systemic Insolvency Point",
        xy=(insolvency.tick, insolvency.ust_price),
        xytext=(insolvency.tick + max(2, len(rows) // 12), insolvency.ust_price + 0.08),
        arrowprops={"arrowstyle": "->", "color": "#111111", "lw": 1.5},
        fontsize=max(plt.rcParams["font.size"] - 1, 10),
        color="#111111",
    )

    ax.legend(
        [line_price, line_gini],
        [line_price.get_label(), line_gini.get_label()],
        loc="best",
        frameon=True,
    )


def draw_chart2_mempool_trampling(
    ax: plt.Axes,
    rows: list[dict[str, Any]],
    congestion_scale: str,
) -> None:
    ticks = [r["tick"] for r in rows]
    tx_success = [r["tx_success"] for r in rows]
    tx_failed = [r["tx_failed"] for r in rows]
    congestion = [r["mempool_congestion"] for r in rows]

    bars_success = ax.bar(
        ticks,
        tx_success,
        color=COLOR_SUCCESS,
        width=0.85,
        alpha=0.88,
        label="tx_success",
    )
    bars_failed = ax.bar(
        ticks,
        tx_failed,
        bottom=tx_success,
        color=COLOR_FAILED,
        width=0.85,
        alpha=0.88,
        label="tx_failed",
    )
    ax.set_xlabel("Tick")
    ax.set_ylabel("Transactions")
    ax.set_title("Mempool Congestion & Panic Trample")
    ax.grid(alpha=0.2, linestyle="--", linewidth=0.6)

    ax2 = ax.twinx()
    line_congestion = ax2.plot(
        ticks,
        congestion,
        color=COLOR_CONGESTION,
        linewidth=2.2,
        label="mempool_congestion",
    )[0]
    if congestion_scale == "log":
        ax2.set_yscale("log")
    ax2.set_ylabel("Mempool Congestion", color=COLOR_CONGESTION)
    ax2.tick_params(axis="y", colors=COLOR_CONGESTION)

    ax.legend(
        [bars_success, bars_failed, line_congestion],
        ["tx_success", "tx_failed", "mempool_congestion"],
        loc="upper left",
        frameon=True,
    )


def _window_band_text(events: list[ProposalEvent], idx: int) -> str:
    event = events[idx]
    return (
        f"[Tick {event.created_tick}-{event.voting_end_tick}: "
        "Voting Period - Panic Escalates]"
    )


def draw_chart3_governance_paralysis(
    ax: plt.Axes,
    rows: list[dict[str, Any]],
    events: list[ProposalEvent],
) -> None:
    ticks = [r["tick"] for r in rows]
    panic = [r["panic_word_freq"] for r in rows]

    ax.fill_between(ticks, panic, color=COLOR_RISK, alpha=0.26, label="panic_word_freq")
    ax.plot(ticks, panic, color=COLOR_RISK, linewidth=1.9)

    created_labeled = False
    window_labeled = False
    applied_labeled = False
    y_top = max(panic) if panic else 1.0
    for idx, event in enumerate(events):
        ax.axvline(
            event.created_tick,
            color="#5f6368",
            linestyle="--",
            linewidth=1.25,
            label="Proposal Created" if not created_labeled else None,
        )
        created_labeled = True

        ax.axvspan(
            event.created_tick,
            event.voting_end_tick,
            color="#9e9e9e",
            alpha=0.20,
            label="Voting Window" if not window_labeled else None,
        )
        window_labeled = True

        if event.apply_tick is not None:
            apply_label = "Parameter Applied"
            if event.apply_is_approx:
                apply_label = "Parameter Applied (approx)"
            ax.axvline(
                event.apply_tick,
                color="#111111",
                linestyle="-.",
                linewidth=1.5,
                label=apply_label if not applied_labeled else None,
            )
            applied_labeled = True

        text_y = y_top * (1.03 + idx * 0.06)
        ax.text(
            event.created_tick,
            text_y,
            _window_band_text(events, idx),
            fontsize=max(plt.rcParams["font.size"] - 3, 9),
            color="#303030",
            ha="left",
            va="bottom",
        )

    ax.set_xlabel("Tick")
    ax.set_ylabel("Panic Word Frequency")
    ax.set_title("Governance Paralysis")
    ax.set_ylim(bottom=0.0, top=max(y_top * (1.18 + max(len(events) - 1, 0) * 0.04), 1.0))
    ax.grid(alpha=0.2, linestyle="--", linewidth=0.6)
    ax.legend(loc="upper left", frameon=True)


def draw_chart4_governance_oligarchy(ax: plt.Axes, rows: list[dict[str, Any]]) -> None:
    ticks = [r["tick"] for r in rows]
    concentration = [r["governance_concentration"] for r in rows]

    ax.step(
        ticks,
        concentration,
        where="post",
        color=COLOR_OLIGARCHY,
        linewidth=2.2,
        label="governance_concentration",
    )
    ax.axhline(
        0.51,
        color=COLOR_THRESHOLD,
        linestyle="--",
        linewidth=1.6,
        label="51% Majority Attack Threshold",
    )

    above = [max(c - 0.51, 0.0) for c in concentration]
    if any(v > 0 for v in above):
        ax.fill_between(
            ticks,
            [0.51 for _ in ticks],
            concentration,
            where=[c >= 0.51 for c in concentration],
            color=COLOR_THRESHOLD,
            alpha=0.15,
            step="post",
        )

    ax.set_xlabel("Tick")
    ax.set_ylabel("Top-3 Approve Share")
    ax.set_title("Governance Oligarchy")
    ax.set_ylim(0.0, 1.05)
    ax.grid(alpha=0.25, linestyle="--", linewidth=0.6)
    ax.legend(loc="upper left", frameon=True)


def _save_figure(fig: plt.Figure, base_path: Path, formats: tuple[str, ...], dpi: int) -> list[Path]:
    outputs: list[Path] = []
    for fmt in formats:
        target = base_path.with_suffix(f".{fmt}")
        if fmt == "png":
            fig.savefig(target, dpi=dpi, bbox_inches="tight")
        else:
            fig.savefig(target, format=fmt, bbox_inches="tight")
        outputs.append(target)
    return outputs


def _save_single_chart(
    stem: str,
    output_dir: Path,
    formats: tuple[str, ...],
    dpi: int,
    rows: list[dict[str, Any]],
    events: list[ProposalEvent],
    insolvency: InsolvencyPoint,
    congestion_scale: str,
) -> dict[str, Path]:
    fig, ax = plt.subplots(figsize=(11, 6.5))
    if stem == "chart1_death_spiral_wealth_transfer":
        draw_chart1_death_spiral(ax, rows, insolvency)
    elif stem == "chart2_mempool_trampling":
        draw_chart2_mempool_trampling(ax, rows, congestion_scale)
    elif stem == "chart3_governance_paralysis":
        draw_chart3_governance_paralysis(ax, rows, events)
    elif stem == "chart4_governance_oligarchy":
        draw_chart4_governance_oligarchy(ax, rows)
    else:
        raise ValueError(f"unknown chart stem: {stem}")

    fig.tight_layout()
    paths = _save_figure(fig, output_dir / stem, formats, dpi)
    plt.close(fig)
    return {p.suffix.lstrip("."): p for p in paths}


def _build_shape_report(
    rows: list[dict[str, Any]],
    shape_gate: ShapeGateResult,
    insolvency: InsolvencyPoint,
    congestion_scale: str,
    congestion_warning: str | None,
) -> dict[str, Any]:
    return {
        "shape_gate": {
            "is_l_shape": shape_gate.is_l_shape,
            "floor_ratio_after_tick1": shape_gate.floor_ratio_after_tick1,
            "min_price_after_tick1": shape_gate.min_price_after_tick1,
            "warning": shape_gate.warning,
            "near_floor_price_threshold": SHAPE_NEAR_FLOOR_PRICE,
            "l_shape_ratio_threshold": SHAPE_FLOOR_RATIO_THRESHOLD,
        },
        "insolvency_point": {
            "tick": insolvency.tick,
            "ust_price": insolvency.ust_price,
            "gini": insolvency.gini,
            "method": insolvency.method,
        },
        "congestion_view": {
            "scale": congestion_scale,
            "warning": congestion_warning,
            "warn_ratio_threshold": CONGESTION_WARN_RATIO,
        },
        "ticks": {
            "start": rows[0]["tick"] if rows else None,
            "end": rows[-1]["tick"] if rows else None,
            "count": len(rows),
        },
    }


def generate_charts(
    *,
    metrics_path: Path,
    summary_path: Path,
    db_path: Path | None,
    output_dir: Path,
    dpi: int,
    style: str,
    formats: tuple[str, ...],
    font_size: int,
    congestion_scale: str,
    strict_shape_check: bool,
    shape_report_json: Path | None,
) -> dict[str, Any]:
    apply_style(style, font_size)
    rows = load_metrics_csv(metrics_path)
    summary = load_summary_json(summary_path)
    events = resolve_proposal_events(summary, db_path)

    shape_gate = compute_shape_gate(rows)
    if strict_shape_check and shape_gate.is_l_shape:
        raise RuntimeError(shape_gate.warning or "L-shape cliff detected")

    insolvency = find_insolvency_point(rows)
    congestion_warning = _congestion_warning(rows)

    output_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(18, 12))
    draw_chart1_death_spiral(axes[0, 0], rows, insolvency)
    draw_chart2_mempool_trampling(axes[0, 1], rows, congestion_scale)
    draw_chart3_governance_paralysis(axes[1, 0], rows, events)
    draw_chart4_governance_oligarchy(axes[1, 1], rows)
    fig.tight_layout()
    dashboard_paths = _save_figure(fig, output_dir / "paper_dashboard_2x2", formats, dpi)
    plt.close(fig)

    outputs: dict[str, Any] = {
        "dashboard": {p.suffix.lstrip("."): p for p in dashboard_paths},
        "chart1": _save_single_chart(
            "chart1_death_spiral_wealth_transfer",
            output_dir,
            formats,
            dpi,
            rows,
            events,
            insolvency,
            congestion_scale,
        ),
        "chart2": _save_single_chart(
            "chart2_mempool_trampling",
            output_dir,
            formats,
            dpi,
            rows,
            events,
            insolvency,
            congestion_scale,
        ),
        "chart3": _save_single_chart(
            "chart3_governance_paralysis",
            output_dir,
            formats,
            dpi,
            rows,
            events,
            insolvency,
            congestion_scale,
        ),
        "chart4": _save_single_chart(
            "chart4_governance_oligarchy",
            output_dir,
            formats,
            dpi,
            rows,
            events,
            insolvency,
            congestion_scale,
        ),
    }

    report = _build_shape_report(
        rows=rows,
        shape_gate=shape_gate,
        insolvency=insolvency,
        congestion_scale=congestion_scale,
        congestion_warning=congestion_warning,
    )
    report_path = shape_report_json or (output_dir / "shape_report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    outputs["shape_report_json"] = report_path
    outputs["diagnostics"] = report
    return outputs


def main() -> None:
    args = parse_args()
    if int(args.dpi) <= 0:
        raise ValueError("dpi must be > 0")
    if int(args.font_size) <= 0:
        raise ValueError("font-size must be > 0")

    formats = parse_formats(args.formats)
    metrics_path = Path(args.metrics).resolve()
    summary_path = Path(args.summary).resolve()
    db_path = Path(args.db).resolve() if args.db else None
    output_dir = Path(args.output_dir).resolve() if args.output_dir else metrics_path.parent.resolve()
    shape_report_json = Path(args.shape_report_json).resolve() if args.shape_report_json else None

    outputs = generate_charts(
        metrics_path=metrics_path,
        summary_path=summary_path,
        db_path=db_path,
        output_dir=output_dir,
        dpi=int(args.dpi),
        style=args.style,
        formats=formats,
        font_size=int(args.font_size),
        congestion_scale=args.congestion_scale,
        strict_shape_check=bool(args.strict_shape_check),
        shape_report_json=shape_report_json,
    )

    for section, value in outputs.items():
        if section == "diagnostics":
            continue
        if isinstance(value, dict):
            for fmt, path in value.items():
                print(f"{section}.{fmt}: {path}")
        else:
            print(f"{section}: {value}")
    if outputs["diagnostics"]["shape_gate"]["warning"]:
        print(f"WARNING: {outputs['diagnostics']['shape_gate']['warning']}")
    if outputs["diagnostics"]["congestion_view"]["warning"]:
        print(f"WARNING: {outputs['diagnostics']['congestion_view']['warning']}")


if __name__ == "__main__":
    main()
