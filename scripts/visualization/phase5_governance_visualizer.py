from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
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

from ace_sim.agents.base_agent import ProjectAgent, RetailAgent, WhaleAgent
from ace_sim.cognition.llm_router import OpenAIChatAdapter
from ace_sim.config.llm_config import load_llm_config
from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.orchestrator.time_orchestrator import TickSettlementReport
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator
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


def seed_accounts(engine: ACE_Engine, count_retail: int) -> list[str]:
    agents = ["whale_0", "whale_1", "project_0"]
    engine.create_account("whale_0", ust="200000", luna="200000", usdc="300000")
    engine.create_account("whale_1", ust="120000", luna="80000", usdc="200000")
    engine.create_account("project_0", ust="100000", luna="30000", usdc="100000")

    for idx in range(count_retail):
        name = f"retail_{idx}"
        engine.create_account(name, ust="5000", luna="1500", usdc="5000")
        agents.append(name)
    return agents


def role_of(agent_id: str) -> str:
    if agent_id.startswith("whale"):
        return "whale"
    if agent_id.startswith("project"):
        return "project"
    return "retail"


def community_of(agent_id: str) -> str:
    if agent_id.startswith("whale"):
        return "c1"
    if agent_id.startswith("project"):
        return "c2"
    return "c0"


def setup_topology(orchestrator: Simulation_Orchestrator, agents: list[str]) -> None:
    for name in agents:
        orchestrator.register_agent(
            name,
            role=role_of(name),
            community_id=community_of(name),
        )

    for sender in agents:
        for receiver in agents:
            if sender != receiver and random.random() < 0.2:
                orchestrator.connect_agents(sender, receiver)


def select_runtime_agents(agents: list[str], llm_agent_count: int) -> list[str]:
    llm_agent_count = max(3, int(llm_agent_count))

    core = ["whale_0", "whale_1", "project_0"]
    retails = [name for name in agents if name.startswith("retail")]

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
    offline_rules: bool,
) -> AgentRuntime:
    agents = []
    for agent_id in runtime_agent_ids:
        role = role_of(agent_id)
        community_id = community_of(agent_id)

        if role == "whale":
            agent = WhaleAgent(
                agent_id=agent_id,
                community_id=community_id,
                llm_callable=offline_llm if offline_rules else None,
            )
        elif role == "project":
            agent = ProjectAgent(
                agent_id=agent_id,
                community_id=community_id,
                llm_callable=offline_llm if offline_rules else None,
            )
        else:
            agent = RetailAgent(
                agent_id=agent_id,
                community_id=community_id,
                llm_callable=offline_llm if offline_rules else None,
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


def _count_overload_people(conn: sqlite3.Connection, tick: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM inbox_overload_log
        WHERE tick = ? AND dropped_count > 0
        """,
        (int(tick),),
    ).fetchone()
    return int(row[0]) if row else 0


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
    ticks: int,
    max_inbox_size: int,
    logger: logging.Logger,
    progress_enabled: bool,
    progress_interval: int,
    offline_rules: bool,
) -> dict[str, Any]:
    retail_agents = [name for name in agents if name.startswith("retail")]
    if not retail_agents:
        raise ValueError("at least one retail agent is required")

    proposal_id: str | None = None
    llm_calls_total = 0
    sleeping_total = 0

    conn = sqlite3.connect(orchestrator.engine.get_db_path())
    try:
        for tick in range(ticks):
            # Governance stream
            if tick == 1:
                proposal_id = orchestrator.submit_event(
                    "whale_0",
                    "PROPOSE",
                    {
                        "proposal_text": "Disable minting and set swap fee to 0.01",
                    },
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

            # Economic stream (intentionally overloaded > max_tx_per_tick)
            for _ in range(orchestrator.max_tx_per_tick + 25):
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
            overload_people = _count_overload_people(conn, report.tick)
            logger.info(
                "[SOCIAL] 产生流言: %d | 触发认知过载: %d 人次",
                int(rumor_count),
                int(overload_people),
            )

            _log_retail_swap_summary(logger, orchestrator, report)
            _log_whale_actions(logger, orchestrator, report)
            _log_governance_exec(logger, report)
    finally:
        conn.close()

    return {
        "llm_calls_total": llm_calls_total,
        "api_calls_total": 0 if offline_rules else llm_calls_total,
        "sleeping_total": sleeping_total,
        "ticks": ticks,
    }


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
    parser.add_argument("--retail", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=str, default="artifacts/phase5")
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if int(args.progress_interval) <= 0:
        raise ValueError("progress_interval must be > 0")

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
    logger.info("[CONFIG] log_file=%s", str(log_path))

    engine = ACE_Engine(db_path=db_path)
    metrics = LoggerMetrics(metrics_csv)
    checkpoints = StateCheckpoint(checkpoint_dir)

    orchestrator = Simulation_Orchestrator(
        engine=engine,
        max_tx_per_tick=50,
        metrics_logger=metrics,
        state_checkpoint=checkpoints,
    )

    agents = seed_accounts(engine, count_retail=args.retail)
    setup_topology(orchestrator, agents)

    runtime_agent_ids = select_runtime_agents(agents, llm_agent_count=args.llm_agent_count)

    if not args.offline_rules:
        logger.info("[CHECK] LLM connectivity preflight...")
        required_roles = {role_of(agent_id) for agent_id in runtime_agent_ids}
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
        offline_rules=args.offline_rules,
    )

    logger.info("[RUN] simulation started")

    try:
        sim_stats = simulate(
            orchestrator,
            runtime,
            agents=agents,
            ticks=args.ticks,
            max_inbox_size=args.max_inbox_size,
            logger=logger,
            progress_enabled=not args.no_progress,
            progress_interval=int(args.progress_interval),
            offline_rules=args.offline_rules,
        )

        summary = {
            "ticks": args.ticks,
            "agents": len(agents),
            "runtime_agents": len(runtime_agent_ids),
            "llm_mode": "offline_rules" if args.offline_rules else "api",
            "decision_calls_total": sim_stats["llm_calls_total"],
            "api_calls_total": sim_stats["api_calls_total"],
            "sleeping_total": sim_stats["sleeping_total"],
            "db": str(db_path),
            "metrics_csv": str(metrics_csv),
            "checkpoint_count": len(list(checkpoint_dir.glob("tick_*.json"))),
            "governance": orchestrator.governance.get_state(),
            "log_file": str(log_path),
        }
        summary_path = out_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        plot_path = out_dir / "phase5_dashboard.png"
        plot_metrics(metrics_csv, plot_path)

        logger.info("[RUN] simulation finished")
        logger.info("[RUN] mode=%s", summary["llm_mode"])
        logger.info("[RUN] decision_calls_total=%s", summary["decision_calls_total"])
        logger.info("[RUN] api_calls_total=%s", summary["api_calls_total"])
        logger.info("[RUN] db=%s", str(db_path))
        logger.info("[RUN] metrics=%s", str(metrics_csv))
        logger.info("[RUN] checkpoints=%s", str(checkpoint_dir))
        logger.info("[RUN] plot=%s", str(plot_path))
        logger.info("[RUN] summary=%s", str(summary_path))
    finally:
        orchestrator.close()
        engine.close()


if __name__ == "__main__":
    main()
