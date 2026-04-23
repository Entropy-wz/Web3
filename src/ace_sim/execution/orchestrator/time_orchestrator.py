from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..action_registry.actions import (
    ActionValidationError,
    action_principal_token,
    is_economic_action,
    is_semantic_action,
    normalize_action_type,
    validate_action_schema,
)
from ...engine.ace_engine import (
    ACE_Engine,
    InsufficientFundsError,
    InvariantViolationError,
    SlippageExceededError,
)
from ...social.channel_manager import ChannelManager, QueuedDelivery
from ...social.network_graph import SocialNetworkGraph
from ...social.perception_filter import PerceptionFilter
from ...governance.governance import GovernanceModule
from ...governance.logger_metrics import LoggerMetrics
from ...governance.state_checkpoint import StateCheckpoint
from ..guardrails.secretary_auditor import InsufficientBalanceError, SecretaryAuditor
from ..mitigation import BaseExecutionMitigation, semantic_panic_ratio_from_deliveries


@dataclass
class Transaction:
    tx_id: str
    agent_id: str
    action_type: str
    params: dict[str, Any]
    gas_price: Decimal
    enqueue_seq: int
    submit_tick: int
    raw_gas_price: Decimal | None = None
    effective_gas_price: Decimal | None = None
    mitigation_flags: list[str] = field(default_factory=list)
    resolved_min_amount_out: Decimal | None = None
    estimated_amount_out: Decimal | None = None


@dataclass
class SemanticEvent:
    event_id: str
    agent_id: str
    action_type: str
    params: dict[str, Any]
    emit_tick: int
    parent_event_id: str | None = None


@dataclass
class TxReceipt:
    tx_id: str
    tick: int
    rank: int
    status: str
    action_type: str
    agent_id: str
    gas_bid: Decimal = Decimal("0")
    gas_effective: Decimal = Decimal("0")
    gas_token: str | None = None
    gas_paid: Decimal = Decimal("0")
    error_code: str | None = None
    error_message: str | None = None
    result: dict[str, Any] | None = None


@dataclass
class TickSettlementReport:
    tick: int
    receipts: list[TxReceipt]
    end_snapshot: dict[str, Any]
    fee_vault_snapshot: dict[str, Decimal]
    semantic_deliveries: list[dict[str, Any]] = field(default_factory=list)
    governance_settlements: list[Any] = field(default_factory=list)
    governance_applied_updates: list[Any] = field(default_factory=list)
    mempool_processed: int = 0
    mempool_congestion: int = 0
    congestion_dropped_count: int = 0
    congestion_dropped_retail_count: int = 0
    congestion_dropped_agent_ids: list[str] = field(default_factory=list)
    congestion_dropped_meta: list[dict[str, str]] = field(default_factory=list)
    failed_reason_counts: dict[str, int] = field(default_factory=dict)


