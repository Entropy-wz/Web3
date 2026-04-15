from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


class StateCheckpoint:
    """Exports full JSON checkpoint per simulation tick."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save_tick(self, *, orchestrator: Any, report: Any) -> Path:
        payload = {
            "tick": int(report.tick),
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "simulation_clock": {
                "current_tick": int(orchestrator.current_tick),
                "ticks_per_day": int(orchestrator.ticks_per_day),
                "current_day": int(orchestrator.current_tick // orchestrator.ticks_per_day),
            },
            "engine_snapshot": _jsonable(report.end_snapshot),
            "fee_vault_snapshot": {k: str(v) for k, v in report.fee_vault_snapshot.items()},
            "governance_state": _jsonable(orchestrator.governance.get_state()),
            "governance_settlements": [
                _jsonable(item.__dict__) if hasattr(item, "__dict__") else _jsonable(item)
                for item in report.governance_settlements
            ],
            "governance_applied_updates": [
                _jsonable(item.__dict__) if hasattr(item, "__dict__") else _jsonable(item)
                for item in report.governance_applied_updates
            ],
            "mempool": {
                "processed": int(report.mempool_processed),
                "remaining": int(report.mempool_congestion),
            },
            "receipts": [
                _jsonable(item.__dict__) if hasattr(item, "__dict__") else _jsonable(item)
                for item in report.receipts
            ],
        }

        out_path = self.output_dir / f"tick_{int(report.tick):06d}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_path


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: _jsonable(v) for k, v in value.__dict__.items()}
    return value


__all__ = ["StateCheckpoint"]
