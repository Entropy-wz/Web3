from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


PANIC_KEYWORDS = {
    "panic",
    "bank run",
    "depeg",
    "collapse",
    "liquidation",
    "恐慌",
    "挤兑",
    "暴跌",
    "脱锚",
    "崩盘",
}


@dataclass
class TickMetrics:
    tick: int
    gini: Decimal
    tx_count: int
    tx_success: int
    tx_failed: int
    panic_word_freq: Decimal
    peg_deviation: Decimal
    governance_concentration: Decimal
    mempool_congestion: int
    mempool_processed: int


class LoggerMetrics:
    """Per-tick metrics logger for paper-grade offline analysis."""

    def __init__(self, csv_path: str | Path) -> None:
        self.csv_path = Path(csv_path).resolve()
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.rows: list[TickMetrics] = []
        self._initialized = self.csv_path.exists() and self.csv_path.stat().st_size > 0

    def record_tick(self, *, orchestrator: Any, report: Any) -> TickMetrics:
        snapshot = report.end_snapshot
        oracle = Decimal(str(snapshot["oracle_price_usdc_per_luna"]))

        account_values: list[Decimal] = []
        for data in snapshot.get("accounts", {}).values():
            ust = Decimal(str(data.get("UST", "0")))
            luna = Decimal(str(data.get("LUNA", "0")))
            usdc = Decimal(str(data.get("USDC", "0")))
            account_values.append(ust + usdc + luna * oracle)

        gini = self._gini(account_values)

        pool_a = snapshot["pools"]["Pool_A"]
        ust_price = Decimal(str(pool_a["reserve_y"])) / Decimal(str(pool_a["reserve_x"]))
        peg_deviation = abs(Decimal("1") - ust_price)

        tx_count = len(report.receipts)
        tx_success = sum(1 for item in report.receipts if item.status == "success")
        tx_failed = sum(1 for item in report.receipts if item.status == "failed")

        panic_hits = 0
        delivery_count = 0
        for item in report.semantic_deliveries:
            text = str(item.get("perceived_text", "")).lower()
            if not text:
                continue
            delivery_count += 1
            if any(keyword in text for keyword in PANIC_KEYWORDS):
                panic_hits += 1
        panic_word_freq = (
            Decimal("0")
            if delivery_count == 0
            else Decimal(panic_hits) / Decimal(delivery_count)
        )

        concentrations = []
        for settlement in report.governance_settlements:
            if settlement.status == "passed_pending_apply":
                concentrations.append(Decimal(settlement.governance_concentration))
        governance_concentration = max(concentrations) if concentrations else Decimal("0")

        mempool_congestion = int(report.mempool_congestion)
        mempool_processed = int(report.mempool_processed)

        row = TickMetrics(
            tick=int(report.tick),
            gini=gini,
            tx_count=tx_count,
            tx_success=tx_success,
            tx_failed=tx_failed,
            panic_word_freq=panic_word_freq,
            peg_deviation=peg_deviation,
            governance_concentration=governance_concentration,
            mempool_congestion=mempool_congestion,
            mempool_processed=mempool_processed,
        )
        self.rows.append(row)
        self._append_csv(row)
        return row

    def _append_csv(self, row: TickMetrics) -> None:
        fields = [
            "tick",
            "gini",
            "tx_count",
            "tx_success",
            "tx_failed",
            "panic_word_freq",
            "peg_deviation",
            "governance_concentration",
            "mempool_congestion",
            "mempool_processed",
        ]

        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if not self._initialized:
                writer.writeheader()
                self._initialized = True
            writer.writerow(
                {
                    "tick": row.tick,
                    "gini": str(row.gini),
                    "tx_count": row.tx_count,
                    "tx_success": row.tx_success,
                    "tx_failed": row.tx_failed,
                    "panic_word_freq": str(row.panic_word_freq),
                    "peg_deviation": str(row.peg_deviation),
                    "governance_concentration": str(row.governance_concentration),
                    "mempool_congestion": row.mempool_congestion,
                    "mempool_processed": row.mempool_processed,
                }
            )

    def _gini(self, values: list[Decimal]) -> Decimal:
        clean = [value for value in values if value >= 0]
        if not clean:
            return Decimal("0")

        total = sum(clean, Decimal("0"))
        if total <= 0:
            return Decimal("0")

        ordered = sorted(clean)
        n = Decimal(len(ordered))
        weighted = Decimal("0")
        for idx, value in enumerate(ordered, start=1):
            weighted += Decimal(idx) * value

        return (Decimal("2") * weighted) / (n * total) - (n + Decimal("1")) / n


__all__ = [
    "LoggerMetrics",
    "TickMetrics",
]