class Simulation_Orchestrator:
    """Dual-track orchestrator: fast semantic events + slow economic tick settlement."""

    def __init__(
        self,
        engine: ACE_Engine,
        ticks_per_day: int = 100,
        secretary: SecretaryAuditor | None = None,
        topology: SocialNetworkGraph | None = None,
        perception_filter: PerceptionFilter | None = None,
        channel_manager: ChannelManager | None = None,
        governance: GovernanceModule | None = None,
        max_tx_per_tick: int = 50,
        default_max_inbox_size: int = 5,
        metrics_logger: LoggerMetrics | None = None,
        state_checkpoint: StateCheckpoint | None = None,
        execution_mitigation: BaseExecutionMitigation | None = None,
    ) -> None:
        if ticks_per_day <= 0:
            raise ValueError("ticks_per_day must be > 0")
        if max_tx_per_tick <= 0:
            raise ValueError("max_tx_per_tick must be > 0")
        if default_max_inbox_size <= 0:
            raise ValueError("default_max_inbox_size must be > 0")

        self.engine = engine
        self.current_tick: int = 0
        self.ticks_per_day: int = int(ticks_per_day)
        self.max_tx_per_tick: int = int(max_tx_per_tick)
        self.default_max_inbox_size: int = int(default_max_inbox_size)
        self.engine.set_simulation_clock(self.current_tick, self.ticks_per_day)

        self.mempool: list[Transaction] = []
        self.event_bus: deque[SemanticEvent] = deque()
        self.protocol_fee_vault: dict[str, Decimal] = {
            "UST": Decimal("0"),
            "LUNA": Decimal("0"),
            "USDC": Decimal("0"),
        }
        self.enqueue_seq: int = 0
        self.tick_history: dict[int, TickSettlementReport] = {}
        self.halted: bool = False
        self.secretary = secretary or SecretaryAuditor()
        self.governance = governance or GovernanceModule(db_path=self.engine.get_db_path())
        self.metrics_logger = metrics_logger
        self.state_checkpoint = state_checkpoint
        self.execution_mitigation = execution_mitigation
        self._logger = logging.getLogger(__name__)
        self._last_tick_panic_word_freq: Decimal = Decimal("0")
        self._account_first_seen_tick: dict[str, int] = {}
        self._event_subscribers: list[Callable[[SemanticEvent], None]] = []

        if channel_manager is not None:
            self.channel_manager = channel_manager
            self.topology = channel_manager.topology
            self.perception_filter = channel_manager.perception_filter
        else:
            self.topology = topology or SocialNetworkGraph()
            self.perception_filter = perception_filter or PerceptionFilter()
            self.channel_manager = ChannelManager(
                topology=self.topology,
                db_path=self.engine.get_db_path(),
                perception_filter=self.perception_filter,
            )

        self._db_path = Path(self.engine.get_db_path()).resolve()
        self._aux_conn = sqlite3.connect(self._db_path)
        self._aux_conn.execute("PRAGMA journal_mode=WAL;")
        self._init_thought_log_table()

    def close(self) -> None:
        self.channel_manager.close()
        self.governance.close()
        if getattr(self, "_aux_conn", None) is not None:
            self._aux_conn.close()
            self._aux_conn = None

    # --------------------------
    # Topology helpers
    # --------------------------
    def register_agent(self, agent_id: str, role: str, community_id: str) -> None:
        self.topology.add_agent(agent_id=agent_id, role=role, community_id=community_id)
        self._account_first_seen_tick.setdefault(str(agent_id), int(self.current_tick))

    def connect_agents(self, sender: str, receiver: str, weight: float = 1.0) -> None:
        self.topology.connect(sender=sender, receiver=receiver, weight=weight)

    def build_social_topology(self, seed: int = 42) -> None:
        self.topology.build_layered_mixed_topology(seed=seed)

    # --------------------------
    # Fast loop (semantic path)
    # --------------------------
    def register_event_subscriber(self, callback: Callable[[SemanticEvent], None]) -> None:
        self._event_subscribers.append(callback)

    def submit_event(
        self,
        agent_id: str,
        action_type: str,
        params: dict[str, Any],
    ) -> str:
        normalized_action = normalize_action_type(action_type)
        if not is_semantic_action(normalized_action):
            raise ActionValidationError(
                f"{normalized_action} is economic and must use submit_transaction"
            )
        validated_params = validate_action_schema(normalized_action, params)

        if normalized_action == "PROPOSE":
            return self.governance.submit_proposal(
                proposer=agent_id,
                proposal_text=validated_params["proposal_text"],
                current_tick=self.current_tick,
                engine=self.engine,
            )
        if normalized_action == "VOTE":
            return self.governance.submit_vote(
                voter=agent_id,
                proposal_id=validated_params["proposal_id"],
                decision=validated_params["decision"],
                current_tick=self.current_tick,
            )

        event = SemanticEvent(
            event_id=str(uuid4()),
            agent_id=agent_id,
            action_type=normalized_action,
            params=validated_params,
            emit_tick=self.current_tick,
            parent_event_id=validated_params.get("parent_event_id"),
        )
        self.event_bus.append(event)
        for callback in list(self._event_subscribers):
            try:
                callback(event)
            except Exception:  # noqa: BLE001
                continue

        # Fast-loop immediate routing + immediate delivery for zero-delay messages.
        self.process_fast_events()
        self.channel_manager.deliver_due(current_tick=self.current_tick)
        return event.event_id

    def process_fast_events(self, max_events: int | None = None) -> list[QueuedDelivery]:
        processed: list[QueuedDelivery] = []
        handled = 0
        while self.event_bus and (max_events is None or handled < max_events):
            event = self.event_bus.popleft()
            routed = self.channel_manager.route_event(event, current_tick=self.current_tick)
            processed.extend(routed)
            handled += 1
        return processed

    def read_inbox(
        self, agent_id: str, max_inbox_size: int | None = None
    ) -> list[dict[str, Any]]:
        limit = self.default_max_inbox_size if max_inbox_size is None else int(max_inbox_size)
        return self.channel_manager.read_inbox(
            agent_id=agent_id,
            current_tick=self.current_tick,
            max_inbox_size=limit,
        )

    def get_public_state(self) -> dict[str, Any]:
        snapshot = self.engine.get_state_snapshot()
        pools = snapshot.get("pools", {})
        governance_state = self.governance.get_state()
        return {
            "tick": int(self.current_tick),
            "oracle_price_usdc_per_luna": snapshot.get("oracle_price_usdc_per_luna"),
            "Pool_A": pools.get("Pool_A"),
            "Pool_B": pools.get("Pool_B"),
            "protocol_fee_vault": {
                token: str(amount) for token, amount in self.protocol_fee_vault.items()
            },
            "governance": {
                "open_proposals": governance_state.get("open_proposals", 0),
                "parameter_version": governance_state.get("parameter_version", 0),
            },
        }

    def set_ticks_per_day(self, ticks_per_day: int) -> None:
        value = int(ticks_per_day)
        if value <= 0:
            raise ValueError("ticks_per_day must be > 0")
        self.ticks_per_day = value
        self.engine.set_simulation_clock(self.current_tick, self.ticks_per_day)

    def set_default_max_inbox_size(self, max_inbox_size: int) -> None:
        value = int(max_inbox_size)
        if value <= 0:
            raise ValueError("max_inbox_size must be > 0")
        self.default_max_inbox_size = value

    def log_agent_thought(
        self,
        agent_id: str,
        role: str,
        thought: str,
        speak_payload: dict[str, Any] | None,
        action_payload: dict[str, Any] | None,
        audit_status: str,
        audit_error: str | None = None,
    ) -> None:
        self._aux_conn.execute(
            """
            INSERT INTO thought_log (
                agent_id,
                role,
                thought,
                speak_json,
                action_json,
                tick,
                audit_status,
                audit_error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                role,
                thought,
                (
                    json.dumps(_jsonable(speak_payload), ensure_ascii=False)
                    if speak_payload
                    else None
                ),
                (
                    json.dumps(_jsonable(action_payload), ensure_ascii=False)
                    if action_payload
                    else None
                ),
                int(self.current_tick),
                audit_status,
                audit_error,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        self._aux_conn.commit()

    # --------------------------
    # Slow loop (economic path)
    # --------------------------
    def submit_transaction(
        self,
        agent_id: str,
        action_type: str,
        params: dict[str, Any],
        gas_price: Any,
    ) -> str:
        normalized_action = normalize_action_type(action_type)
        if not is_economic_action(normalized_action):
            raise ActionValidationError(
                f"{normalized_action} is semantic and must use submit_event"
            )

        validated_params = validate_action_schema(normalized_action, params)
        gas_price_dec = Decimal(str(gas_price))
        if gas_price_dec < 0:
            raise ActionValidationError("gas_price must be >= 0")

        tx = Transaction(
            tx_id=str(uuid4()),
            agent_id=agent_id,
            action_type=normalized_action,
            params=validated_params,
            gas_price=gas_price_dec,
            enqueue_seq=self.enqueue_seq,
            submit_tick=self.current_tick,
            raw_gas_price=gas_price_dec,
            effective_gas_price=gas_price_dec,
        )
        self.enqueue_seq += 1
        self._account_first_seen_tick.setdefault(str(agent_id), int(self.current_tick))

        if normalized_action == "SWAP":
            self.secretary.resolve_swap_guardrail(tx, self.engine)

        self.mempool.append(tx)
        return tx.tx_id

    def step_tick(self) -> TickSettlementReport:
        if self.halted:
            raise RuntimeError("orchestrator halted due to fatal invariant violation")

        self.current_tick += 1
        governance_applied_updates = self.governance.apply_due_updates(
            current_tick=self.current_tick,
            engine=self.engine,
            orchestrator=self,
        )
        self.engine.set_simulation_clock(self.current_tick, self.ticks_per_day)

        # Drain semantic bus first, then deliver only due messages for current tick.
        self.process_fast_events()
        semantic_due = self.channel_manager.deliver_due(current_tick=self.current_tick)
        current_semantic_panic = semantic_panic_ratio_from_deliveries(
            [self._delivery_to_dict(item) for item in semantic_due]
        )

        batch_all = self._drain_mempool()
        for tx in batch_all:
            if tx.raw_gas_price is None:
                tx.raw_gas_price = Decimal(str(tx.gas_price))
            if tx.effective_gas_price is None:
                tx.effective_gas_price = Decimal(str(tx.raw_gas_price))
            if tx.mitigation_flags is None:
                tx.mitigation_flags = []

        if self.execution_mitigation is not None:
            account_roles: dict[str, str] = {}
            for tx in batch_all:
                agent = str(tx.agent_id)
                if agent in account_roles:
                    continue
                role = "retail"
                if agent in self.topology.graph:
                    role = str(self.topology.graph.nodes[agent].get("role", "retail"))
                account_roles[agent] = role
                self._account_first_seen_tick.setdefault(agent, int(self.current_tick))

            mitigation_result = self.execution_mitigation.apply_policy(
                transactions=batch_all,
                current_tick=self.current_tick,
                last_tick_panic_word_freq=self._last_tick_panic_word_freq,
                current_semantic_panic_word_freq=current_semantic_panic,
                account_first_seen_tick=self._account_first_seen_tick,
                account_roles=account_roles,
            )
            batch_all = list(mitigation_result.ordered_transactions)
        else:
            batch_all.sort(
                key=lambda tx: (
                    -Decimal(str(tx.effective_gas_price)),
                    tx.enqueue_seq,
                )
            )
        batch = batch_all[: self.max_tx_per_tick]
        leftovers = batch_all[self.max_tx_per_tick :]
        if leftovers:
            self.mempool.extend(leftovers)
        congestion_dropped_count = len(leftovers)
        congestion_dropped_agent_ids = [str(tx.agent_id) for tx in leftovers]
        congestion_dropped_meta = [
            {
                "tx_id": str(tx.tx_id),
                "agent_id": str(tx.agent_id),
                "raw_gas": str(tx.raw_gas_price),
                "effective_gas": str(tx.effective_gas_price),
            }
            for tx in leftovers
        ]
        congestion_dropped_retail_count = 0
        for tx in leftovers:
            agent = str(tx.agent_id)
            role = "retail"
            if agent in self.topology.graph:
                role = str(self.topology.graph.nodes[agent].get("role", "retail"))
            if role == "retail":
                congestion_dropped_retail_count += 1

        receipts: list[TxReceipt] = []
        failed_reason_counts: dict[str, int] = {
            "slippage": 0,
            "balance": 0,
            "validation": 0,
            "invariant": 0,
            "congestion": int(congestion_dropped_count),
            "other": 0,
        }
        for rank, tx in enumerate(batch):
            gas_token: str | None = None
            gas_paid = Decimal("0")
            try:
                self.secretary.precheck_transaction(tx, self.engine, self.current_tick)

                gas_token = action_principal_token(tx.action_type, tx.params)
                effective_gas = Decimal(str(tx.effective_gas_price))
                raw_gas = Decimal(str(tx.raw_gas_price))
                if effective_gas > 0:
                    self.engine.charge_fee(
                        address=tx.agent_id,
                        token=gas_token,
                        amount=effective_gas,
                        reason=(
                            f"tick={self.current_tick},tx={tx.tx_id},"
                            f"raw_gas={raw_gas},effective_gas={effective_gas}"
                        ),
                    )
                    self.protocol_fee_vault[gas_token] += effective_gas
                    gas_paid = effective_gas

                result = self._dispatch_economic_tx(tx)
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="success",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
                        gas_bid=Decimal(str(tx.raw_gas_price)),
                        gas_effective=Decimal(str(tx.effective_gas_price)),
                        gas_token=gas_token,
                        gas_paid=gas_paid,
                        result=result,
                    )
                )
            except SlippageExceededError as exc:
                failed_reason_counts["slippage"] += 1
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="failed",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
                        gas_bid=Decimal(str(tx.raw_gas_price)),
                        gas_effective=Decimal(str(tx.effective_gas_price)),
                        gas_token=gas_token,
                        gas_paid=gas_paid,
                        error_code="SlippageExceededError",
                        error_message=str(exc),
                    )
                )
                continue
            except (
                ActionValidationError,
                InsufficientBalanceError,
                InsufficientFundsError,
                PermissionError,
                ValueError,
            ) as exc:
                reason_key = _classify_failed_reason(type(exc).__name__)
                failed_reason_counts[reason_key] = failed_reason_counts.get(reason_key, 0) + 1
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="failed",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
                        gas_bid=Decimal(str(tx.raw_gas_price)),
                        gas_effective=Decimal(str(tx.effective_gas_price)),
                        gas_token=gas_token,
                        gas_paid=gas_paid,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                continue
            except InvariantViolationError as exc:
                failed_reason_counts["invariant"] += 1
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="fatal",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
                        gas_bid=Decimal(str(tx.raw_gas_price)),
                        gas_effective=Decimal(str(tx.effective_gas_price)),
                        gas_token=gas_token,
                        gas_paid=gas_paid,
                        error_code="InvariantViolationError",
                        error_message=str(exc),
                    )
                )
                self.halted = True
                break

        governance_settlements = self.governance.settle_due(current_tick=self.current_tick)
        end_snapshot = self.engine.get_state_snapshot()
        report = TickSettlementReport(
            tick=self.current_tick,
            receipts=receipts,
            end_snapshot=end_snapshot,
            fee_vault_snapshot={k: Decimal(v) for k, v in self.protocol_fee_vault.items()},
            semantic_deliveries=[self._delivery_to_dict(item) for item in semantic_due],
            governance_settlements=governance_settlements,
            governance_applied_updates=governance_applied_updates,
            mempool_processed=len(batch),
            mempool_congestion=len(self.mempool),
            congestion_dropped_count=int(congestion_dropped_count),
            congestion_dropped_retail_count=int(congestion_dropped_retail_count),
            congestion_dropped_agent_ids=congestion_dropped_agent_ids,
            congestion_dropped_meta=congestion_dropped_meta,
            failed_reason_counts=failed_reason_counts,
        )

        if self.metrics_logger is not None:
            self.metrics_logger.record_tick(orchestrator=self, report=report)
        if self.state_checkpoint is not None:
            self.state_checkpoint.save_tick(orchestrator=self, report=report)

        self.tick_history[self.current_tick] = report
        self._last_tick_panic_word_freq = current_semantic_panic
        return report

    def _drain_mempool(self) -> list[Transaction]:
        batch = list(self.mempool)
        self.mempool.clear()
        return batch

    def _dispatch_economic_tx(self, tx: Transaction) -> dict[str, Any]:
        if tx.action_type == "SWAP":
            return self.engine.swap(
                address=tx.agent_id,
                pool_name=tx.params["pool_name"],
                token_in=tx.params["token_in"],
                amount=tx.params["amount"],
                min_amount_out=tx.resolved_min_amount_out,
            )
        if tx.action_type == "UST_TO_LUNA":
            return self.engine.ust_to_luna(tx.agent_id, tx.params["amount_ust"])
        if tx.action_type == "LUNA_TO_UST":
            return self.engine.luna_to_ust(tx.agent_id, tx.params["amount_luna"])
        raise ActionValidationError(f"unsupported economic action: {tx.action_type}")

    def _delivery_to_dict(self, delivery: QueuedDelivery) -> dict[str, Any]:
        return {
            "delivery_id": delivery.delivery_id,
            "event_id": delivery.event_id,
            "parent_event_id": delivery.parent_event_id,
            "sender": delivery.sender,
            "receiver": delivery.receiver,
            "channel": delivery.channel,
            "emit_tick": delivery.emit_tick,
            "deliver_tick": delivery.deliver_tick,
            "raw_text": delivery.raw_text,
            "perceived_text": delivery.perceived_text,
            "transform_tag": delivery.transform_tag,
        }

    def _init_thought_log_table(self) -> None:
        self._aux_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thought_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL,
                thought TEXT NOT NULL,
                speak_json TEXT,
                action_json TEXT,
                tick INTEGER NOT NULL,
                audit_status TEXT NOT NULL,
                audit_error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._aux_conn.commit()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _classify_failed_reason(error_code: str) -> str:
    code = str(error_code).strip()
    if code == "SlippageExceededError":
        return "slippage"
    if code in {"InsufficientBalanceError", "InsufficientFundsError"}:
        return "balance"
    if code in {"ActionValidationError", "PermissionError", "ValueError"}:
        return "validation"
    if code == "InvariantViolationError":
        return "invariant"
    return "other"


__all__ = [
    "Simulation_Orchestrator",
    "Transaction",
    "SemanticEvent",
    "TxReceipt",
    "TickSettlementReport",
]
