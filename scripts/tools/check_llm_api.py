from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ace_sim.cognition.llm_router import OpenAIChatAdapter
from ace_sim.config.llm_config import load_llm_config


@dataclass
class ModelCheckResult:
    role: str
    backend: str
    model: str
    success: bool
    latency_seconds: float
    message: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight check for LLM API connectivity before simulation runs."
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional path to llm config TOML. Defaults to normal project resolution.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=12.0,
        help="Timeout (seconds) for each model test request.",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional path to write JSON report.",
    )
    parser.add_argument(
        "--stop-on-first-fail",
        action="store_true",
        help="Stop immediately when a model check fails.",
    )
    return parser.parse_args()


def test_single_model(
    adapter: OpenAIChatAdapter,
    *,
    role: str,
    backend: str,
    model: str,
    timeout: float,
) -> ModelCheckResult:
    if backend.lower() != "openai":
        return ModelCheckResult(
            role=role,
            backend=backend,
            model=model,
            success=False,
            latency_seconds=0.0,
            message="backend is not openai; this checker only validates OpenAI-compatible endpoints",
        )

    prompt = (
        "Return strict JSON with keys thought, speak, action. "
        "Set thought to 'api-ok', speak to null, action to null."
    )

    start = time.perf_counter()
    try:
        raw = adapter.generate(
            model=model,
            prompt=prompt,
            timeout=float(timeout),
            schema=None,
        )
        latency = time.perf_counter() - start

        parsed: dict[str, Any]
        if isinstance(raw, str):
            parsed = json.loads(raw)
        elif isinstance(raw, dict):
            parsed = raw
        else:
            raise ValueError("unexpected response type")

        required = {"thought", "speak", "action"}
        if not required.issubset(set(parsed.keys())):
            missing = sorted(required - set(parsed.keys()))
            raise ValueError(f"response missing fields: {missing}")

        return ModelCheckResult(
            role=role,
            backend=backend,
            model=model,
            success=True,
            latency_seconds=latency,
            message="ok",
        )
    except Exception as exc:  # noqa: BLE001
        latency = time.perf_counter() - start
        return ModelCheckResult(
            role=role,
            backend=backend,
            model=model,
            success=False,
            latency_seconds=latency,
            message=str(exc),
        )


def build_report(results: list[ModelCheckResult], cfg_path: str) -> dict[str, Any]:
    success_count = sum(1 for item in results if item.success)
    return {
        "checked_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "config_path": cfg_path,
        "total": len(results),
        "success": success_count,
        "failure": len(results) - success_count,
        "all_passed": success_count == len(results) if results else False,
        "results": [
            {
                "role": item.role,
                "backend": item.backend,
                "model": item.model,
                "success": item.success,
                "latency_seconds": round(item.latency_seconds, 4),
                "message": item.message,
            }
            for item in results
        ],
    }


def print_console(results: list[ModelCheckResult], report: dict[str, Any]) -> None:
    print("=" * 78)
    print("LLM API Preflight Check")
    print("=" * 78)
    for item in results:
        status = "PASS" if item.success else "FAIL"
        print(
            f"[{status}] role={item.role:<8} backend={item.backend:<8} "
            f"model={item.model:<20} latency={item.latency_seconds:.2f}s"
        )
        if not item.success:
            print(f"       reason: {item.message}")

    print("-" * 78)
    print(
        f"Summary: total={report['total']}, success={report['success']}, "
        f"failure={report['failure']}, all_passed={report['all_passed']}"
    )


def main() -> int:
    args = parse_args()
    cfg = load_llm_config(args.config)

    cfg_path = str(cfg.source_path) if cfg.source_path is not None else "<default>"
    print(f"Using config: {cfg_path}")

    api_key = cfg.openai.resolved_api_key()
    if not api_key:
        print("ERROR: OPENAI_API_KEY is not set (or api_key in config is empty).")
        print("Set key first, then retry.")
        return 2

    adapter = OpenAIChatAdapter(
        api_key=api_key,
        base_url=cfg.openai.base_url,
        organization=cfg.openai.organization,
        project=cfg.openai.project,
    )

    if not cfg.roles:
        print("ERROR: no role routes found in config.")
        return 2

    results: list[ModelCheckResult] = []
    for role in sorted(cfg.roles.keys()):
        route = cfg.roles[role]
        result = test_single_model(
            adapter,
            role=role,
            backend=route.backend,
            model=route.model,
            timeout=float(args.timeout),
        )
        results.append(result)

        if args.stop_on_first_fail and not result.success:
            break

    report = build_report(results, cfg_path=cfg_path)
    print_console(results, report)

    if args.output_json:
        output_path = Path(args.output_json).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Report saved: {output_path}")

    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
