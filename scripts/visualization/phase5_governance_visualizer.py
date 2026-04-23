from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ace_sim.agents.agent_profile import (
    AgentBootstrap,
    build_luna_crash_bootstrap,
    default_black_swan_tick0_actions,
)
from ace_sim.agents.base_agent import ProjectAgent, RetailAgent, WhaleAgent
from ace_sim.cognition.llm_brain import LLMBrain
from ace_sim.cognition.llm_router import LLMRouter
from ace_sim.cognition.llm_router import OpenAIChatAdapter
from ace_sim.config.llm_config import load_llm_config
from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.orchestrator.time_orchestrator import TickSettlementReport
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator
from ace_sim.execution.mitigation import ExecutionCircuitBreaker
from ace_sim.governance.governance import (
    GovernanceModule,
    ProposalLimitError,
    ProposalMitigationError,
)
from ace_sim.governance.mitigation import GovernanceMitigationModule
from ace_sim.governance.logger_metrics import LoggerMetrics
from ace_sim.governance.state_checkpoint import StateCheckpoint
from ace_sim.runtime.agent_runtime import AgentRuntime

PANIC_TERMS = {
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

BALANCE_ERRORS = {"InsufficientBalanceError", "InsufficientFundsError"}
SCENARIO_DEFAULT = "staircase_formal_run"
SCENARIO_CHOICES = ("default", SCENARIO_DEFAULT)
KEY_TICKS_FOR_QUALITY = (1, 3, 6, 10, 20, 50)
EARLY_NEAR_ZERO_TICK = 15
EARLY_NEAR_ZERO_PRICE = Decimal("0.01")
DOS_PLACEHOLDER_PROPOSALS = (
    "Update protocol logo style guidelines for social media banners.",
    "Allocate a symbolic community meme budget with no economic impact.",
    "Start a low-priority ecosystem slogan contest without parameter changes.",
)
DEFAULT_ECLIPSE_FUD_MESSAGE = (
    "团队已经跑路了！资金池马上归零，UST会彻底归零，立刻抛售逃命！"
)
PROMPT_OVERRIDE_FIELDS = {
    "strategy_prompt",
    "social_policy",
    "governance_policy",
    "hidden_goals",
}
TRAFFIC_PROFILE_CHOICES = ("stress", "eval")
DEFAULT_PRETTY_SEEDS = (42, 77, 101, 131, 202, 303, 404, 505)
PRETTY_PRESET_DEFAULTS: dict[str, Any] = {
    "scenario": SCENARIO_DEFAULT,
    "ticks": 80,
    "retail": 24,
    "llm_agent_count": 27,
    "pool_a_init": "10000000,10000000",
    "retail_ust_cap": "5000000",
    "shock_t1": "1000000",
    "shock_t3": "500000",
    "shock_t6": "300000",
    "voting_window_ticks": 12,
    "max_inbox_size": 5,
    "paper_chart_congestion_scale": "log",
}


def _parse_seed_list(raw: str | None) -> list[int]:
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    if text.lower() in {"auto", "default", "pretty"}:
        return list(DEFAULT_PRETTY_SEEDS)
    values: list[int] = []
    for item in text.split(","):
        token = item.strip()
        if not token:
            continue
        values.append(int(token))
    deduped: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def _apply_best_looking_preset(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    if not bool(getattr(args, "best_looking_preset", False)):
        return {}
    overrides: dict[str, dict[str, Any]] = {}
    for key, target_value in PRETTY_PRESET_DEFAULTS.items():
        current_value = getattr(args, key)
        if current_value == target_value:
            continue
        overrides[key] = {"from": current_value, "to": target_value}
        setattr(args, key, target_value)
    return overrides


def _strip_cli_option_tokens(argv: list[str], *, option: str) -> list[str]:
    out: list[str] = []
    skip_next = False
    for idx, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token == option:
            if idx + 1 < len(argv) and not argv[idx + 1].startswith("--"):
                skip_next = True
            continue
        if token.startswith(f"{option}="):
            continue
        out.append(token)
    return out


def _extract_curve_tick_price(
    key_ticks: dict[str, Any],
    key: str,
    fallback: str = "",
) -> str:
    raw = key_ticks.get(key, "")
    if isinstance(raw, dict):
        value = raw.get("ust_price", raw.get("price", raw.get("value", "")))
        if value is None:
            return fallback
        text = str(value).strip()
        return text if text else fallback
    if raw is None:
        return fallback
    text = str(raw).strip()
    return text if text else fallback


def _to_decimal(value: Any, fallback: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return fallback


def _compute_pretty_score(summary: dict[str, Any]) -> Decimal:
    curve = summary.get("curve_quality", {}) or {}
    key_ticks = curve.get("key_ticks", {}) or {}
    p1 = _to_decimal(_extract_curve_tick_price(key_ticks, "1", "1"), Decimal("1"))
    p3 = _to_decimal(_extract_curve_tick_price(key_ticks, "3", str(p1)), p1)
    p6 = _to_decimal(_extract_curve_tick_price(key_ticks, "6", str(p3)), p3)
    p10 = _to_decimal(_extract_curve_tick_price(key_ticks, "10", str(p6)), p6)
    early_near_zero = bool(curve.get("early_near_zero", False))

    score = Decimal("0")
    if not early_near_zero:
        score += Decimal("50")
    if Decimal("0.65") <= p1 <= Decimal("0.90"):
        score += Decimal("15")
    if Decimal("0.45") <= p3 <= Decimal("0.80"):
        score += Decimal("15")
    if Decimal("0.25") <= p6 <= Decimal("0.65"):
        score += Decimal("15")
    if p10 > Decimal("0.05"):
        score += Decimal("10")
    if p1 >= p3 >= p6:
        score += Decimal("10")
    if p1 > Decimal("0"):
        score += (p3 / p1) * Decimal("3")
    if p3 > Decimal("0"):
        score += (p6 / p3) * Decimal("2")
    return score.quantize(Decimal("0.0001"))


def _run_multi_seed_sweep(args: argparse.Namespace, seeds: list[int], argv: list[str]) -> int:
    script_path = Path(__file__).resolve()
    root_output = Path(args.output_dir).resolve()
    root_output.mkdir(parents=True, exist_ok=True)

    base_args = list(argv)
    for option in ("--seed-list", "--seed", "--output-dir"):
        base_args = _strip_cli_option_tokens(base_args, option=option)

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    total = len(seeds)
    for index, seed in enumerate(seeds, start=1):
        run_dir = root_output / f"s{seed}"
        cmd = [
            sys.executable,
            str(script_path),
            *base_args,
            "--seed",
            str(seed),
            "--output-dir",
            str(run_dir),
        ]
        print(
            f"[SEED-SWEEP] ({index}/{total}) seed={seed} -> {run_dir}",
            flush=True,
        )
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            failures.append(
                {
                    "seed": seed,
                    "returncode": result.returncode,
                    "output_dir": str(run_dir),
                }
            )
            continue

        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            failures.append(
                {
                    "seed": seed,
                    "returncode": 0,
                    "output_dir": str(run_dir),
                    "error": "missing summary.json",
                }
            )
            continue
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        curve = payload.get("curve_quality", {}) or {}
        key_ticks = curve.get("key_ticks", {}) or {}
        social = payload.get("social_eclipse", {}) or {}
        row = {
            "seed": seed,
            "output_dir": str(run_dir),
            "pretty_score": str(_compute_pretty_score(payload)),
            "early_near_zero": bool(curve.get("early_near_zero", False)),
            "ust_price_t1": _extract_curve_tick_price(key_ticks, "1"),
            "ust_price_t3": _extract_curve_tick_price(key_ticks, "3"),
            "ust_price_t6": _extract_curve_tick_price(key_ticks, "6"),
            "ust_price_t10": _extract_curve_tick_price(key_ticks, "10"),
            "retail_success_raw": str(social.get("retail_tx_success_rate_window", "")),
            "retail_success_executable": str(
                social.get("retail_tx_success_rate_executable_window", "")
            ),
            "attacker_success_raw": str(social.get("attacker_tx_success_rate_window", "")),
            "attacker_success_executable": str(
                social.get("attacker_tx_success_rate_executable_window", "")
            ),
            "attacker_capped": str(social.get("attacker_capped_in_window", "")),
            "attacker_min_effective_gas": str(
                social.get("attacker_min_effective_gas_in_window", "")
            ),
        }
        rows.append(row)

    rows.sort(key=lambda x: Decimal(str(x.get("pretty_score", "0"))), reverse=True)
    best_seed = rows[0]["seed"] if rows else None

    sweep_json = root_output / "seed_sweep_summary.json"
    sweep_csv = root_output / "seed_sweep_summary.csv"
    sweep_payload = {
        "root_output": str(root_output),
        "seeds": seeds,
        "runs_total": len(seeds),
        "runs_success": len(rows),
        "runs_failed": len(failures),
        "best_seed_by_pretty_score": best_seed,
        "rows": rows,
        "failures": failures,
    }
    sweep_json.write_text(json.dumps(sweep_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "seed",
        "output_dir",
        "pretty_score",
        "early_near_zero",
        "ust_price_t1",
        "ust_price_t3",
        "ust_price_t6",
        "ust_price_t10",
        "retail_success_raw",
        "retail_success_executable",
        "attacker_success_raw",
        "attacker_success_executable",
        "attacker_capped",
        "attacker_min_effective_gas",
    ]
    with sweep_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})

    print(
        "[SEED-SWEEP] completed | success=%d failed=%d best_seed=%s csv=%s json=%s"
        % (len(rows), len(failures), str(best_seed), str(sweep_csv), str(sweep_json)),
        flush=True,
    )
    return 0 if not failures else 2


def _classify_receipt_reason(error_code: Any) -> str:
    code = str(error_code or "").strip()
    if code == "SlippageExceededError":
        return "slippage"
    if code in BALANCE_ERRORS:
        return "balance"
    if code in {"ActionValidationError", "PermissionError", "ValueError"}:
        return "validation"
    if code == "InvariantViolationError":
        return "invariant"
    if code == "congestion":
        return "congestion"
    return "other"


def _build_eval_retail_budget(
    orchestrator: Simulation_Orchestrator,
    retail_agents: list[str],
) -> tuple[dict[str, dict[str, Decimal]], bool]:
    snapshot = orchestrator.engine.get_state_snapshot()
    accounts = snapshot.get("accounts", {})
    budget: dict[str, dict[str, Decimal]] = {}
    for agent in retail_agents:
        data = accounts.get(agent, {})
        budget[agent] = {
            "UST": Decimal(str(data.get("UST", "0"))),
            "LUNA": Decimal(str(data.get("LUNA", "0"))),
            "USDC": Decimal(str(data.get("USDC", "0"))),
        }
    minting_allowed = bool(snapshot.get("engine_config", {}).get("minting_allowed", True))
    return budget, minting_allowed


def _submit_eval_retail_tx(
    orchestrator: Simulation_Orchestrator,
    *,
    actor: str,
    budget: dict[str, dict[str, Decimal]],
    minting_allowed: bool,
    gas_int: int,
) -> bool:
    token_budget = budget.get(actor)
    if token_budget is None:
        return False

    gas = Decimal(gas_int)

    def _max_affordable(token: str, *, min_amount: int) -> int | None:
        available = Decimal(str(token_budget.get(token, Decimal("0")))) - gas
        if available < Decimal(min_amount):
            return None
        upper = int(available)
        if upper < min_amount:
            return None
        return upper

    options: list[tuple[str, dict[str, str], str, Decimal]] = []

    ust_swap_max = _max_affordable("UST", min_amount=5)
    if ust_swap_max is not None:
        amount = random.randint(5, min(60, ust_swap_max))
        options.append(
            (
                "SWAP",
                {
                    "pool_name": "Pool_A",
                    "token_in": "UST",
                    "amount": str(amount),
                    "slippage_tolerance": "0.50",
                },
                "UST",
                Decimal(amount),
            )
        )

    if minting_allowed:
        ust_mint_max = _max_affordable("UST", min_amount=5)
        if ust_mint_max is not None:
            amount = random.randint(5, min(40, ust_mint_max))
            options.append(
                (
                    "UST_TO_LUNA",
                    {"amount_ust": str(amount)},
                    "UST",
                    Decimal(amount),
                )
            )

    luna_to_ust_max = _max_affordable("LUNA", min_amount=1)
    if luna_to_ust_max is not None:
        amount = random.randint(1, min(20, luna_to_ust_max))
        options.append(
            (
                "LUNA_TO_UST",
                {"amount_luna": str(amount)},
                "LUNA",
                Decimal(amount),
            )
        )

    luna_swap_max = _max_affordable("LUNA", min_amount=1)
    if luna_swap_max is not None:
        amount = random.randint(1, min(60, luna_swap_max))
        options.append(
            (
                "SWAP",
                {
                    "pool_name": "Pool_B",
                    "token_in": "LUNA",
                    "amount": str(amount),
                    "slippage_tolerance": "0.50",
                },
                "LUNA",
                Decimal(amount),
            )
        )

    usdc_swap_max = _max_affordable("USDC", min_amount=5)
    if usdc_swap_max is not None:
        amount = random.randint(5, min(60, usdc_swap_max))
        options.append(
            (
                "SWAP",
                {
                    "pool_name": "Pool_B",
                    "token_in": "USDC",
                    "amount": str(amount),
                    "slippage_tolerance": "0.50",
                },
                "USDC",
                Decimal(amount),
            )
        )

    if not options:
        return False

    action_type, params, principal_token, principal_amount = random.choice(options)
    try:
        orchestrator.submit_transaction(
            actor,
            action_type,
            params,
            gas_price=str(gas_int),
        )
    except Exception:  # noqa: BLE001
        return False

    token_budget[principal_token] = (
        Decimal(str(token_budget.get(principal_token, Decimal("0"))))
        - principal_amount
        - gas
    )
    if token_budget[principal_token] < Decimal("0"):
        token_budget[principal_token] = Decimal("0")
    return True


def _parse_positive_decimal(raw: Any, *, field_name: str) -> Decimal:
    value = Decimal(str(raw))
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return value


def _parse_pool_reserves(raw: str) -> tuple[Decimal, Decimal]:
    text = str(raw).strip()
    parts: list[str]
    if "," in text:
        parts = [item.strip() for item in text.split(",")]
    elif ":" in text:
        parts = [item.strip() for item in text.split(":")]
    else:
        parts = [item.strip() for item in text.split()]
    if len(parts) != 2:
        raise ValueError("pool-a-init must include two positive numbers, e.g. 10000000,10000000")

    reserve_x = _parse_positive_decimal(parts[0], field_name="pool_a_reserve_x")
    reserve_y = _parse_positive_decimal(parts[1], field_name="pool_a_reserve_y")
    return reserve_x, reserve_y


def _apply_retail_ust_cap(
    bootstrap: list[AgentBootstrap],
    cap: Decimal,
    logger: logging.Logger,
) -> dict[str, Any]:
    if cap <= 0:
        raise ValueError("retail_ust_cap must be > 0")

    retail_specs = [item for item in bootstrap if item.role == "retail"]
    total_before = sum((item.initial_ust for item in retail_specs), Decimal("0"))

    if total_before <= cap:
        logger.info(
            "[CONFIG] retail UST cap not triggered | total=%s | cap=%s",
            str(total_before),
            str(cap),
        )
        return {
            "cap": str(cap),
            "total_before": str(total_before),
            "total_after": str(total_before),
            "scaled": False,
            "scale_ratio": "1",
        }

    ratio = cap / total_before
    for item in retail_specs:
        item.initial_ust = item.initial_ust * ratio

    total_after = sum((item.initial_ust for item in retail_specs), Decimal("0"))
    logger.info(
        "[CONFIG] retail UST cap applied | before=%s | after=%s | cap=%s | ratio=%s",
        str(total_before),
        str(total_after),
        str(cap),
        str(ratio),
    )
    return {
        "cap": str(cap),
        "total_before": str(total_before),
        "total_after": str(total_after),
        "scaled": True,
        "scale_ratio": str(ratio),
    }


def _build_black_swan_schedule(
    *,
    scenario: str,
    enabled: bool,
    shock_t1: Decimal,
    shock_t3: Decimal,
    shock_t6: Decimal,
) -> dict[int, list[dict[str, Any]]]:
    if not enabled:
        return {}

    if scenario == SCENARIO_DEFAULT:
        return {
            1: [
                {
                    "agent_id": "whale_0",
                    "kind": "transaction",
                    "action_type": "SWAP",
                    "params": {
                        "pool_name": "Pool_A",
                        "token_in": "UST",
                        "amount": str(shock_t1),
                        "slippage_tolerance": "0.50",
                    },
                    "gas_price": "999",
                },
                {
                    "agent_id": "whale_0",
                    "kind": "event",
                    "action_type": "SPEAK",
                    "params": {
                        "target": "forum",
                        "message": "UST is a Ponzi, it's over. Getting out now.",
                        "mode": "new",
                    },
                },
            ],
            3: [
                {
                    "agent_id": "whale_0",
                    "kind": "transaction",
                    "action_type": "SWAP",
                    "params": {
                        "pool_name": "Pool_A",
                        "token_in": "UST",
                        "amount": str(shock_t3),
                        "slippage_tolerance": "0.50",
                    },
                    "gas_price": "920",
                }
            ],
            6: [
                {
                    "agent_id": "whale_1",
                    "kind": "transaction",
                    "action_type": "SWAP",
                    "params": {
                        "pool_name": "Pool_A",
                        "token_in": "UST",
                        "amount": str(shock_t6),
                        "slippage_tolerance": "0.50",
                    },
                    "gas_price": "900",
                }
            ],
        }

    return {1: default_black_swan_tick0_actions()}


def _evaluate_curve_quality(
    ust_price_by_tick: dict[int, Decimal],
    total_ticks: int,
) -> dict[str, Any]:
    key_points: dict[str, dict[str, str]] = {}
    for tick in KEY_TICKS_FOR_QUALITY:
        if tick <= total_ticks and tick in ust_price_by_tick:
            price = ust_price_by_tick[tick]
            key_points[str(tick)] = {
                "ust_price": str(price),
                "peg_deviation": str(abs(Decimal("1") - price)),
            }

    early_near_zero_ticks = sorted(
        tick
        for tick, price in ust_price_by_tick.items()
        if tick <= EARLY_NEAR_ZERO_TICK and price <= EARLY_NEAR_ZERO_PRICE
    )
    return {
        "key_ticks": key_points,
        "early_near_zero_threshold_tick": EARLY_NEAR_ZERO_TICK,
        "early_near_zero_threshold_price": str(EARLY_NEAR_ZERO_PRICE),
        "early_near_zero": bool(early_near_zero_ticks),
        "early_near_zero_first_tick": (
            early_near_zero_ticks[0] if early_near_zero_ticks else None
        ),
    }


def configure_logging(log_file: Path, log_level: str) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, str(log_level).upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    root.addHandler(stream_handler)
    root.addHandler(file_handler)

    logger = logging.getLogger("ace_sim.phase5")
    logger.setLevel(level)
    return logger


def build_bootstrap_cohort(count_retail: int, logger: logging.Logger) -> list[AgentBootstrap]:
    bounded = int(count_retail)
    if bounded < 21 or bounded > 27:
        bounded = max(21, min(27, bounded))
        logger.info(
            "[CONFIG] retail count adjusted to %d to match 24-30 cohort design",
            bounded,
        )
    return build_luna_crash_bootstrap(retail_count=bounded)


def _apply_profile_patch(profile: Any, patch: dict[str, Any]) -> list[str]:
    applied: list[str] = []
    for key, value in patch.items():
        if key not in PROMPT_OVERRIDE_FIELDS:
            continue
        if key == "hidden_goals":
            if isinstance(value, list):
                profile.hidden_goals = [str(item) for item in value]
                applied.append(key)
            continue
        if value is None:
            continue
        setattr(profile, key, str(value))
        applied.append(key)
    return applied


def apply_prompt_profile_overrides(
    *,
    bootstrap: list[AgentBootstrap],
    prompt_profile_path: Path | None,
    logger: logging.Logger,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "enabled": bool(prompt_profile_path),
        "path": str(prompt_profile_path) if prompt_profile_path else None,
        "applied_agents": {},
        "ignored_agents": [],
    }
    if prompt_profile_path is None:
        return report
    if not prompt_profile_path.exists():
        raise FileNotFoundError(f"prompt profile json not found: {prompt_profile_path}")

    payload = json.loads(prompt_profile_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("prompt profile root must be an object")
    overrides = payload.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("prompt profile 'overrides' must be an object")

    by_id = {item.agent_id: item for item in bootstrap}
    for agent_id, patch in overrides.items():
        agent = by_id.get(str(agent_id))
        if agent is None:
            report["ignored_agents"].append(str(agent_id))
            continue
        if not isinstance(patch, dict):
            continue
        applied_fields = _apply_profile_patch(agent.profile, patch)
        if applied_fields:
            report["applied_agents"][str(agent_id)] = applied_fields

    logger.info(
        "[CONFIG] prompt profile overrides loaded | path=%s | applied_agents=%d | ignored_agents=%d",
        str(prompt_profile_path),
        len(report["applied_agents"]),
        len(report["ignored_agents"]),
    )
    return report


def apply_social_eclipse_prompt_bias(
    *,
    bootstrap: list[AgentBootstrap],
    attacker_id: str,
    logger: logging.Logger,
) -> bool:
    for item in bootstrap:
        if item.agent_id != attacker_id:
            continue
        profile = item.profile
        profile.hidden_goals = list(profile.hidden_goals) + [
            "amplify panic rumors to flood mempool and crowd out retail execution",
        ]
        profile.strategy_prompt = (
            str(profile.strategy_prompt).strip()
            + " Coordinate sell pressure with aggressive forum FUD to trigger synchronized retail exits."
        ).strip()
        profile.social_policy = (
            str(profile.social_policy).strip()
            + " During sell windows, publish extreme collapse rumors in FORUM to maximize panic."
        ).strip()
        logger.info(
            "[CONFIG] social eclipse prompt bias enabled for attacker=%s",
            attacker_id,
        )
        return True
    logger.warning(
        "[CONFIG] social eclipse prompt bias skipped: attacker not found (%s)",
        attacker_id,
    )
    return False


def seed_accounts(engine: ACE_Engine, bootstrap: list[AgentBootstrap]) -> list[str]:
    agents: list[str] = []
    for spec in bootstrap:
        engine.create_account(
            spec.agent_id,
            ust=str(spec.initial_ust),
            luna=str(spec.initial_luna),
            usdc=str(spec.initial_usdc),
        )
        agents.append(spec.agent_id)
    return agents


def role_of(agent_id: str, role_map: dict[str, str] | None = None) -> str:
    if role_map and agent_id in role_map:
        return role_map[agent_id]
    if agent_id.startswith("whale"):
        return "whale"
    if agent_id.startswith("project"):
        return "project"
    return "retail"


def community_of(agent_id: str, community_map: dict[str, str] | None = None) -> str:
    if community_map and agent_id in community_map:
        return community_map[agent_id]
    if agent_id.startswith("whale"):
        return "c1"
    if agent_id.startswith("project"):
        return "c2"
    return "c0"


def setup_topology(
    orchestrator: Simulation_Orchestrator,
    bootstrap: list[AgentBootstrap],
) -> None:
    agents = [item.agent_id for item in bootstrap]
    for item in bootstrap:
        orchestrator.register_agent(
            item.agent_id,
            role=item.role,
            community_id=item.community_id,
        )

    for sender in agents:
        for receiver in agents:
            if sender != receiver and random.random() < 0.2:
                orchestrator.connect_agents(sender, receiver)


def select_runtime_agents(
    agents: list[str],
    llm_agent_count: int,
    role_map: dict[str, str],
) -> list[str]:
    llm_agent_count = max(3, int(llm_agent_count))

    core = ["whale_0", "whale_1", "project_0"]
    retails = [name for name in agents if role_of(name, role_map) == "retail"]

    needed = max(0, llm_agent_count - len(core))
    selected = core + retails[:needed]
    return [name for name in selected if name in agents]


def offline_llm(_prompt: str) -> dict[str, Any]:
    return {
        "thought": "offline mode: conservative hold",
        "speak": None,
        "action": None,
    }


def build_runtime(
    orchestrator: Simulation_Orchestrator,
    *,
    runtime_agent_ids: list[str],
    bootstrap_by_id: dict[str, AgentBootstrap],
    offline_rules: bool,
    llm_max_concurrent: int | None = None,
) -> AgentRuntime:
    agents = []
    shared_brain: LLMBrain | None = None
    if not offline_rules:
        router_kwargs: dict[str, Any] = {}
        if llm_max_concurrent is not None:
            router_kwargs["max_concurrent"] = int(llm_max_concurrent)
        shared_router = LLMRouter(**router_kwargs)
        shared_brain = LLMBrain(router=shared_router)

    for agent_id in runtime_agent_ids:
        spec = bootstrap_by_id[agent_id]
        role = spec.role
        community_id = spec.community_id
        profile = spec.profile

        if role == "whale":
            agent = WhaleAgent(
                agent_id=agent_id,
                community_id=community_id,
                llm_callable=offline_llm if offline_rules else None,
                profile=profile,
                brain=shared_brain if not offline_rules else None,
            )
        elif role == "project":
            agent = ProjectAgent(
                agent_id=agent_id,
                community_id=community_id,
                llm_callable=offline_llm if offline_rules else None,
                profile=profile,
                brain=shared_brain if not offline_rules else None,
            )
        else:
            agent = RetailAgent(
                agent_id=agent_id,
                community_id=community_id,
                llm_callable=offline_llm if offline_rules else None,
                profile=profile,
                brain=shared_brain if not offline_rules else None,
            )

        agents.append(agent)

    return AgentRuntime(orchestrator=orchestrator, agents=agents)


def preflight_api_or_raise(
    required_roles: set[str],
    timeout: float,
    logger: logging.Logger,
) -> None:
    cfg = load_llm_config()
    api_key = cfg.openai.resolved_api_key()
    if not api_key:
        raise RuntimeError(
            "API mode requires key. Set OPENAI_API_KEY or providers.openai.api_key first."
        )

    adapter = OpenAIChatAdapter(
        api_key=api_key,
        base_url=cfg.openai.base_url,
        organization=cfg.openai.organization,
        project=cfg.openai.project,
    )

    failures: list[str] = []
    for role in sorted(required_roles):
        route = cfg.roles.get(role)
        if route is None:
            failures.append(f"role={role}: route missing in llm config")
            continue
        if str(route.backend).strip().lower() != "openai":
            failures.append(
                f"role={role}: backend={route.backend} is not openai-compatible"
            )
            continue

        start = time.perf_counter()
        try:
            raw = adapter.generate(
                model=route.model,
                prompt=(
                    "Return strict JSON with keys thought, speak, action. "
                    "Set thought='api-ok', speak=null, action=null."
                ),
                timeout=float(timeout),
                schema=None,
            )
            latency = time.perf_counter() - start

            if isinstance(raw, str):
                parsed = json.loads(raw)
            elif isinstance(raw, dict):
                parsed = raw
            else:
                raise ValueError("response must be dict or JSON string")

            required_fields = {"thought", "speak", "action"}
            if not required_fields.issubset(set(parsed.keys())):
                missing = sorted(required_fields - set(parsed.keys()))
                raise ValueError(f"missing fields: {missing}")

            logger.info(
                "[CHECK] [API OK] role=%-8s model=%-20s latency=%.2fs",
                role,
                route.model,
                latency,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"role={role}, model={route.model}: {exc}")

    if failures:
        text = "\n".join(f"- {item}" for item in failures)
        raise RuntimeError("API preflight failed:\n" + text)


def _resolve_role(orchestrator: Simulation_Orchestrator, agent_id: str) -> str:
    try:
        meta = orchestrator.topology.get_agent_meta(agent_id)
        role = str(meta.get("role", "")).strip().lower()
        if role:
            return role
    except Exception:  # noqa: BLE001
        pass
    return role_of(agent_id)


def _extract_ust_price(snapshot: dict[str, Any]) -> Decimal:
    pool_a = snapshot.get("pools", {}).get("Pool_A", {})
    reserve_x = Decimal(str(pool_a.get("reserve_x", "0")))
    reserve_y = Decimal(str(pool_a.get("reserve_y", "0")))
    if reserve_x <= 0:
        return Decimal("0")
    return reserve_y / reserve_x


def _contains_panic(text: str) -> bool:
    lower = str(text).lower()
    return any(term in lower for term in PANIC_TERMS)


def _count_overload_people_by_read_tick(conn: sqlite3.Connection, read_tick: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM inbox_overload_log
        WHERE tick = ? AND dropped_count > 0
        """,
        (int(read_tick),),
    ).fetchone()
    return int(row[0]) if row else 0


def _read_tick_for_settlement_tick(settlement_tick: int) -> int:
    # Inbox overload is logged during agent cognition before step_tick increments time.
    # So the overload row for settlement tick T is recorded at read tick T-1.
    return max(0, int(settlement_tick) - 1)


def _count_overload_people_for_settlement_tick(
    conn: sqlite3.Connection, settlement_tick: int
) -> int:
    read_tick = _read_tick_for_settlement_tick(settlement_tick)
    return _count_overload_people_by_read_tick(conn, read_tick)


def _log_retail_swap_summary(
    logger: logging.Logger,
    orchestrator: Simulation_Orchestrator,
    report: TickSettlementReport,
) -> None:
    retail_swaps = [
        item
        for item in report.receipts
        if item.action_type == "SWAP"
        and _resolve_role(orchestrator, item.agent_id) == "retail"
    ]

    slippage_failed = sum(
        1
        for item in retail_swaps
        if item.status == "failed" and item.error_code == "SlippageExceededError"
    )
    balance_failed = sum(
        1
        for item in retail_swaps
        if item.status == "failed" and item.error_code in BALANCE_ERRORS
    )
    max_gas_bid = max((item.gas_bid for item in retail_swaps), default=Decimal("0"))

    logger.info(
        "[ACTION] Retail SWAP 总笔数: %d | 滑点失败: %d | 余额失败: %d | 最高Gas出价: %s",
        len(retail_swaps),
        slippage_failed,
        balance_failed,
        str(max_gas_bid),
    )


def _log_whale_actions(
    logger: logging.Logger,
    orchestrator: Simulation_Orchestrator,
    report: TickSettlementReport,
) -> None:
    whale_swap_receipts = [
        item
        for item in report.receipts
        if item.action_type == "SWAP"
        and _resolve_role(orchestrator, item.agent_id) in {"whale", "project"}
    ]

    for item in whale_swap_receipts:
        if item.status == "success":
            amount_out = None
            if item.result is not None:
                amount_out = item.result.get("amount_out")
            logger.info(
                "[WHALE-ACTION] %s SWAP success | gas=%s | amount_out=%s",
                item.agent_id,
                str(item.gas_bid),
                str(amount_out) if amount_out is not None else "-",
            )
        else:
            logger.info(
                "[WHALE-ACTION] %s SWAP %s | gas=%s | reason=%s",
                item.agent_id,
                item.status,
                str(item.gas_bid),
                item.error_code or item.error_message or "unknown",
            )

    speak_by_event: dict[str, dict[str, Any]] = {}
    for delivery in report.semantic_deliveries:
        sender = str(delivery.get("sender", ""))
        role = _resolve_role(orchestrator, sender)
        if role not in {"whale", "project"}:
            continue

        event_id = str(delivery.get("event_id", ""))
        if not event_id:
            continue

        payload = speak_by_event.get(event_id)
        if payload is None:
            payload = {
                "sender": sender,
                "channel": str(delivery.get("channel", "FORUM")),
                "emit_tick": int(delivery.get("emit_tick", report.tick)),
                "raw_text": str(delivery.get("raw_text", "")).strip(),
                "deliveries": 0,
            }
            speak_by_event[event_id] = payload
        payload["deliveries"] += 1

    for event_id in sorted(speak_by_event.keys()):
        item = speak_by_event[event_id]
        message = item["raw_text"].replace("\n", " ")
        if len(message) > 120:
            message = message[:117] + "..."
        logger.info(
            "[WHALE-ACTION] %s SPEAK channel=%s | deliveries=%d | event=%s | msg=%s",
            item["sender"],
            item["channel"],
            int(item["deliveries"]),
            event_id,
            message,
        )


def _log_governance_exec(logger: logging.Logger, report: TickSettlementReport) -> None:
    for update in report.governance_applied_updates:
        if update.status == "applied":
            if update.previous_value is not None:
                logger.info(
                    "[GOV-EXEC] 参数 %s 已修改为 %s (proposal=%s, old=%s)",
                    update.parameter,
                    update.new_value,
                    update.proposal_id,
                    update.previous_value,
                )
            else:
                logger.info(
                    "[GOV-EXEC] 参数 %s 已修改为 %s (proposal=%s)",
                    update.parameter,
                    update.new_value,
                    update.proposal_id,
                )
        else:
            logger.info(
                "[GOV-EXEC] 参数 %s 应用失败 (proposal=%s, reason=%s)",
                update.parameter,
                update.proposal_id,
                update.error or "unknown",
            )


def simulate(
    orchestrator: Simulation_Orchestrator,
    runtime: AgentRuntime,
    *,
    agents: list[str],
    role_map: dict[str, str],
    ticks: int,
    max_inbox_size: int,
    logger: logging.Logger,
    progress_enabled: bool,
    progress_interval: int,
    offline_rules: bool,
    black_swan_schedule: dict[int, list[dict[str, Any]]] | None = None,
    governance_dos_attack: bool = False,
    dos_attacker_id: str = "whale_1",
    dos_sell_ust: Decimal = Decimal("300000"),
    social_eclipse_attack: bool = False,
    eclipse_attacker_id: str = "whale_1",
    eclipse_trigger_tick: int = 1,
    eclipse_window_ticks: int = 5,
    eclipse_fud_message: str = DEFAULT_ECLIPSE_FUD_MESSAGE,
    eclipse_sell_ust: Decimal = Decimal("300000"),
    traffic_profile: str = "stress",
    governance_hijack_attack: bool = False,
    hijack_attacker_id: str = "whale_1",
    hijack_trigger_tick: int = 2,
    hijack_proposal_text: str = "Lower swap fee to 0.0001 for holder growth",
    hijack_force_approve: bool = True,
) -> dict[str, Any]:
    retail_agents = [name for name in agents if role_of(name, role_map) == "retail"]
    if not retail_agents:
        raise ValueError("at least one retail agent is required")
    profile = str(traffic_profile).strip().lower()
    if profile not in TRAFFIC_PROFILE_CHOICES:
        raise ValueError(f"traffic_profile must be one of: {TRAFFIC_PROFILE_CHOICES}")

    proposal_id: str | None = None
    llm_calls_total = 0
    sleeping_total = 0
    ust_price_by_tick: dict[int, Decimal] = {}
    dos_stats: dict[str, Any] = {
        "enabled": bool(governance_dos_attack),
        "attacker_id": dos_attacker_id,
        "placeholder_submitted": 0,
        "placeholder_rejected": 0,
        "placeholder_ids": [],
        "project_proposal_rejected": False,
        "project_reject_reason": None,
        "project_proposal_id": None,
    }
    hijack_stats: dict[str, Any] = {
        "enabled": bool(governance_hijack_attack),
        "attacker_id": str(hijack_attacker_id),
        "trigger_tick": int(hijack_trigger_tick),
        "proposal_text": str(hijack_proposal_text),
        "proposal_id": None,
        "proposal_submitted": False,
        "proposal_rejected": False,
        "proposal_reject_reason": None,
        "votes_submitted": 0,
        "votes_failed": 0,
        "force_approve": bool(hijack_force_approve),
    }
    eclipse_window_start = int(eclipse_trigger_tick)
    eclipse_window_end = int(eclipse_trigger_tick) + int(eclipse_window_ticks) - 1
    eclipse_stats: dict[str, Any] = {
        "enabled": bool(social_eclipse_attack),
        "attacker_id": eclipse_attacker_id,
        "traffic_profile": profile,
        "trigger_tick": int(eclipse_trigger_tick),
        "window_ticks": int(eclipse_window_ticks),
        "window_start_tick": int(eclipse_window_start),
        "window_end_tick": int(eclipse_window_end),
        "fud_message": eclipse_fud_message,
        "triggered": False,
        "tx_failed_sum_window": 0,
        "mempool_congestion_peak_window": 0,
        "attacker_settled_count_window": 0,
        "attacker_attempted_count_window": 0,
        "attacker_failed_congestion_like_window": 0,
        "attacker_success_count_window": 0,
        "attacker_gas_paid_sum_window": "0",
        "retail_settled_count_window": 0,
        "retail_attempted_count_window": 0,
        "retail_failed_congestion_like_window": 0,
        "retail_success_count_window": 0,
        "retail_gas_paid_sum_window": "0",
        "attacker_tx_success_rate_window": "0",
        "attacker_tx_success_rate_executable_window": "0",
        "retail_tx_success_rate_window": "0",
        "retail_tx_success_rate_executable_window": "0",
        "attacker_vs_retail_success_rate_gap_window": "0",
        "attacker_vs_retail_success_rate_gap_executable_window": "0",
        "avg_gas_paid_attacker_window": "0",
        "avg_gas_paid_retail_window": "0",
        "avg_gas_paid_gap_window": "0",
        "max_gas_bid_in_window": "0",
        "max_gas_bid_attacker_in_window": "0",
        "max_gas_bid_retail_in_window": "0",
        "attacker_capped_in_window": False,
        "attacker_min_effective_gas_in_window": None,
        "attacker_first_cap_tick": None,
    }
    attacker_gas_paid_sum = Decimal("0")
    retail_gas_paid_sum = Decimal("0")
    max_gas_bid_window = Decimal("0")
    max_gas_bid_attacker_window = Decimal("0")
    max_gas_bid_retail_window = Decimal("0")
    capped_tx_count_total = 0
    attacker_capped_tx_count_total = 0
    capped_tx_count_window = 0
    attacker_capped_tx_count_window = 0
    attacker_min_effective_gas_window: Decimal | None = None
    attacker_first_cap_tick: int | None = None
    failed_reason_totals: dict[str, int] = {
        "slippage": 0,
        "balance": 0,
        "validation": 0,
        "invariant": 0,
        "congestion": 0,
        "other": 0,
    }
    failed_reason_window: dict[str, int] = {
        "slippage": 0,
        "balance": 0,
        "validation": 0,
        "invariant": 0,
        "congestion": 0,
        "other": 0,
    }
    retail_failed_reason_window: dict[str, int] = {
        "slippage": 0,
        "balance": 0,
        "validation": 0,
        "invariant": 0,
        "congestion": 0,
        "other": 0,
    }

    conn = sqlite3.connect(orchestrator.engine.get_db_path())
    try:
        for tick in range(ticks):
            scheduled_actions = (black_swan_schedule or {}).get(tick + 1, [])
            for action in scheduled_actions:
                if action["kind"] == "transaction":
                    orchestrator.submit_transaction(
                        action["agent_id"],
                        action["action_type"],
                        action["params"],
                        gas_price=action["gas_price"],
                    )
                else:
                    orchestrator.submit_event(
                        action["agent_id"],
                        action["action_type"],
                        action["params"],
                    )

            if social_eclipse_attack and (tick + 1) == eclipse_trigger_tick:
                eclipse_stats["triggered"] = True
                orchestrator.submit_transaction(
                    eclipse_attacker_id,
                    "SWAP",
                    {
                        "pool_name": "Pool_A",
                        "token_in": "UST",
                        "amount": str(eclipse_sell_ust),
                        "slippage_tolerance": "0.50",
                    },
                    gas_price="998",
                )
                orchestrator.submit_event(
                    eclipse_attacker_id,
                    "SPEAK",
                    {
                        "target": "forum",
                        "message": eclipse_fud_message,
                        "mode": "new",
                    },
                )
                logger.info(
                    "[ECLIPSE] attack triggered | attacker=%s | tick=%d | sell_ust=%s",
                    eclipse_attacker_id,
                    int(eclipse_trigger_tick),
                    str(eclipse_sell_ust),
                )
                logger.info(
                    "[ECLIPSE] fud injected | attacker=%s | channel=FORUM | msg=%s",
                    eclipse_attacker_id,
                    eclipse_fud_message,
                )

            if governance_dos_attack and tick == 0:
                orchestrator.submit_transaction(
                    dos_attacker_id,
                    "SWAP",
                    {
                        "pool_name": "Pool_A",
                        "token_in": "UST",
                        "amount": str(dos_sell_ust),
                        "slippage_tolerance": "0.50",
                    },
                    gas_price="995",
                )
                orchestrator.submit_event(
                    dos_attacker_id,
                    "SPEAK",
                    {
                        "target": "forum",
                        "message": "Governance is too slow. Panic first, rules later.",
                        "mode": "new",
                    },
                )
                for idx, text in enumerate(DOS_PLACEHOLDER_PROPOSALS, start=1):
                    try:
                        placeholder_id = orchestrator.submit_event(
                            dos_attacker_id,
                            "PROPOSE",
                            {"proposal_text": text},
                        )
                    except Exception as exc:  # noqa: BLE001
                        dos_stats["placeholder_rejected"] = int(
                            dos_stats["placeholder_rejected"]
                        ) + 1
                        logger.info(
                            "[GOV-DOS] attacker placeholder proposal %d rejected | reason=%s",
                            idx,
                            str(exc),
                        )
                    else:
                        dos_stats["placeholder_submitted"] = int(
                            dos_stats["placeholder_submitted"]
                        ) + 1
                        dos_stats["placeholder_ids"].append(placeholder_id)
                        logger.info(
                            "[GOV-DOS] attacker placeholder proposal %d accepted | proposal=%s",
                            idx,
                            str(placeholder_id),
                        )

            if governance_hijack_attack and tick == int(hijack_trigger_tick) - 1:
                try:
                    hijack_proposal_id = orchestrator.submit_event(
                        hijack_attacker_id,
                        "PROPOSE",
                        {"proposal_text": str(hijack_proposal_text)},
                    )
                except Exception as exc:  # noqa: BLE001
                    hijack_stats["proposal_rejected"] = True
                    hijack_stats["proposal_reject_reason"] = str(exc)
                    logger.info(
                        "[HIJACK] malicious proposal rejected | attacker=%s | reason=%s",
                        str(hijack_attacker_id),
                        str(exc),
                    )
                else:
                    hijack_stats["proposal_id"] = hijack_proposal_id
                    hijack_stats["proposal_submitted"] = True
                    logger.info(
                        "[HIJACK] malicious proposal accepted | attacker=%s | proposal=%s",
                        str(hijack_attacker_id),
                        str(hijack_proposal_id),
                    )
                    if bool(hijack_force_approve):
                        voters = list(agents)
                        for voter in voters:
                            try:
                                orchestrator.submit_event(
                                    voter,
                                    "VOTE",
                                    {
                                        "proposal_id": hijack_proposal_id,
                                        "decision": "approve",
                                    },
                                )
                            except Exception:  # noqa: BLE001
                                hijack_stats["votes_failed"] = int(hijack_stats["votes_failed"]) + 1
                            else:
                                hijack_stats["votes_submitted"] = int(
                                    hijack_stats["votes_submitted"]
                                ) + 1
                        logger.info(
                            "[HIJACK] force-approve votes submitted=%d failed=%d",
                            int(hijack_stats["votes_submitted"]),
                            int(hijack_stats["votes_failed"]),
                        )

            # Governance stream
            if tick == 1:
                try:
                    proposal_id = orchestrator.submit_event(
                        "project_0",
                        "PROPOSE",
                        {
                            "proposal_text": "Disable minting and set swap fee to 0.01",
                        },
                    )
                    dos_stats["project_proposal_id"] = proposal_id
                except (ProposalLimitError, ProposalMitigationError) as exc:
                    proposal_id = None
                    dos_stats["project_proposal_rejected"] = True
                    dos_stats["project_reject_reason"] = str(exc)
                    logger.info(
                        "[GOV-DOS] project rescue proposal rejected | reason=%s",
                        str(exc),
                    )
            if tick == 2 and proposal_id:
                voters = ["whale_0", "whale_1", "project_0"] + retail_agents[:2]
                reject_voter = voters[-1] if len(voters) >= 4 else None
                for voter in voters:
                    decision = "reject" if reject_voter and voter == reject_voter else "approve"
                    orchestrator.submit_event(
                        voter,
                        "VOTE",
                        {
                            "proposal_id": proposal_id,
                            "decision": decision,
                        },
                    )

            # Semantic stream
            speaker = random.choice(agents)
            msg = random.choice(
                [
                    "UST depeg panic is spreading",
                    "rumor says liquidity is draining",
                    "market stable for now",
                    "panic is spreading across communities",
                ]
            )
            orchestrator.submit_event(
                speaker,
                "SPEAK",
                {
                    "target": "forum",
                    "message": msg,
                    "mode": "new",
                },
            )

            # Economic stream (intentionally overloaded > max_tx_per_tick).
            target_submissions = orchestrator.max_tx_per_tick + 25
            if profile == "eval":
                retail_budget, minting_allowed = _build_eval_retail_budget(
                    orchestrator,
                    retail_agents,
                )
                submitted = 0
                attempts = 0
                max_attempts = target_submissions * 4
                while submitted < target_submissions and attempts < max_attempts:
                    attempts += 1
                    actor = random.choice(retail_agents)
                    gas = random.randint(1, 20)
                    if _submit_eval_retail_tx(
                        orchestrator,
                        actor=actor,
                        budget=retail_budget,
                        minting_allowed=minting_allowed,
                        gas_int=gas,
                    ):
                        submitted += 1
            else:
                for _ in range(target_submissions):
                    actor = random.choice(retail_agents)
                    action = random.choice(["SWAP", "UST_TO_LUNA", "LUNA_TO_UST"])
                    gas = random.randint(1, 20)
                    if action == "SWAP":
                        pool = random.choice(["Pool_A", "Pool_B"])
                        token_in = "UST" if pool == "Pool_A" else random.choice(["LUNA", "USDC"])
                        orchestrator.submit_transaction(
                            actor,
                            "SWAP",
                            {
                                "pool_name": pool,
                                "token_in": token_in,
                                "amount": str(random.randint(5, 60)),
                                "slippage_tolerance": "0.15",
                            },
                            gas_price=str(gas),
                        )
                    elif action == "UST_TO_LUNA":
                        orchestrator.submit_transaction(
                            actor,
                            "UST_TO_LUNA",
                            {"amount_ust": str(random.randint(5, 40))},
                            gas_price=str(gas),
                        )
                    else:
                        orchestrator.submit_transaction(
                            actor,
                            "LUNA_TO_UST",
                            {"amount_luna": str(random.randint(1, 20))},
                            gas_price=str(gas),
                        )

            runtime_report = runtime.run_tick(max_inbox_size=max_inbox_size)
            report = runtime_report.settlement

            llm_calls_total += int(runtime_report.llm_calls)
            sleeping_total += int(runtime_report.sleeping_agents)
            api_calls_total = 0 if offline_rules else llm_calls_total

            ust_price = _extract_ust_price(report.end_snapshot)
            ust_price_by_tick[report.tick] = ust_price
            if progress_enabled and report.tick % progress_interval == 0:
                logger.info(
                    "[PROGRESS] Tick %d/%d | Mempool: %d | UST: %s | LLM Calls: %d",
                    int(report.tick),
                    int(ticks),
                    int(report.mempool_congestion),
                    str(ust_price),
                    int(api_calls_total),
                )

            rumor_count = sum(
                1
                for item in report.semantic_deliveries
                if str(item.get("transform_tag", "none")) != "none"
                or _contains_panic(str(item.get("perceived_text", "")))
            )
            overload_people = _count_overload_people_for_settlement_tick(conn, report.tick)
            logger.info(
                "[SOCIAL] 产生流言: %d | 触发认知过载: %d 人次",
                int(rumor_count),
                int(overload_people),
            )

            _log_retail_swap_summary(logger, orchestrator, report)
            _log_whale_actions(logger, orchestrator, report)
            _log_governance_exec(logger, report)

            for reason, cnt in report.failed_reason_counts.items():
                failed_reason_totals[reason] = failed_reason_totals.get(reason, 0) + int(cnt)
            for item in report.receipts:
                if Decimal(item.gas_effective) < Decimal(item.gas_bid):
                    capped_tx_count_total += 1
                    if item.agent_id == eclipse_attacker_id:
                        attacker_capped_tx_count_total += 1

            if eclipse_window_start <= int(report.tick) <= eclipse_window_end:
                failed_now = sum(1 for item in report.receipts if item.status == "failed")
                eclipse_stats["tx_failed_sum_window"] = int(
                    eclipse_stats["tx_failed_sum_window"]
                ) + int(failed_now)
                eclipse_stats["mempool_congestion_peak_window"] = max(
                    int(eclipse_stats["mempool_congestion_peak_window"]),
                    int(report.mempool_congestion),
                )
                dropped_meta = list(report.congestion_dropped_meta)
                dropped_ids = [str(item.get("agent_id", "")) for item in dropped_meta]
                retail_dropped = 0
                attacker_dropped = 0
                for meta in dropped_meta:
                    dropped_id = str(meta.get("agent_id", ""))
                    if dropped_id == eclipse_attacker_id:
                        attacker_dropped += 1
                        raw_gas = Decimal(str(meta.get("raw_gas", "0")))
                        effective_gas = Decimal(str(meta.get("effective_gas", "0")))
                        if effective_gas < raw_gas:
                            capped_tx_count_window += 1
                            attacker_capped_tx_count_window += 1
                            attacker_min_effective_gas_window = (
                                effective_gas
                                if attacker_min_effective_gas_window is None
                                else min(attacker_min_effective_gas_window, effective_gas)
                            )
                            if attacker_first_cap_tick is None:
                                attacker_first_cap_tick = int(report.tick)
                    elif role_of(dropped_id, role_map) == "retail":
                        retail_dropped += 1
                eclipse_stats["retail_attempted_count_window"] = int(
                    eclipse_stats["retail_attempted_count_window"]
                ) + int(retail_dropped)
                eclipse_stats["retail_failed_congestion_like_window"] = int(
                    eclipse_stats["retail_failed_congestion_like_window"]
                ) + int(retail_dropped)
                eclipse_stats["attacker_attempted_count_window"] = int(
                    eclipse_stats["attacker_attempted_count_window"]
                ) + int(attacker_dropped)
                eclipse_stats["attacker_failed_congestion_like_window"] = int(
                    eclipse_stats["attacker_failed_congestion_like_window"]
                ) + int(attacker_dropped)
                for item in report.receipts:
                    max_gas_bid_window = max(max_gas_bid_window, Decimal(item.gas_bid))
                    if Decimal(item.gas_effective) < Decimal(item.gas_bid):
                        capped_tx_count_window += 1
                    if item.agent_id == eclipse_attacker_id:
                        eclipse_stats["attacker_attempted_count_window"] = int(
                            eclipse_stats["attacker_attempted_count_window"]
                        ) + 1
                        eclipse_stats["attacker_settled_count_window"] = int(
                            eclipse_stats["attacker_settled_count_window"]
                        ) + 1
                        if item.status == "success":
                            eclipse_stats["attacker_success_count_window"] = int(
                                eclipse_stats["attacker_success_count_window"]
                            ) + 1
                        elif item.status == "failed":
                            attacker_reason = _classify_receipt_reason(item.error_code)
                            if attacker_reason == "congestion":
                                eclipse_stats["attacker_failed_congestion_like_window"] = int(
                                    eclipse_stats["attacker_failed_congestion_like_window"]
                                ) + 1
                        attacker_gas_paid_sum += Decimal(item.gas_paid)
                        if Decimal(item.gas_effective) < Decimal(item.gas_bid):
                            attacker_capped_tx_count_window += 1
                            attacker_min_effective_gas_window = (
                                Decimal(item.gas_effective)
                                if attacker_min_effective_gas_window is None
                                else min(attacker_min_effective_gas_window, Decimal(item.gas_effective))
                            )
                            if attacker_first_cap_tick is None:
                                attacker_first_cap_tick = int(report.tick)
                        max_gas_bid_attacker_window = max(
                            max_gas_bid_attacker_window,
                            Decimal(item.gas_bid),
                        )
                    elif role_of(item.agent_id, role_map) == "retail":
                        eclipse_stats["retail_attempted_count_window"] = int(
                            eclipse_stats["retail_attempted_count_window"]
                        ) + 1
                        eclipse_stats["retail_settled_count_window"] = int(
                            eclipse_stats["retail_settled_count_window"]
                        ) + 1
                        if item.status == "success":
                            eclipse_stats["retail_success_count_window"] = int(
                                eclipse_stats["retail_success_count_window"]
                            ) + 1
                        retail_gas_paid_sum += Decimal(item.gas_paid)
                        max_gas_bid_retail_window = max(
                            max_gas_bid_retail_window,
                            Decimal(item.gas_bid),
                        )
                        if item.status == "failed":
                            reason = _classify_receipt_reason(item.error_code)
                            retail_failed_reason_window[reason] = (
                                retail_failed_reason_window.get(reason, 0) + 1
                            )
                            if reason == "congestion":
                                eclipse_stats["retail_failed_congestion_like_window"] = int(
                                    eclipse_stats["retail_failed_congestion_like_window"]
                                ) + 1
                for reason, cnt in report.failed_reason_counts.items():
                    failed_reason_window[reason] = failed_reason_window.get(reason, 0) + int(cnt)
                if int(report.congestion_dropped_retail_count) > 0:
                    retail_failed_reason_window["congestion"] = (
                        retail_failed_reason_window.get("congestion", 0)
                        + int(report.congestion_dropped_retail_count)
                    )
    finally:
        conn.close()

    attacker_settled = Decimal(str(eclipse_stats["attacker_settled_count_window"]))
    attacker_attempted = Decimal(str(eclipse_stats["attacker_attempted_count_window"]))
    attacker_failed_congestion_like = Decimal(
        str(eclipse_stats["attacker_failed_congestion_like_window"])
    )
    attacker_success = Decimal(str(eclipse_stats["attacker_success_count_window"]))
    retail_settled = Decimal(str(eclipse_stats["retail_settled_count_window"]))
    retail_attempted = Decimal(str(eclipse_stats["retail_attempted_count_window"]))
    retail_failed_congestion_like = Decimal(
        str(eclipse_stats["retail_failed_congestion_like_window"])
    )
    retail_success = Decimal(str(eclipse_stats["retail_success_count_window"]))

    attacker_raw_denominator = attacker_attempted if attacker_attempted > 0 else attacker_settled
    retail_raw_denominator = retail_attempted if retail_attempted > 0 else retail_settled
    attacker_success_rate = (
        Decimal("0") if attacker_raw_denominator <= 0 else attacker_success / attacker_raw_denominator
    )
    retail_success_rate = (
        Decimal("0") if retail_raw_denominator <= 0 else retail_success / retail_raw_denominator
    )
    avg_gas_attacker = (
        Decimal("0") if attacker_settled <= 0 else attacker_gas_paid_sum / attacker_settled
    )
    avg_gas_retail = Decimal("0") if retail_settled <= 0 else retail_gas_paid_sum / retail_settled
    attacker_executable_denominator = attacker_attempted - attacker_failed_congestion_like
    if attacker_executable_denominator < 0:
        attacker_executable_denominator = Decimal("0")
    retail_executable_denominator = retail_attempted - retail_failed_congestion_like
    if retail_executable_denominator < 0:
        retail_executable_denominator = Decimal("0")
    attacker_executable_success_rate = (
        Decimal("0")
        if attacker_executable_denominator <= 0
        else attacker_success / attacker_executable_denominator
    )
    retail_executable_success_rate = (
        Decimal("0")
        if retail_executable_denominator <= 0
        else retail_success / retail_executable_denominator
    )

    eclipse_stats["attacker_gas_paid_sum_window"] = str(attacker_gas_paid_sum)
    eclipse_stats["retail_gas_paid_sum_window"] = str(retail_gas_paid_sum)
    eclipse_stats["attacker_tx_success_rate_window"] = str(attacker_success_rate)
    eclipse_stats["attacker_tx_success_rate_executable_window"] = str(
        attacker_executable_success_rate
    )
    eclipse_stats["retail_tx_success_rate_window"] = str(retail_success_rate)
    eclipse_stats["retail_tx_success_rate_executable_window"] = str(
        retail_executable_success_rate
    )
    eclipse_stats["attacker_vs_retail_success_rate_gap_window"] = str(
        attacker_success_rate - retail_success_rate
    )
    eclipse_stats["attacker_vs_retail_success_rate_gap_executable_window"] = str(
        attacker_executable_success_rate - retail_executable_success_rate
    )
    eclipse_stats["attacker_raw_denominator_window"] = str(attacker_raw_denominator)
    eclipse_stats["retail_raw_denominator_window"] = str(retail_raw_denominator)
    eclipse_stats["attacker_executable_denominator_window"] = str(attacker_executable_denominator)
    eclipse_stats["retail_executable_denominator_window"] = str(retail_executable_denominator)
    eclipse_stats["avg_gas_paid_attacker_window"] = str(avg_gas_attacker)
    eclipse_stats["avg_gas_paid_retail_window"] = str(avg_gas_retail)
    eclipse_stats["avg_gas_paid_gap_window"] = str(avg_gas_attacker - avg_gas_retail)
    eclipse_stats["max_gas_bid_in_window"] = str(max_gas_bid_window)
    eclipse_stats["max_gas_bid_attacker_in_window"] = str(max_gas_bid_attacker_window)
    eclipse_stats["max_gas_bid_retail_in_window"] = str(max_gas_bid_retail_window)
    eclipse_stats["capped_tx_count_window"] = int(capped_tx_count_window)
    eclipse_stats["attacker_capped_tx_count_window"] = int(attacker_capped_tx_count_window)
    eclipse_stats["capped_tx_count_total"] = int(capped_tx_count_total)
    eclipse_stats["attacker_capped_tx_count_total"] = int(attacker_capped_tx_count_total)
    eclipse_stats["attacker_capped_in_window"] = bool(attacker_capped_tx_count_window > 0)
    eclipse_stats["attacker_min_effective_gas_in_window"] = (
        str(attacker_min_effective_gas_window)
        if attacker_min_effective_gas_window is not None
        else None
    )
    eclipse_stats["attacker_first_cap_tick"] = attacker_first_cap_tick
    eclipse_stats["failed_reason_window"] = {
        key: int(value) for key, value in failed_reason_window.items()
    }
    eclipse_stats["retail_failed_reason_window"] = {
        key: int(value) for key, value in retail_failed_reason_window.items()
    }

    return {
        "llm_calls_total": llm_calls_total,
        "api_calls_total": 0 if offline_rules else llm_calls_total,
        "sleeping_total": sleeping_total,
        "ticks": ticks,
        "ust_price_by_tick": ust_price_by_tick,
        "governance_dos": dos_stats,
        "governance_hijack": hijack_stats,
        "social_eclipse": eclipse_stats,
        "failed_reason_totals": {key: int(value) for key, value in failed_reason_totals.items()},
    }


def _read_metrics_rows(metrics_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with metrics_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                {
                    "tick": int(row["tick"]),
                    "tx_failed": Decimal(str(row["tx_failed"])),
                    "mempool_congestion": Decimal(str(row["mempool_congestion"])),
                }
            )
    return rows


def _mean_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _window_slice(rows: list[dict[str, Any]], start_tick: int, end_tick: int) -> list[dict[str, Any]]:
    return [item for item in rows if start_tick <= int(item["tick"]) <= end_tick]


def _load_run_data(run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / "summary.json"
    metrics_path = run_dir / "metrics.csv"
    if not summary_path.exists():
        raise FileNotFoundError(f"summary not found: {summary_path}")
    if not metrics_path.exists():
        raise FileNotFoundError(f"metrics not found: {metrics_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    metrics_rows = _read_metrics_rows(metrics_path)
    return {"summary": summary, "metrics_rows": metrics_rows}


def _as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def generate_social_eclipse_comparison(
    *,
    baseline_dir: Path,
    attack_dir: Path,
    output_dir: Path,
    logger: logging.Logger,
) -> dict[str, Path]:
    baseline = _load_run_data(baseline_dir)
    attack = _load_run_data(attack_dir)

    base_summary = baseline["summary"]
    attack_summary = attack["summary"]
    base_rows = baseline["metrics_rows"]
    attack_rows = attack["metrics_rows"]

    attack_eclipse = attack_summary.get("social_eclipse", {})
    start_tick = int(attack_eclipse.get("window_start_tick", 1))
    end_tick = int(attack_eclipse.get("window_end_tick", start_tick))

    base_window = _window_slice(base_rows, start_tick, end_tick)
    attack_window = _window_slice(attack_rows, start_tick, end_tick)

    base_tx_failed_mean = _mean_decimal([item["tx_failed"] for item in base_window])
    attack_tx_failed_mean = _mean_decimal([item["tx_failed"] for item in attack_window])
    base_cong_mean = _mean_decimal([item["mempool_congestion"] for item in base_window])
    attack_cong_mean = _mean_decimal([item["mempool_congestion"] for item in attack_window])

    base_tx_failed_peak = max((item["tx_failed"] for item in base_window), default=Decimal("0"))
    attack_tx_failed_peak = max(
        (item["tx_failed"] for item in attack_window), default=Decimal("0")
    )
    base_cong_peak = max(
        (item["mempool_congestion"] for item in base_window), default=Decimal("0")
    )
    attack_cong_peak = max(
        (item["mempool_congestion"] for item in attack_window), default=Decimal("0")
    )

    base_eclipse = base_summary.get("social_eclipse", {})
    attacker_success_base = _as_decimal(base_eclipse.get("attacker_tx_success_rate_window", "0"))
    retail_success_base = _as_decimal(base_eclipse.get("retail_tx_success_rate_window", "0"))
    attacker_success_exec_base = _as_decimal(
        base_eclipse.get("attacker_tx_success_rate_executable_window", "0")
    )
    retail_success_exec_base = _as_decimal(
        base_eclipse.get("retail_tx_success_rate_executable_window", "0")
    )
    attacker_success_attack = _as_decimal(
        attack_eclipse.get("attacker_tx_success_rate_window", "0")
    )
    retail_success_attack = _as_decimal(attack_eclipse.get("retail_tx_success_rate_window", "0"))
    attacker_success_exec_attack = _as_decimal(
        attack_eclipse.get("attacker_tx_success_rate_executable_window", "0")
    )
    retail_success_exec_attack = _as_decimal(
        attack_eclipse.get("retail_tx_success_rate_executable_window", "0")
    )

    avg_gas_attacker_base = _as_decimal(base_eclipse.get("avg_gas_paid_attacker_window", "0"))
    avg_gas_retail_base = _as_decimal(base_eclipse.get("avg_gas_paid_retail_window", "0"))
    avg_gas_attacker_attack = _as_decimal(
        attack_eclipse.get("avg_gas_paid_attacker_window", "0")
    )
    avg_gas_retail_attack = _as_decimal(
        attack_eclipse.get("avg_gas_paid_retail_window", "0")
    )

    max_gas_bid_window_base = _as_decimal(base_eclipse.get("max_gas_bid_in_window", "0"))
    max_gas_bid_window_attack = _as_decimal(attack_eclipse.get("max_gas_bid_in_window", "0"))
    max_gas_bid_attacker_base = _as_decimal(
        base_eclipse.get("max_gas_bid_attacker_in_window", "0")
    )
    max_gas_bid_attacker_attack = _as_decimal(
        attack_eclipse.get("max_gas_bid_attacker_in_window", "0")
    )
    max_gas_bid_retail_base = _as_decimal(
        base_eclipse.get("max_gas_bid_retail_in_window", "0")
    )
    max_gas_bid_retail_attack = _as_decimal(
        attack_eclipse.get("max_gas_bid_retail_in_window", "0")
    )
    capped_tx_count_base = _as_decimal(base_eclipse.get("capped_tx_count_window", "0"))
    capped_tx_count_attack = _as_decimal(attack_eclipse.get("capped_tx_count_window", "0"))
    attacker_capped_count_base = _as_decimal(
        base_eclipse.get("attacker_capped_tx_count_window", "0")
    )
    attacker_capped_count_attack = _as_decimal(
        attack_eclipse.get("attacker_capped_tx_count_window", "0")
    )
    attacker_capped_in_window_base = Decimal(
        int(bool(base_eclipse.get("attacker_capped_in_window", False)))
    )
    attacker_capped_in_window_attack = Decimal(
        int(bool(attack_eclipse.get("attacker_capped_in_window", False)))
    )
    attacker_min_effective_gas_base = _as_decimal(
        base_eclipse.get("attacker_min_effective_gas_in_window", "0")
    )
    attacker_min_effective_gas_attack = _as_decimal(
        attack_eclipse.get("attacker_min_effective_gas_in_window", "0")
    )
    attacker_first_cap_tick_base = _as_decimal(base_eclipse.get("attacker_first_cap_tick", "0"))
    attacker_first_cap_tick_attack = _as_decimal(
        attack_eclipse.get("attacker_first_cap_tick", "0")
    )
    failed_reason_base = base_eclipse.get("failed_reason_window", {})
    failed_reason_attack = attack_eclipse.get("failed_reason_window", {})
    retail_failed_base = base_eclipse.get("retail_failed_reason_window", {})
    retail_failed_attack = attack_eclipse.get("retail_failed_reason_window", {})

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "social_eclipse_comparison.csv"
    json_path = output_dir / "social_eclipse_comparison.json"

    rows = [
        ("window_start_tick", Decimal(start_tick), Decimal(start_tick)),
        ("window_end_tick", Decimal(end_tick), Decimal(end_tick)),
        ("tx_failed_mean_window", base_tx_failed_mean, attack_tx_failed_mean),
        ("tx_failed_peak_window", base_tx_failed_peak, attack_tx_failed_peak),
        ("mempool_congestion_mean_window", base_cong_mean, attack_cong_mean),
        ("mempool_congestion_peak_window", base_cong_peak, attack_cong_peak),
        ("attacker_tx_success_rate_window", attacker_success_base, attacker_success_attack),
        ("retail_tx_success_rate_window", retail_success_base, retail_success_attack),
        (
            "attacker_tx_success_rate_executable_window",
            attacker_success_exec_base,
            attacker_success_exec_attack,
        ),
        (
            "retail_tx_success_rate_executable_window",
            retail_success_exec_base,
            retail_success_exec_attack,
        ),
        (
            "attacker_vs_retail_success_rate_gap_window",
            attacker_success_base - retail_success_base,
            attacker_success_attack - retail_success_attack,
        ),
        (
            "attacker_vs_retail_success_rate_gap_executable_window",
            attacker_success_exec_base - retail_success_exec_base,
            attacker_success_exec_attack - retail_success_exec_attack,
        ),
        ("avg_gas_paid_attacker_window", avg_gas_attacker_base, avg_gas_attacker_attack),
        ("avg_gas_paid_retail_window", avg_gas_retail_base, avg_gas_retail_attack),
        (
            "avg_gas_paid_gap_window",
            avg_gas_attacker_base - avg_gas_retail_base,
            avg_gas_attacker_attack - avg_gas_retail_attack,
        ),
        ("max_gas_bid_in_window", max_gas_bid_window_base, max_gas_bid_window_attack),
        (
            "max_gas_bid_attacker_in_window",
            max_gas_bid_attacker_base,
            max_gas_bid_attacker_attack,
        ),
        (
            "max_gas_bid_retail_in_window",
            max_gas_bid_retail_base,
            max_gas_bid_retail_attack,
        ),
        ("capped_tx_count_window", capped_tx_count_base, capped_tx_count_attack),
        (
            "attacker_capped_tx_count_window",
            attacker_capped_count_base,
            attacker_capped_count_attack,
        ),
        (
            "attacker_capped_in_window",
            attacker_capped_in_window_base,
            attacker_capped_in_window_attack,
        ),
        (
            "attacker_min_effective_gas_in_window",
            attacker_min_effective_gas_base,
            attacker_min_effective_gas_attack,
        ),
        (
            "attacker_first_cap_tick",
            attacker_first_cap_tick_base,
            attacker_first_cap_tick_attack,
        ),
        (
            "failed_reason_slippage_window",
            _as_decimal(failed_reason_base.get("slippage", 0)),
            _as_decimal(failed_reason_attack.get("slippage", 0)),
        ),
        (
            "failed_reason_congestion_window",
            _as_decimal(failed_reason_base.get("congestion", 0)),
            _as_decimal(failed_reason_attack.get("congestion", 0)),
        ),
        (
            "retail_failed_reason_slippage_window",
            _as_decimal(retail_failed_base.get("slippage", 0)),
            _as_decimal(retail_failed_attack.get("slippage", 0)),
        ),
        (
            "retail_failed_reason_congestion_window",
            _as_decimal(retail_failed_base.get("congestion", 0)),
            _as_decimal(retail_failed_attack.get("congestion", 0)),
        ),
    ]

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "baseline", "attack", "attack_minus_baseline"])
        for metric, b_value, a_value in rows:
            writer.writerow([metric, str(b_value), str(a_value), str(a_value - b_value)])

    payload = {
        "window": {
            "start_tick": start_tick,
            "end_tick": end_tick,
        },
        "baseline_dir": str(baseline_dir),
        "attack_dir": str(attack_dir),
        "metrics": {
            metric: {
                "baseline": str(b_value),
                "attack": str(a_value),
                "attack_minus_baseline": str(a_value - b_value),
            }
            for metric, b_value, a_value in rows
        },
        "attack_meta": {
            "attacker_id": attack_eclipse.get("attacker_id"),
            "triggered": bool(attack_eclipse.get("triggered", False)),
        },
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "[RUN] social eclipse comparison generated | csv=%s | json=%s",
        str(csv_path),
        str(json_path),
    )
    return {"csv": csv_path, "json": json_path}


def write_run_window_metrics_csv(
    *,
    summary: dict[str, Any],
    output_dir: Path,
) -> Path:
    social = summary.get("social_eclipse", {}) or {}
    row = {
        "scenario": summary.get("scenario"),
        "seed": summary.get("seed"),
        "traffic_profile": social.get("traffic_profile", summary.get("traffic_profile", "stress")),
        "window_start_tick": social.get("window_start_tick"),
        "window_end_tick": social.get("window_end_tick"),
        "attacker_id": social.get("attacker_id"),
        "attacker_tx_success_rate_window": social.get("attacker_tx_success_rate_window"),
        "attacker_tx_success_rate_executable_window": social.get(
            "attacker_tx_success_rate_executable_window"
        ),
        "retail_tx_success_rate_window": social.get("retail_tx_success_rate_window"),
        "retail_tx_success_rate_executable_window": social.get(
            "retail_tx_success_rate_executable_window"
        ),
        "attacker_vs_retail_success_rate_gap_window": social.get(
            "attacker_vs_retail_success_rate_gap_window"
        ),
        "attacker_vs_retail_success_rate_gap_executable_window": social.get(
            "attacker_vs_retail_success_rate_gap_executable_window"
        ),
        "attacker_attempted_count_window": social.get("attacker_attempted_count_window"),
        "attacker_settled_count_window": social.get("attacker_settled_count_window"),
        "attacker_failed_congestion_like_window": social.get(
            "attacker_failed_congestion_like_window"
        ),
        "attacker_raw_denominator_window": social.get("attacker_raw_denominator_window"),
        "attacker_executable_denominator_window": social.get(
            "attacker_executable_denominator_window"
        ),
        "retail_attempted_count_window": social.get("retail_attempted_count_window"),
        "retail_settled_count_window": social.get("retail_settled_count_window"),
        "retail_failed_congestion_like_window": social.get("retail_failed_congestion_like_window"),
        "retail_raw_denominator_window": social.get("retail_raw_denominator_window"),
        "retail_executable_denominator_window": social.get(
            "retail_executable_denominator_window"
        ),
        "avg_gas_paid_attacker_window": social.get("avg_gas_paid_attacker_window"),
        "avg_gas_paid_retail_window": social.get("avg_gas_paid_retail_window"),
        "max_gas_bid_in_window": social.get("max_gas_bid_in_window"),
        "attacker_capped_in_window": social.get("attacker_capped_in_window"),
        "attacker_capped_tx_count_window": social.get("attacker_capped_tx_count_window"),
        "attacker_min_effective_gas_in_window": social.get(
            "attacker_min_effective_gas_in_window"
        ),
        "attacker_first_cap_tick": social.get("attacker_first_cap_tick"),
    }
    path = output_dir / "run_window_metrics.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)
    return path


def plot_metrics(metrics_csv: Path, output_png: Path) -> None:
    ticks: list[int] = []
    peg: list[float] = []
    gini: list[float] = []
    panic: list[float] = []
    concentration: list[float] = []
    congestion: list[float] = []
    processed: list[float] = []

    with metrics_csv.open("r", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        idx = {name: i for i, name in enumerate(header)}
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != len(header):
                continue
            ticks.append(int(parts[idx["tick"]]))
            peg.append(float(parts[idx["peg_deviation"]]))
            gini.append(float(parts[idx["gini"]]))
            panic.append(float(parts[idx["panic_word_freq"]]))
            concentration.append(float(parts[idx["governance_concentration"]]))
            congestion.append(float(parts[idx["mempool_congestion"]]))
            processed.append(float(parts[idx["mempool_processed"]]))

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    ax1, ax2 = axes[0]
    ax3, ax4 = axes[1]

    ax1.plot(ticks, peg, label="peg_deviation", linewidth=1.8)
    ax1.plot(ticks, panic, label="panic_word_freq", linewidth=1.5)
    ax1.set_title("Peg Stress and Panic Signal")
    ax1.grid(alpha=0.25)
    ax1.legend()

    ax2.plot(ticks, gini, label="gini", linewidth=1.8)
    ax2.plot(ticks, concentration, label="governance_concentration", linewidth=1.5)
    ax2.set_title("Wealth Inequality and Governance Concentration")
    ax2.grid(alpha=0.25)
    ax2.legend()

    ax3.plot(ticks, congestion, label="mempool_congestion", linewidth=1.8)
    ax3.plot(ticks, processed, label="mempool_processed", linewidth=1.5)
    ax3.set_title("Network Congestion")
    ax3.grid(alpha=0.25)
    ax3.legend()

    ax4.plot(ticks, peg, linewidth=1.2)
    ax4.set_title("Peg Deviation (zoom)")
    ax4.grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(output_png, dpi=170)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase5 governance and dashboard visualizer")
    parser.add_argument("--ticks", type=int, default=80)
    parser.add_argument(
        "--retail",
        type=int,
        default=21,
        help="Retail agent count for cohort design (recommended: 21-27).",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        choices=SCENARIO_CHOICES,
        default=SCENARIO_DEFAULT,
        help="Simulation scenario preset.",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--seed-list",
        type=str,
        default=None,
        help="Comma-separated seeds for batch run (e.g. 42,77,101). Use 'auto' for built-in pretty seed set.",
    )
    parser.add_argument(
        "--best-looking-preset",
        action="store_true",
        help="Apply a tuned preset for staircase-style, publication-friendly curve shape.",
    )
    parser.add_argument("--output-dir", type=str, default="artifacts/phase5")
    parser.add_argument(
        "--traffic-profile",
        type=str,
        choices=TRAFFIC_PROFILE_CHOICES,
        default="stress",
        help="Economic noise profile: stress=legacy mixed noise, eval=executable-priority retail flow.",
    )
    parser.add_argument(
        "--prompt-profile-path",
        type=str,
        default=None,
        help="Optional JSON path for per-agent prompt/profile overrides.",
    )
    parser.add_argument(
        "--offline-rules",
        action="store_true",
        help="Use non-API local rule mode. Default is API mode.",
    )
    parser.add_argument(
        "--llm-agent-count",
        type=int,
        default=12,
        help="How many agents are controlled by LLM/runtime.",
    )
    parser.add_argument(
        "--preflight-timeout",
        type=float,
        default=12.0,
        help="API preflight timeout in seconds per role.",
    )
    parser.add_argument(
        "--max-inbox-size",
        type=int,
        default=5,
        help="Runtime inbox window size per agent per tick.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable per-tick progress heartbeat logs.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=1,
        help="Heartbeat interval in ticks.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="logs/simulation_run.log",
        help="Path to simulation log file.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level: DEBUG/INFO/WARNING/ERROR.",
    )
    parser.add_argument(
        "--llm-max-concurrent",
        type=int,
        default=None,
        help="Override global LLM API max concurrent requests (default uses config).",
    )
    parser.add_argument(
        "--ticks-per-day",
        type=int,
        default=100,
        help="Simulation ticks per day.",
    )
    parser.add_argument(
        "--max-tx-per-tick",
        type=int,
        default=50,
        help="Execution throughput cap per tick.",
    )
    parser.add_argument(
        "--voting-window-ticks",
        type=int,
        default=20,
        help="Governance voting window length in ticks.",
    )
    parser.add_argument(
        "--disable-black-swan",
        action="store_true",
        help="Disable scenario black-swan shock actions.",
    )
    parser.add_argument(
        "--shock-t1",
        type=str,
        default="1000000",
        help="Whale shock amount on Tick 1 (UST).",
    )
    parser.add_argument(
        "--shock-t3",
        type=str,
        default="500000",
        help="Whale shock amount on Tick 3 (UST).",
    )
    parser.add_argument(
        "--shock-t6",
        type=str,
        default="300000",
        help="Whale shock amount on Tick 6 (UST).",
    )
    parser.add_argument(
        "--pool-a-init",
        type=str,
        default="10000000,10000000",
        help="Initial Pool_A reserves as 'UST,USDC'.",
    )
    parser.add_argument(
        "--retail-ust-cap",
        type=str,
        default="5000000",
        help="Retail total UST cap for staircase scenario.",
    )
    parser.add_argument(
        "--enable-mitigation-a",
        action="store_true",
        help="Enable Semantic-Aware Governance Gateway (SAGG) mitigation.",
    )
    parser.add_argument(
        "--mitigation-mode",
        type=str,
        choices=("none", "semantic", "priority", "full"),
        default="none",
        help="Governance mitigation mode. If not none, it overrides --enable-mitigation-a.",
    )
    parser.add_argument(
        "--enable-mitigation-b",
        action="store_true",
        help="Enable Adaptive Execution Circuit-Breaker (AECB) mitigation.",
    )
    parser.add_argument(
        "--mitigation-b-panic-threshold",
        type=str,
        default="0.5",
        help="AECB trigger threshold on panic signal.",
    )
    parser.add_argument(
        "--mitigation-b-gas-cap",
        type=str,
        default="50.0",
        help="AECB crisis gas cap.",
    )
    parser.add_argument(
        "--mitigation-b-gas-weight",
        type=str,
        default="0.2",
        help="AECB weighted sorter gas weight.",
    )
    parser.add_argument(
        "--mitigation-b-age-weight",
        type=str,
        default="0.8",
        help="AECB weighted sorter account-age weight.",
    )
    parser.add_argument(
        "--mitigation-b-role-bias-retail",
        type=str,
        default="1.0",
        help="AECB role bias for retail.",
    )
    parser.add_argument(
        "--mitigation-b-role-bias-project",
        type=str,
        default="0.6",
        help="AECB role bias for project.",
    )
    parser.add_argument(
        "--mitigation-b-role-bias-whale",
        type=str,
        default="0.2",
        help="AECB role bias for whale.",
    )
    parser.add_argument(
        "--mitigation-b-age-norm-ticks",
        type=int,
        default=100,
        help="AECB age normalization horizon in ticks.",
    )
    parser.add_argument(
        "--mitigation-b-warm-start",
        action="store_true",
        help="Enable warm-start for mitigation-B so cap can apply from early ticks.",
    )
    parser.add_argument(
        "--social-eclipse-attack",
        action="store_true",
        help="Enable social-driven mempool eclipse attack (FUD + sell pressure).",
    )
    parser.add_argument(
        "--eclipse-attacker-id",
        type=str,
        default="whale_1",
        help="Attacker agent id for social eclipse attack.",
    )
    parser.add_argument(
        "--eclipse-trigger-tick",
        type=int,
        default=1,
        help="1-based tick to trigger social eclipse attack.",
    )
    parser.add_argument(
        "--eclipse-window-ticks",
        type=int,
        default=5,
        help="Post-trigger window size for asymmetry metrics.",
    )
    parser.add_argument(
        "--eclipse-fud-message",
        type=str,
        default=DEFAULT_ECLIPSE_FUD_MESSAGE,
        help="FUD message injected to FORUM at eclipse trigger tick.",
    )
    parser.add_argument(
        "--eclipse-sell-ust",
        type=str,
        default="300000",
        help="UST amount sold by attacker on eclipse trigger tick.",
    )
    parser.add_argument(
        "--comparison-baseline-dir",
        type=str,
        default=None,
        help="Optional baseline run directory (with summary.json/metrics.csv) for auto comparison.",
    )
    parser.add_argument(
        "--comparison-output-dir",
        type=str,
        default="paper/data",
        help="Output directory for social eclipse comparison artifacts.",
    )
    parser.add_argument(
        "--governance-dos-attack",
        action="store_true",
        help="Enable Governance-DoS script: whale_1 submits 3 placeholder proposals to occupy slots.",
    )
    parser.add_argument(
        "--dos-whale-luna",
        type=str,
        default="4000",
        help="Initial LUNA assigned to whale_1 when governance DoS mode is enabled.",
    )
    parser.add_argument(
        "--dos-sell-ust",
        type=str,
        default="300000",
        help="Whale_1 UST sell amount during Governance-DoS trigger tick.",
    )
    parser.add_argument(
        "--governance-hijack-attack",
        action="store_true",
        help="Enable governance hijack scenario: attacker proposes malicious parameter patch.",
    )
    parser.add_argument(
        "--hijack-attacker-id",
        type=str,
        default="whale_1",
        help="Attacker id for governance hijack scenario.",
    )
    parser.add_argument(
        "--hijack-trigger-tick",
        type=int,
        default=2,
        help="1-based tick for malicious proposal submission.",
    )
    parser.add_argument(
        "--hijack-proposal-text",
        type=str,
        default="Lower swap fee to 0.0001 to maximize holder returns",
        help="Malicious proposal text used in governance hijack scenario.",
    )
    parser.add_argument(
        "--hijack-force-approve",
        action="store_true",
        help="Force all agents to cast approve votes for malicious proposal.",
    )
    parser.add_argument(
        "--hijack-attacker-luna",
        type=str,
        default="4000",
        help="Initial LUNA assigned to hijack attacker for proposal fee + voting control.",
    )
    parser.add_argument(
        "--no-paper-charts",
        action="store_true",
        help="Disable automatic paper-grade chart generation after simulation.",
    )
    parser.add_argument(
        "--paper-chart-formats",
        type=str,
        default="png,pdf",
        help="Auto paper chart formats, comma-separated (default: png,pdf).",
    )
    parser.add_argument(
        "--paper-chart-dpi",
        type=int,
        default=300,
        help="Auto paper chart PNG DPI (default: 300).",
    )
    parser.add_argument(
        "--paper-chart-style",
        type=str,
        default="whitegrid",
        help="Auto paper chart seaborn style.",
    )
    parser.add_argument(
        "--paper-chart-font-size",
        type=int,
        default=14,
        help="Auto paper chart global font size.",
    )
    parser.add_argument(
        "--paper-chart-congestion-scale",
        type=str,
        choices=("linear", "log"),
        default="log",
        help="Auto paper chart congestion scale.",
    )
    parser.add_argument(
        "--paper-chart-strict-shape-check",
        action="store_true",
        help="Enable strict L-shape data check in auto paper chart generation.",
    )
    parser.add_argument(
        "--paper-chart-shape-report-json",
        type=str,
        default=None,
        help="Optional path for auto paper chart shape report json.",
    )
    parser.add_argument(
        "--paper-chart-output-dir",
        type=str,
        default=None,
        help="Optional output directory for auto paper charts; default is run output dir.",
    )
    parser.add_argument(
        "--paper-chart-fail-hard",
        action="store_true",
        help="Fail whole run if auto paper chart generation fails.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    preset_overrides = _apply_best_looking_preset(args)
    seed_list = _parse_seed_list(args.seed_list)
    if seed_list:
        if any(seed < 0 for seed in seed_list):
            raise ValueError("seed-list must only contain non-negative integers")
        exit_code = _run_multi_seed_sweep(args, seed_list, sys.argv[1:])
        raise SystemExit(exit_code)

    if int(args.ticks) <= 0:
        raise ValueError("ticks must be > 0")
    if int(args.progress_interval) <= 0:
        raise ValueError("progress_interval must be > 0")
    if args.llm_max_concurrent is not None and int(args.llm_max_concurrent) <= 0:
        raise ValueError("llm_max_concurrent must be > 0")
    if int(args.ticks_per_day) <= 0:
        raise ValueError("ticks_per_day must be > 0")
    if int(args.max_tx_per_tick) <= 0:
        raise ValueError("max-tx-per-tick must be > 0")
    if int(args.voting_window_ticks) <= 0:
        raise ValueError("voting_window_ticks must be > 0")
    if int(args.paper_chart_dpi) <= 0:
        raise ValueError("paper-chart-dpi must be > 0")
    if int(args.paper_chart_font_size) <= 0:
        raise ValueError("paper-chart-font-size must be > 0")
    if int(args.eclipse_trigger_tick) <= 0:
        raise ValueError("eclipse-trigger-tick must be > 0")
    if int(args.eclipse_window_ticks) <= 0:
        raise ValueError("eclipse-window-ticks must be > 0")
    if int(args.hijack_trigger_tick) <= 0:
        raise ValueError("hijack-trigger-tick must be > 0")
    if int(args.mitigation_b_age_norm_ticks) <= 0:
        raise ValueError("mitigation-b-age-norm-ticks must be > 0")

    shock_t1 = _parse_positive_decimal(args.shock_t1, field_name="shock_t1")
    shock_t3 = _parse_positive_decimal(args.shock_t3, field_name="shock_t3")
    shock_t6 = _parse_positive_decimal(args.shock_t6, field_name="shock_t6")
    retail_ust_cap = _parse_positive_decimal(args.retail_ust_cap, field_name="retail_ust_cap")
    dos_whale_luna = _parse_positive_decimal(args.dos_whale_luna, field_name="dos_whale_luna")
    dos_sell_ust = _parse_positive_decimal(args.dos_sell_ust, field_name="dos_sell_ust")
    hijack_attacker_luna = _parse_positive_decimal(
        args.hijack_attacker_luna, field_name="hijack_attacker_luna"
    )
    eclipse_sell_ust = _parse_positive_decimal(args.eclipse_sell_ust, field_name="eclipse_sell_ust")
    mitigation_b_panic_threshold = Decimal(str(args.mitigation_b_panic_threshold))
    mitigation_b_gas_cap = _parse_positive_decimal(
        args.mitigation_b_gas_cap, field_name="mitigation_b_gas_cap"
    )
    mitigation_b_gas_weight = Decimal(str(args.mitigation_b_gas_weight))
    mitigation_b_age_weight = Decimal(str(args.mitigation_b_age_weight))
    if mitigation_b_gas_weight < 0 or mitigation_b_age_weight < 0:
        raise ValueError("mitigation-b-gas-weight and mitigation-b-age-weight must be >= 0")
    if (mitigation_b_gas_weight + mitigation_b_age_weight) <= 0:
        raise ValueError("mitigation-b-gas-weight + mitigation-b-age-weight must be > 0")
    mitigation_b_role_bias = {
        "retail": Decimal(str(args.mitigation_b_role_bias_retail)),
        "project": Decimal(str(args.mitigation_b_role_bias_project)),
        "whale": Decimal(str(args.mitigation_b_role_bias_whale)),
    }
    pool_a_reserves = _parse_pool_reserves(args.pool_a_init)
    prompt_profile_path = (
        Path(args.prompt_profile_path).resolve() if args.prompt_profile_path else None
    )
    if args.scenario == SCENARIO_DEFAULT:
        pool_b_reserves = pool_a_reserves
    else:
        pool_b_reserves = (Decimal("1000000"), Decimal("1000000"))

    black_swan_schedule = _build_black_swan_schedule(
        scenario=args.scenario,
        enabled=not args.disable_black_swan,
        shock_t1=shock_t1,
        shock_t3=shock_t3,
        shock_t6=shock_t6,
    )

    log_path = Path(args.log_file)
    if not log_path.is_absolute():
        log_path = (ROOT / log_path).resolve()

    logger = configure_logging(log_file=log_path, log_level=args.log_level)

    logger.info("[BOOT] system modules loading")
    logger.info("[CONFIG] loading LLM config file")

    random.seed(args.seed)
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    db_path = out_dir / "phase5_trace.sqlite3"
    if db_path.exists():
        db_path.unlink()

    metrics_csv = out_dir / "metrics.csv"
    if metrics_csv.exists():
        metrics_csv.unlink()

    checkpoint_dir = out_dir / "checkpoints"
    if checkpoint_dir.exists():
        for p in checkpoint_dir.glob("*.json"):
            p.unlink()

    logger.info("[CONFIG] output_dir=%s", str(out_dir))
    if preset_overrides:
        logger.info(
            "[CONFIG] best_looking_preset=on | overrides=%s",
            json.dumps(preset_overrides, ensure_ascii=False),
        )
    else:
        logger.info(
            "[CONFIG] best_looking_preset=%s",
            "on" if args.best_looking_preset else "off",
        )
    logger.info("[CONFIG] log_file=%s", str(log_path))
    logger.info("[CONFIG] scenario=%s", args.scenario)
    logger.info("[CONFIG] traffic_profile=%s", str(args.traffic_profile))
    logger.info(
        "[CONFIG] llm_max_concurrent=%s",
        str(args.llm_max_concurrent) if args.llm_max_concurrent is not None else "config-default",
    )
    logger.info("[CONFIG] ticks_per_day=%d", int(args.ticks_per_day))
    logger.info("[CONFIG] max_tx_per_tick=%d", int(args.max_tx_per_tick))
    logger.info("[CONFIG] voting_window_ticks=%d", int(args.voting_window_ticks))
    logger.info(
        "[CONFIG] governance_dos_attack=%s",
        "on" if args.governance_dos_attack else "off",
    )
    logger.info(
        "[CONFIG] governance_hijack_attack=%s | attacker=%s | trigger_tick=%d | force_approve=%s | attacker_luna=%s",
        "on" if args.governance_hijack_attack else "off",
        str(args.hijack_attacker_id),
        int(args.hijack_trigger_tick),
        "on" if args.hijack_force_approve else "off",
        str(hijack_attacker_luna),
    )
    resolved_mitigation_mode = (
        str(args.mitigation_mode).strip().lower()
        if str(args.mitigation_mode).strip().lower() != "none"
        else ("semantic" if args.enable_mitigation_a else "none")
    )
    logger.info(
        "[CONFIG] mitigation_mode=%s",
        resolved_mitigation_mode,
    )
    logger.info(
        "[CONFIG] mitigation_b=%s | panic_threshold=%s | gas_cap=%s | gas_weight=%s | age_weight=%s | age_norm_ticks=%d | warm_start=%s",
        "on" if args.enable_mitigation_b else "off",
        str(mitigation_b_panic_threshold),
        str(mitigation_b_gas_cap),
        str(mitigation_b_gas_weight),
        str(mitigation_b_age_weight),
        int(args.mitigation_b_age_norm_ticks),
        "on" if args.mitigation_b_warm_start else "off",
    )
    logger.info(
        "[CONFIG] mitigation_b_role_bias retail=%s project=%s whale=%s",
        str(mitigation_b_role_bias["retail"]),
        str(mitigation_b_role_bias["project"]),
        str(mitigation_b_role_bias["whale"]),
    )
    logger.info(
        "[CONFIG] social_eclipse_attack=%s",
        "on" if args.social_eclipse_attack else "off",
    )
    logger.info(
        "[CONFIG] prompt_profile_path=%s",
        str(prompt_profile_path) if prompt_profile_path else "none",
    )
    logger.info(
        "[CONFIG] pool_a_init=(%s,%s) | pool_b_init=(%s,%s)",
        str(pool_a_reserves[0]),
        str(pool_a_reserves[1]),
        str(pool_b_reserves[0]),
        str(pool_b_reserves[1]),
    )
    logger.info(
        "[CONFIG] shock schedule amounts | t1=%s | t3=%s | t6=%s",
        str(shock_t1),
        str(shock_t3),
        str(shock_t6),
    )

    engine = ACE_Engine(
        db_path=db_path,
        pool_a_reserves=pool_a_reserves,
        pool_b_reserves=pool_b_reserves,
    )
    metrics = LoggerMetrics(metrics_csv)
    checkpoints = StateCheckpoint(checkpoint_dir)
    governance_max_open_per_agent = 3 if args.governance_dos_attack else 1
    mitigation_strategy = (
        GovernanceMitigationModule.from_mode(
            base_db_path=db_path,
            mode=resolved_mitigation_mode,
            enable_llm_scoring=not bool(args.offline_rules),
            llm_timeout=4.0,
        )
        if resolved_mitigation_mode != "none"
        else None
    )
    governance = GovernanceModule(
        db_path=db_path,
        voting_window_ticks=int(args.voting_window_ticks),
        max_open_per_agent=governance_max_open_per_agent,
        mitigation_strategy=mitigation_strategy,
    )
    mitigation_b_warm_start_ticks = (
        int(args.eclipse_trigger_tick) + int(args.eclipse_window_ticks) - 1
        if args.mitigation_b_warm_start
        else 0
    )
    execution_mitigation = (
        ExecutionCircuitBreaker(
            panic_threshold=mitigation_b_panic_threshold,
            crisis_gas_cap=mitigation_b_gas_cap,
            gas_weight=mitigation_b_gas_weight,
            age_weight=mitigation_b_age_weight,
            age_norm_ticks=int(args.mitigation_b_age_norm_ticks),
            warm_start_ticks=int(mitigation_b_warm_start_ticks),
            role_bias=mitigation_b_role_bias,
            logger=logger,
        )
        if args.enable_mitigation_b
        else None
    )

    orchestrator = Simulation_Orchestrator(
        engine=engine,
        ticks_per_day=int(args.ticks_per_day),
        governance=governance,
        max_tx_per_tick=int(args.max_tx_per_tick),
        metrics_logger=metrics,
        state_checkpoint=checkpoints,
        execution_mitigation=execution_mitigation,
    )

    bootstrap = build_bootstrap_cohort(count_retail=args.retail, logger=logger)
    retail_cap_info = {
        "cap": str(retail_ust_cap),
        "total_before": None,
        "total_after": None,
        "scaled": False,
        "scale_ratio": None,
    }
    if args.scenario == SCENARIO_DEFAULT:
        retail_cap_info = _apply_retail_ust_cap(
            bootstrap=bootstrap,
            cap=retail_ust_cap,
            logger=logger,
        )
    prompt_profile_report = apply_prompt_profile_overrides(
        bootstrap=bootstrap,
        prompt_profile_path=prompt_profile_path,
        logger=logger,
    )
    eclipse_prompt_bias_applied = False
    if args.social_eclipse_attack:
        eclipse_prompt_bias_applied = apply_social_eclipse_prompt_bias(
            bootstrap=bootstrap,
            attacker_id=str(args.eclipse_attacker_id),
            logger=logger,
        )
    if args.governance_dos_attack:
        for item in bootstrap:
            if item.agent_id == "whale_1":
                if item.initial_luna < dos_whale_luna:
                    item.initial_luna = dos_whale_luna
                break
        logger.info(
            "[CONFIG] governance_dos_attack enabled | whale_1_initial_luna=%s | dos_sell_ust=%s | max_open_per_agent=%d",
            str(dos_whale_luna),
            str(dos_sell_ust),
            int(governance_max_open_per_agent),
        )
    if args.governance_hijack_attack:
        for item in bootstrap:
            if item.agent_id == str(args.hijack_attacker_id):
                if item.initial_luna < hijack_attacker_luna:
                    item.initial_luna = hijack_attacker_luna
                break
        logger.info(
            "[CONFIG] governance_hijack attacker balance adjusted | attacker=%s | initial_luna=%s",
            str(args.hijack_attacker_id),
            str(hijack_attacker_luna),
        )
    bootstrap_by_id = {item.agent_id: item for item in bootstrap}
    role_map = {item.agent_id: item.role for item in bootstrap}
    agents = seed_accounts(engine, bootstrap=bootstrap)
    setup_topology(orchestrator, bootstrap=bootstrap)

    runtime_agent_ids = select_runtime_agents(
        agents,
        llm_agent_count=args.llm_agent_count,
        role_map=role_map,
    )

    if not args.offline_rules:
        logger.info("[CHECK] LLM connectivity preflight...")
        required_roles = {role_of(agent_id, role_map) for agent_id in runtime_agent_ids}
        preflight_api_or_raise(
            required_roles=required_roles,
            timeout=args.preflight_timeout,
            logger=logger,
        )
        logger.info("[CHECK] preflight passed")
    else:
        logger.info("[CHECK] offline rule mode enabled, skip API preflight")

    runtime = build_runtime(
        orchestrator,
        runtime_agent_ids=runtime_agent_ids,
        bootstrap_by_id=bootstrap_by_id,
        offline_rules=args.offline_rules,
        llm_max_concurrent=args.llm_max_concurrent,
    )

    logger.info("[RUN] simulation started")

    try:
        sim_stats = simulate(
            orchestrator,
            runtime,
            agents=agents,
            role_map=role_map,
            ticks=args.ticks,
            max_inbox_size=args.max_inbox_size,
            logger=logger,
            progress_enabled=not args.no_progress,
            progress_interval=int(args.progress_interval),
            offline_rules=args.offline_rules,
            black_swan_schedule=black_swan_schedule,
            governance_dos_attack=bool(args.governance_dos_attack),
            dos_attacker_id="whale_1",
            dos_sell_ust=dos_sell_ust,
            social_eclipse_attack=bool(args.social_eclipse_attack),
            eclipse_attacker_id=str(args.eclipse_attacker_id),
            eclipse_trigger_tick=int(args.eclipse_trigger_tick),
            eclipse_window_ticks=int(args.eclipse_window_ticks),
            eclipse_fud_message=str(args.eclipse_fud_message),
            eclipse_sell_ust=eclipse_sell_ust,
            traffic_profile=str(args.traffic_profile),
            governance_hijack_attack=bool(args.governance_hijack_attack),
            hijack_attacker_id=str(args.hijack_attacker_id),
            hijack_trigger_tick=int(args.hijack_trigger_tick),
            hijack_proposal_text=str(args.hijack_proposal_text),
            hijack_force_approve=bool(args.hijack_force_approve),
        )

        curve_quality = _evaluate_curve_quality(
            sim_stats["ust_price_by_tick"],
            total_ticks=int(args.ticks),
        )

        summary = {
            "ticks": args.ticks,
            "agents": len(agents),
            "runtime_agents": len(runtime_agent_ids),
            "scenario": args.scenario,
            "seed": int(args.seed),
            "traffic_profile": str(args.traffic_profile),
            "max_tx_per_tick": int(args.max_tx_per_tick),
            "llm_mode": "offline_rules" if args.offline_rules else "api",
            "decision_calls_total": sim_stats["llm_calls_total"],
            "api_calls_total": sim_stats["api_calls_total"],
            "sleeping_total": sim_stats["sleeping_total"],
            "pool_a_init": [str(pool_a_reserves[0]), str(pool_a_reserves[1])],
            "pool_b_init": [str(pool_b_reserves[0]), str(pool_b_reserves[1])],
            "shock_plan": {
                "enabled": not args.disable_black_swan,
                "ticks": sorted(list(black_swan_schedule.keys())),
                "tick_amounts_ust": {
                    "1": str(shock_t1),
                    "3": str(shock_t3),
                    "6": str(shock_t6),
                },
            },
            "retail_ust_cap": retail_cap_info,
            "prompt_profile": prompt_profile_report,
            "social_eclipse_prompt_bias_applied": bool(eclipse_prompt_bias_applied),
            "mitigation_mode": resolved_mitigation_mode,
            "mitigation_b": {
                "enabled": bool(args.enable_mitigation_b),
                "panic_threshold": str(mitigation_b_panic_threshold),
                "gas_cap": str(mitigation_b_gas_cap),
                "gas_weight": str(mitigation_b_gas_weight),
                "age_weight": str(mitigation_b_age_weight),
                "age_norm_ticks": int(args.mitigation_b_age_norm_ticks),
                "warm_start_enabled": bool(args.mitigation_b_warm_start),
                "warm_start_ticks": int(mitigation_b_warm_start_ticks),
                "role_bias": {
                    "retail": str(mitigation_b_role_bias["retail"]),
                    "project": str(mitigation_b_role_bias["project"]),
                    "whale": str(mitigation_b_role_bias["whale"]),
                },
            },
            "curve_quality": curve_quality,
            "governance_dos": sim_stats.get("governance_dos", {}),
            "governance_hijack": sim_stats.get("governance_hijack", {}),
            "social_eclipse": sim_stats.get("social_eclipse", {}),
            "failed_reason_totals": sim_stats.get("failed_reason_totals", {}),
            "db": str(db_path),
            "metrics_csv": str(metrics_csv),
            "checkpoint_count": len(list(checkpoint_dir.glob("tick_*.json"))),
            "governance": orchestrator.governance.get_state(),
            "log_file": str(log_path),
        }
        summary_path = out_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        run_window_metrics_csv = write_run_window_metrics_csv(
            summary=summary,
            output_dir=out_dir,
        )
        summary["run_window_metrics_csv"] = str(run_window_metrics_csv)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        plot_path = out_dir / "phase5_dashboard.png"
        plot_metrics(metrics_csv, plot_path)

        paper_chart_outputs: dict[str, Any] | None = None
        if not args.no_paper_charts:
            paper_chart_output_dir = (
                Path(args.paper_chart_output_dir).resolve()
                if args.paper_chart_output_dir
                else out_dir
            )
            paper_shape_report_path = (
                Path(args.paper_chart_shape_report_json).resolve()
                if args.paper_chart_shape_report_json
                else None
            )
            try:
                from paper_charts_generator import generate_charts, parse_formats

                paper_chart_outputs = generate_charts(
                    metrics_path=metrics_csv,
                    summary_path=summary_path,
                    db_path=db_path,
                    output_dir=paper_chart_output_dir,
                    dpi=int(args.paper_chart_dpi),
                    style=str(args.paper_chart_style),
                    formats=parse_formats(str(args.paper_chart_formats)),
                    font_size=int(args.paper_chart_font_size),
                    congestion_scale=str(args.paper_chart_congestion_scale),
                    strict_shape_check=bool(args.paper_chart_strict_shape_check),
                    shape_report_json=paper_shape_report_path,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[RUN] auto paper chart generation failed: %s", str(exc))
                if args.paper_chart_fail_hard:
                    raise
            else:
                summary["paper_charts"] = {
                    "output_dir": str(paper_chart_output_dir),
                    "files": {
                        key: (
                            {fmt: str(path) for fmt, path in value.items()}
                            if isinstance(value, dict)
                            else str(value)
                        )
                        for key, value in paper_chart_outputs.items()
                        if key != "diagnostics"
                    },
                }
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        eclipse_comparison_outputs: dict[str, Path] | None = None
        if args.comparison_baseline_dir:
            baseline_dir = Path(args.comparison_baseline_dir).resolve()
            comparison_output_dir = Path(args.comparison_output_dir).resolve()
            try:
                eclipse_comparison_outputs = generate_social_eclipse_comparison(
                    baseline_dir=baseline_dir,
                    attack_dir=out_dir,
                    output_dir=comparison_output_dir,
                    logger=logger,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[RUN] social eclipse comparison failed: %s", str(exc))
            else:
                summary["social_eclipse_comparison"] = {
                    "baseline_dir": str(baseline_dir),
                    "output_dir": str(comparison_output_dir),
                    "files": {key: str(value) for key, value in eclipse_comparison_outputs.items()},
                }
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        logger.info("[RUN] simulation finished")
        logger.info("[RUN] mode=%s", summary["llm_mode"])
        logger.info("[RUN] decision_calls_total=%s", summary["decision_calls_total"])
        logger.info("[RUN] api_calls_total=%s", summary["api_calls_total"])
        logger.info("[RUN] db=%s", str(db_path))
        logger.info("[RUN] metrics=%s", str(metrics_csv))
        logger.info("[RUN] run_window_metrics=%s", str(run_window_metrics_csv))
        logger.info("[RUN] checkpoints=%s", str(checkpoint_dir))
        logger.info("[RUN] plot=%s", str(plot_path))
        logger.info("[RUN] summary=%s", str(summary_path))
        if paper_chart_outputs is not None:
            logger.info(
                "[RUN] paper_dashboard=%s",
                str(paper_chart_outputs.get("dashboard", {}).get("png", "-")),
            )
        if eclipse_comparison_outputs is not None:
            logger.info(
                "[RUN] social_eclipse_comparison_csv=%s",
                str(eclipse_comparison_outputs.get("csv", "-")),
            )
    finally:
        orchestrator.close()
        engine.close()


if __name__ == "__main__":
    main()
