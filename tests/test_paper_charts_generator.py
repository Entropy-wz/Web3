from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from PIL import Image


def _load_module():
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "scripts" / "visualization" / "paper_charts_generator.py"
    spec = importlib.util.spec_from_file_location("paper_charts_generator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PCG = _load_module()


def _write_metrics_csv(path: Path, rows: list[list[object]] | None = None) -> None:
    fields = [
        "tick",
        "gini",
        "tx_success",
        "tx_failed",
        "panic_word_freq",
        "peg_deviation",
        "governance_concentration",
        "mempool_congestion",
    ]
    rows = rows or [
        [1, "0.80", "40", "10", "0.10", "0.02", "0", "5"],
        [2, "0.81", "35", "15", "0.25", "0.05", "0", "8"],
        [3, "0.83", "30", "20", "0.40", "0.10", "1", "12"],
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        writer.writerows(rows)


def _write_summary_json(path: Path) -> None:
    payload = {
        "governance": {
            "proposals": [
                {
                    "proposal_id": "p1",
                    "created_tick": 1,
                    "voting_end_tick": 3,
                    "settled_tick": 3,
                    "status": "applied",
                }
            ]
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_db_with_applied_tick(path: Path, applied_tick: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE governance_pending_updates (
            update_id TEXT,
            proposal_id TEXT,
            status TEXT,
            applied_tick INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO governance_pending_updates (update_id, proposal_id, status, applied_tick)
        VALUES ('u1', 'p1', 'applied', ?)
        """,
        (int(applied_tick),),
    )
    conn.commit()
    conn.close()


def test_load_metrics_csv_requires_columns(tmp_path: Path):
    p = tmp_path / "metrics.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["tick", "gini", "tx_success"])
        writer.writerow([1, 0.8, 10])

    with pytest.raises(ValueError, match="missing columns"):
        PCG.load_metrics_csv(p)


def test_parse_formats():
    assert PCG.parse_formats("png,pdf") == ("png", "pdf")
    assert PCG.parse_formats(" pdf , png,pdf ") == ("pdf", "png")
    with pytest.raises(ValueError):
        PCG.parse_formats("png,svg")


def test_resolve_proposal_events_db_and_approx(tmp_path: Path):
    summary_path = tmp_path / "summary.json"
    _write_summary_json(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    events_no_db = PCG.resolve_proposal_events(summary, db_path=None)
    assert len(events_no_db) == 1
    assert events_no_db[0].apply_tick == 4
    assert events_no_db[0].apply_is_approx is True

    db_path = tmp_path / "trace.sqlite3"
    _write_db_with_applied_tick(db_path, applied_tick=7)
    events_with_db = PCG.resolve_proposal_events(summary, db_path=db_path)
    assert len(events_with_db) == 1
    assert events_with_db[0].apply_tick == 7
    assert events_with_db[0].apply_is_approx is False


def test_compute_shape_gate_detects_l_shape():
    rows = [
        {"tick": 1, "peg_deviation": 0.0},
        {"tick": 2, "peg_deviation": 0.995},
        {"tick": 3, "peg_deviation": 0.998},
        {"tick": 4, "peg_deviation": 0.999},
        {"tick": 5, "peg_deviation": 0.997},
    ]
    gate = PCG.compute_shape_gate(rows)  # type: ignore[arg-type]
    assert gate.is_l_shape is True
    assert gate.warning is not None


def test_find_insolvency_point_prefers_price_threshold():
    rows = [
        {"tick": 1, "peg_deviation": 0.02, "gini": 0.80},
        {"tick": 2, "peg_deviation": 0.45, "gini": 0.82},
        {"tick": 3, "peg_deviation": 0.52, "gini": 0.84},
    ]
    point = PCG.find_insolvency_point(rows)  # type: ignore[arg-type]
    assert point.tick == 3
    assert point.method == "price<=0.5"


def test_generate_charts_outputs_png_pdf_and_reports(tmp_path: Path):
    metrics_path = tmp_path / "metrics.csv"
    summary_path = tmp_path / "summary.json"
    db_path = tmp_path / "trace.sqlite3"
    out = tmp_path / "charts"

    _write_metrics_csv(metrics_path)
    _write_summary_json(summary_path)
    _write_db_with_applied_tick(db_path, applied_tick=7)

    outputs = PCG.generate_charts(
        metrics_path=metrics_path,
        summary_path=summary_path,
        db_path=db_path,
        output_dir=out,
        dpi=300,
        style="whitegrid",
        formats=("png", "pdf"),
        font_size=14,
        congestion_scale="linear",
        strict_shape_check=False,
        shape_report_json=None,
    )

    for section in ("dashboard", "chart1", "chart2", "chart3", "chart4"):
        assert "png" in outputs[section]
        assert "pdf" in outputs[section]
        assert outputs[section]["png"].exists()
        assert outputs[section]["pdf"].exists()

    report_path = outputs["shape_report_json"]
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "shape_gate" in report
    assert "insolvency_point" in report

    with Image.open(outputs["dashboard"]["png"]) as im:
        dpi = im.info.get("dpi", (0, 0))
    assert dpi[0] == pytest.approx(300, rel=0.02)
    assert dpi[1] == pytest.approx(300, rel=0.02)


def test_generate_charts_strict_shape_check_raises(tmp_path: Path):
    metrics_path = tmp_path / "metrics.csv"
    summary_path = tmp_path / "summary.json"
    out = tmp_path / "charts"

    _write_metrics_csv(
        metrics_path,
        rows=[
            [1, "0.80", "40", "10", "0.10", "0.0", "0", "5"],
            [2, "0.81", "35", "15", "0.20", "0.995", "0", "8"],
            [3, "0.82", "30", "20", "0.30", "0.998", "0", "12"],
            [4, "0.83", "25", "25", "0.40", "0.999", "0", "15"],
        ],
    )
    _write_summary_json(summary_path)

    with pytest.raises(RuntimeError, match="L-shape cliff detected"):
        PCG.generate_charts(
            metrics_path=metrics_path,
            summary_path=summary_path,
            db_path=None,
            output_dir=out,
            dpi=300,
            style="whitegrid",
            formats=("png",),
            font_size=14,
            congestion_scale="linear",
            strict_shape_check=True,
            shape_report_json=None,
        )
