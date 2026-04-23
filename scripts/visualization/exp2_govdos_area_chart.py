from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_project_rescue_tick(summary: dict) -> int | None:
    governance = summary.get("governance", {})
    dos = summary.get("governance_dos", {})
    proposals = governance.get("proposals", [])
    project_id = dos.get("project_proposal_id")

    if project_id:
        for proposal in proposals:
            if proposal.get("proposal_id") == project_id:
                settled = proposal.get("settled_tick")
                return int(settled) if settled is not None else None

    for proposal in proposals:
        if proposal.get("proposer") == "project_0":
            settled = proposal.get("settled_tick")
            return int(settled) if settled is not None else None
    return None


def _load_run(run_dir: Path) -> tuple[pd.DataFrame, dict, int | None]:
    metrics = pd.read_csv(run_dir / "metrics.csv")
    summary = _load_json(run_dir / "summary.json")
    rescue_tick = _extract_project_rescue_tick(summary)
    return metrics, summary, rescue_tick


def _smooth_array(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return values
    if window % 2 == 0:
        window += 1
    pad = window // 2
    padded = np.pad(values, (pad, pad), mode="edge")
    kernel = np.ones(window, dtype=float) / float(window)
    return np.convolve(padded, kernel, mode="valid")


def _prepare_plot_series(
    x_raw: np.ndarray,
    y_raw: np.ndarray,
    curve_style: str,
    smooth_window: int,
    smooth_density: int,
) -> tuple[np.ndarray, np.ndarray]:
    if curve_style == "raw":
        return x_raw, y_raw
    x_min = float(np.min(x_raw))
    x_max = float(np.max(x_raw))
    dense_points = int(max(300, (x_max - x_min) * max(2, smooth_density)))
    x_dense = np.linspace(x_min, x_max, dense_points)
    y_interp = np.interp(x_dense, x_raw, y_raw)
    y_smooth = _smooth_array(y_interp, smooth_window)
    return x_dense, y_smooth


def build_chart(
    none_dir: Path,
    dos3_dir: Path,
    output_dir: Path,
    dpi: int = 300,
    dos1_dir: Path | None = None,
    x_scale: str = "linear",
    curve_style: str = "smooth",
    smooth_window: int = 13,
    smooth_density: int = 10,
) -> tuple[Path, Path]:
    none_metrics, none_summary, none_rescue_tick = _load_run(none_dir)
    dos3_metrics, dos3_summary, dos3_rescue_tick = _load_run(dos3_dir)

    merged = pd.merge(
        none_metrics[["tick", "peg_deviation"]].rename(columns={"peg_deviation": "none_peg_deviation"}),
        dos3_metrics[["tick", "peg_deviation"]].rename(columns={"peg_deviation": "dos3_peg_deviation"}),
        on="tick",
        how="inner",
    )
    dos1_rescue_tick = None
    if dos1_dir is not None:
        dos1_metrics, _dos1_summary, dos1_rescue_tick = _load_run(dos1_dir)
        merged = pd.merge(
            merged,
            dos1_metrics[["tick", "peg_deviation"]].rename(columns={"peg_deviation": "dos1_peg_deviation"}),
            on="tick",
            how="inner",
        )
    merged = merged.sort_values("tick")

    if merged.empty:
        raise ValueError("No overlapping ticks found between none and dos3 metrics.")

    max_tick = int(merged["tick"].max())
    dos3_rescue_display = f">{max_tick}" if dos3_rescue_tick is None else str(dos3_rescue_tick)

    plt.style.use("seaborn-v0_8-whitegrid")
    plt.rcParams.update({"font.size": 14})
    fig, ax = plt.subplots(figsize=(12, 6.8))

    x_raw = merged["tick"].to_numpy(dtype=float)
    y_none_raw = merged["none_peg_deviation"].to_numpy(dtype=float)
    y_dos3_raw = merged["dos3_peg_deviation"].to_numpy(dtype=float)

    x_none, y_none = _prepare_plot_series(
        x_raw,
        y_none_raw,
        curve_style=curve_style,
        smooth_window=smooth_window,
        smooth_density=smooth_density,
    )
    x_dos3, y_dos3 = _prepare_plot_series(
        x_raw,
        y_dos3_raw,
        curve_style=curve_style,
        smooth_window=smooth_window,
        smooth_density=smooth_density,
    )

    if curve_style == "smooth":
        ax.plot(x_raw, y_none_raw, color="#1f77b4", linewidth=1.0, alpha=0.25, linestyle="--", label="_nolegend_")
        ax.plot(x_raw, y_dos3_raw, color="#d62728", linewidth=1.0, alpha=0.25, linestyle="--", label="_nolegend_")

    ax.plot(x_none, y_none, color="#1f77b4", linewidth=2.8, label=f"none (rescue tick={none_rescue_tick})")
    if "dos1_peg_deviation" in merged.columns:
        y_dos1_raw = merged["dos1_peg_deviation"].to_numpy(dtype=float)
        x_dos1, y_dos1 = _prepare_plot_series(
            x_raw,
            y_dos1_raw,
            curve_style=curve_style,
            smooth_window=smooth_window,
            smooth_density=smooth_density,
        )
        dos1_rescue_display = f">{max_tick}" if dos1_rescue_tick is None else str(dos1_rescue_tick)
        if curve_style == "smooth":
            ax.plot(x_raw, y_dos1_raw, color="#ff7f0e", linewidth=1.0, alpha=0.25, linestyle="--", label="_nolegend_")
        ax.plot(
            x_dos1,
            y_dos1,
            color="#ff7f0e",
            linewidth=2.6,
            label=f"dos1 (rescue tick={dos1_rescue_display})",
        )
    ax.plot(x_dos3, y_dos3, color="#d62728", linewidth=3.0, label=f"dos3 (rescue tick={dos3_rescue_display})")

    ax.fill_between(
        x_none,
        y_none,
        y_dos3,
        where=(y_dos3 >= y_none),
        interpolate=True,
        color="#9467bd",
        alpha=0.24,
        label="Gov-DoS Physical Loss (dos3 - none area)",
    )

    if none_rescue_tick is not None:
        ax.axvline(
            x=none_rescue_tick,
            color="#1f77b4",
            linestyle="--",
            linewidth=1.6,
            alpha=0.9,
        )
        ax.text(
            none_rescue_tick + 0.8,
            max(y_none.max(), y_dos3.max()) * 0.20,
            f"none rescue @ tick {none_rescue_tick}",
            color="#1f77b4",
            fontsize=11,
        )

    # Use >MAX_TICKS instead of NA for failed rescue in dos3.
    if dos3_rescue_tick is None:
        ax.text(
            max_tick * 0.62,
            max(y_none.max(), y_dos3.max()) * 0.92,
            f"dos3 rescue failed: >{max_tick}",
            color="#d62728",
            fontsize=11,
            bbox={"facecolor": "#fff5f5", "edgecolor": "#d62728", "alpha": 0.85},
        )
    else:
        ax.axvline(
            x=dos3_rescue_tick,
            color="#d62728",
            linestyle="--",
            linewidth=1.6,
            alpha=0.9,
        )

    if x_scale == "log":
        ax.set_xscale("log")
        ax.set_xticks([1, 2, 3, 5, 10, 20, 50, 100])
        ax.get_xaxis().set_major_formatter(plt.ScalarFormatter())

    all_y = [pd.Series(y_none), pd.Series(y_dos3)]
    if "dos1_peg_deviation" in merged.columns:
        all_y.append(pd.Series(y_dos1))
    y_min = min(float(series.min()) for series in all_y)
    y_max = max(float(series.max()) for series in all_y)
    y_pad = (y_max - y_min) * 0.10 if y_max > y_min else 0.02
    ax.set_ylim(max(0.0, y_min - y_pad), min(1.0, y_max + y_pad))

    ax.set_title("Exp2 Gov-DoS: Peg Deviation Trajectory and Physical Loss Gap")
    ax.set_xlabel("Ticks")
    ax.set_ylabel("Peg Deviation (higher = more dangerous)")
    ax.set_xlim(1, max_tick)
    ax.legend(loc="upper left", frameon=True)
    ax.grid(alpha=0.3)

    footnote = (
        f"Footnote: Rescue tick is shown as >{max_tick} when rescue never succeeded within run horizon "
        f"(governance lock-up)."
    )
    fig.text(0.01, 0.01, footnote, fontsize=10)
    fig.tight_layout(rect=[0, 0.04, 1, 1])

    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{x_scale}_{curve_style}"
    if dos1_dir is not None:
        suffix += "_with_dos1"
    png_path = output_dir / f"exp2_govdos_area_chart{suffix}.png"
    pdf_path = output_dir / f"exp2_govdos_area_chart{suffix}.pdf"
    fig.savefig(png_path, dpi=dpi, bbox_inches="tight")
    fig.savefig(pdf_path, format="pdf", bbox_inches="tight")
    plt.close(fig)
    return png_path, pdf_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Exp2 Gov-DoS area chart from two run dirs.")
    parser.add_argument("--none-dir", required=True, help="Run directory for none group.")
    parser.add_argument("--dos1-dir", default=None, help="Optional run directory for dos1 group.")
    parser.add_argument("--dos3-dir", required=True, help="Run directory for dos3 group.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for chart. Defaults to parent of none-dir.",
    )
    parser.add_argument(
        "--x-scale",
        choices=("linear", "log"),
        default="linear",
        help="X-axis scale. Use log to emphasize early-stage divergence.",
    )
    parser.add_argument(
        "--curve-style",
        choices=("raw", "smooth"),
        default="smooth",
        help="Curve rendering style. smooth keeps a faint raw reference line.",
    )
    parser.add_argument(
        "--smooth-window",
        type=int,
        default=13,
        help="Smoothing window size for smooth mode (odd values recommended).",
    )
    parser.add_argument(
        "--smooth-density",
        type=int,
        default=10,
        help="Interpolation density factor for smooth mode.",
    )
    parser.add_argument("--dpi", type=int, default=300)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    none_dir = Path(args.none_dir).resolve()
    dos1_dir = Path(args.dos1_dir).resolve() if args.dos1_dir else None
    dos3_dir = Path(args.dos3_dir).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else none_dir.parent.resolve()
    png_path, pdf_path = build_chart(
        none_dir,
        dos3_dir,
        output_dir,
        dpi=args.dpi,
        dos1_dir=dos1_dir,
        x_scale=args.x_scale,
        curve_style=args.curve_style,
        smooth_window=args.smooth_window,
        smooth_density=args.smooth_density,
    )
    print(f"[OK] png={png_path}")
    print(f"[OK] pdf={pdf_path}")


if __name__ == "__main__":
    main()
