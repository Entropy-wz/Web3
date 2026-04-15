from __future__ import annotations

import json
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
from ..guardrails.secretary_auditor import InsufficientBalanceError, SecretaryAuditor


@dataclass
class Transaction:
    tx_id: str
    agent_id: str
    action_type: str
    params: dict[str, Any]
    gas_price: Decimal
    enqueue_seq: int
    submit_tick: int
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
    ) -> None:
        if ticks_per_day <= 0:
            raise ValueError("ticks_per_day must be > 0")

        self.engine = engine
        self.current_tick: int = 0
        self.ticks_per_day: int = int(ticks_per_day)
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
        if getattr(self, "_aux_conn", None) is not None:
            self._aux_conn.close()
            self._aux_conn = None

    # --------------------------
    # Topology helpers
    # --------------------------
    def register_agent(self, agent_id: str, role: str, community_id: str) -> None:
        self.topology.add_agent(agent_id=agent_id, role=role, community_id=community_id)

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

    def read_inbox(self, agent_id: str, max_inbox_size: int = 5) -> list[dict[str, Any]]:
        return self.channel_manager.read_inbox(
            agent_id=agent_id,
            current_tick=self.current_tick,
            max_inbox_size=max_inbox_size,
        )

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
        )
        self.enqueue_seq += 1

        if normalized_action == "SWAP":
            self.secretary.resolve_swap_guardrail(tx, self.engine)

        self.mempool.append(tx)
        return tx.tx_id

    def step_tick(self) -> TickSettlementReport:
        if self.halted:
            raise RuntimeError("orchestrator halted due to fatal invariant violation")

        self.current_tick += 1
        self.engine.set_simulation_clock(self.current_tick, self.ticks_per_day)

        # Drain semantic bus first, then deliver only due messages for current tick.
        self.process_fast_events()
        semantic_due = self.channel_manager.deliver_due(current_tick=self.current_tick)

        batch = self._drain_mempool()
        batch.sort(key=lambda tx: (-tx.gas_price, tx.enqueue_seq))

        receipts: list[TxReceipt] = []
        for rank, tx in enumerate(batch):
            gas_token: str | None = None
            gas_paid = Decimal("0")
            try:
                self.secretary.precheck_transaction(tx, self.engine, self.current_tick)

                gas_token = action_principal_token(tx.action_type, tx.params)
                if tx.gas_price > 0:
                    self.engine.charge_fee(
                        address=tx.agent_id,
                        token=gas_token,
                        amount=tx.gas_price,
                        reason=f"tick={self.current_tick},tx={tx.tx_id}",
                    )
                    self.protocol_fee_vault[gas_token] += tx.gas_price
                    gas_paid = tx.gas_price

                result = self._dispatch_economic_tx(tx)
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="success",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
                        gas_token=gas_token,
                        gas_paid=gas_paid,
                        result=result,
                    )
                )
            except SlippageExceededError as exc:
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="failed",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
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
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="failed",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
                        gas_token=gas_token,
                        gas_paid=gas_paid,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                continue
            except InvariantViolationError as exc:
                receipts.append(
                    TxReceipt(
                        tx_id=tx.tx_id,
                        tick=self.current_tick,
                        rank=rank,
                        status="fatal",
                        action_type=tx.action_type,
                        agent_id=tx.agent_id,
                        gas_token=gas_token,
                        gas_paid=gas_paid,
                        error_code="InvariantViolationError",
                        error_message=str(exc),
                    )
                )
                self.halted = True
                break

        end_snapshot = self.engine.get_state_snapshot()
        report = TickSettlementReport(
            tick=self.current_tick,
            receipts=receipts,
            end_snapshot=end_snapshot,
            fee_vault_snapshot={k: Decimal(v) for k, v in self.protocol_fee_vault.items()},
            semantic_deliveries=[self._delivery_to_dict(item) for item in semantic_due],
        )
        self.tick_history[self.current_tick] = report
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


__all__ = [
    "Simulation_Orchestrator",
    "Transaction",
    "SemanticEvent",
    "TxReceipt",
    "TickSettlementReport",
]
