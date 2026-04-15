from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .network_graph import SocialNetworkGraph
from .perception_filter import PerceptionFilter

SYSTEM_OVERLOAD_MESSAGE = "[System] 你错过了大量嘈杂的未读消息..."


@dataclass
class QueuedDelivery:
    delivery_id: str
    event_id: str
    parent_event_id: str | None
    sender: str
    receiver: str
    channel: str
    raw_text: str
    perceived_text: str
    emit_tick: int
    deliver_tick: int
    transform_tag: str


class ChannelManager:
    """Channel routing for fast-loop semantic events with delayed delivery."""

    def __init__(
        self,
        topology: SocialNetworkGraph,
        db_path: str | Path,
        perception_filter: PerceptionFilter | None = None,
    ) -> None:
        self.topology = topology
        self.perception_filter = perception_filter or PerceptionFilter()
        self.pending_deliveries: list[QueuedDelivery] = []
        self._inbox: dict[str, list[QueuedDelivery]] = defaultdict(list)

        self._db_path = Path(db_path).resolve()
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_tables()

    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None

    def route_event(self, event: Any, current_tick: int) -> list[QueuedDelivery]:
        action_type = str(event.action_type).strip().upper()
        if action_type != "SPEAK":
            return []

        target = str(event.params.get("target", "forum")).strip().upper()
        channel = "SYSTEM_NEWS" if target in {"SYSTEM_NEWS", "SYSTEM", "NEWS"} else "FORUM"
        message = str(event.params["message"])
        sender = str(event.agent_id)
        emit_tick = int(event.emit_tick)
        parent_event_id = event.parent_event_id
        recipients = self._resolve_recipients(sender, channel)

        scheduled: list[QueuedDelivery] = []
        for receiver in recipients:
            is_cross = self.topology.is_cross_community(sender, receiver)
            filtered = self.perception_filter.transform(
                message=message,
                sender=sender,
                receiver=receiver,
                channel=channel,
                is_cross_community=is_cross,
                current_tick=current_tick,
            )
            delivery = QueuedDelivery(
                delivery_id=str(uuid4()),
                event_id=str(event.event_id),
                parent_event_id=parent_event_id,
                sender=sender,
                receiver=receiver,
                channel=channel,
                raw_text=message,
                perceived_text=filtered.message,
                emit_tick=emit_tick,
                deliver_tick=current_tick + int(filtered.delay_ticks),
                transform_tag=filtered.transform_tag,
            )
            self.pending_deliveries.append(delivery)
            self._write_semantic_delivery_log(delivery)
            scheduled.append(delivery)
        return scheduled

    def deliver_due(self, current_tick: int) -> list[QueuedDelivery]:
        ready: list[QueuedDelivery] = []
        pending_next: list[QueuedDelivery] = []
        for item in self.pending_deliveries:
            if item.deliver_tick <= current_tick:
                ready.append(item)
            else:
                pending_next.append(item)
        self.pending_deliveries = pending_next

        for item in ready:
            self._inbox[item.receiver].append(item)
        return ready

    def read_inbox(
        self,
        agent_id: str,
        current_tick: int,
        max_inbox_size: int = 5,
    ) -> list[dict[str, Any]]:
        if max_inbox_size <= 0:
            raise ValueError("max_inbox_size must be > 0")
        receiver = str(agent_id).strip()
        pending = list(self._inbox.get(receiver, []))
        if not pending:
            return []

        ranking = sorted(
            pending,
            key=lambda m: (
                -self._channel_weight(m.channel),
                -m.deliver_tick,
                -m.emit_tick,
                m.delivery_id,
            ),
        )
        self._inbox[receiver].clear()

        dropped_count = 0
        selected: list[QueuedDelivery] = ranking
        dropped_ids: list[str] = []
        if len(ranking) > max_inbox_size:
            if max_inbox_size == 1:
                selected = []
            else:
                selected = ranking[: max_inbox_size - 1]
            dropped = ranking[len(selected) :]
            dropped_count = len(dropped)
            dropped_ids = [item.delivery_id for item in dropped]

        payload = [self._delivery_to_message(item) for item in selected]
        if dropped_count > 0:
            overload_message = {
                "event_id": None,
                "parent_event_id": None,
                "sender": "SYSTEM",
                "receiver": receiver,
                "channel": "SYSTEM_NEWS",
                "message": SYSTEM_OVERLOAD_MESSAGE,
                "emit_tick": current_tick,
                "deliver_tick": current_tick,
                "transform_tag": "overload",
                "is_overload_notice": True,
            }
            if max_inbox_size == 1:
                payload = [overload_message]
            else:
                payload.append(overload_message)

        if dropped_ids:
            self._conn.executemany(
                """
                UPDATE semantic_delivery_log
                SET dropped_by_overload = 1
                WHERE delivery_id = ?
                """,
                [(delivery_id,) for delivery_id in dropped_ids],
            )
            self._conn.commit()

        self._write_overload_log(
            agent_id=receiver,
            tick=current_tick,
            total_pending=len(ranking),
            returned_count=len(payload),
            dropped_count=dropped_count,
        )
        return payload

    def _resolve_recipients(self, sender: str, channel: str) -> list[str]:
        if channel == "SYSTEM_NEWS":
            return [agent for agent in self.topology.all_agents() if agent != sender]
        return [agent for agent in self.topology.listeners_of(sender) if agent != sender]

    def _delivery_to_message(self, delivery: QueuedDelivery) -> dict[str, Any]:
        return {
            "event_id": delivery.event_id,
            "parent_event_id": delivery.parent_event_id,
            "sender": delivery.sender,
            "receiver": delivery.receiver,
            "channel": delivery.channel,
            "message": delivery.perceived_text,
            "emit_tick": delivery.emit_tick,
            "deliver_tick": delivery.deliver_tick,
            "transform_tag": delivery.transform_tag,
            "is_overload_notice": False,
        }

    def _channel_weight(self, channel: str) -> int:
        return 2 if str(channel).upper() == "SYSTEM_NEWS" else 1

    def _init_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                event_id TEXT NOT NULL,
                parent_event_id TEXT,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL,
                channel TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                perceived_text TEXT NOT NULL,
                emit_tick INTEGER NOT NULL,
                deliver_tick INTEGER NOT NULL,
                transform_tag TEXT NOT NULL,
                dropped_by_overload INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbox_overload_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                tick INTEGER NOT NULL,
                total_pending INTEGER NOT NULL,
                returned_count INTEGER NOT NULL,
                dropped_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _write_semantic_delivery_log(self, delivery: QueuedDelivery) -> None:
        self._conn.execute(
            """
            INSERT INTO semantic_delivery_log (
                delivery_id,
                event_id,
                parent_event_id,
                sender,
                receiver,
                channel,
                raw_text,
                perceived_text,
                emit_tick,
                deliver_tick,
                transform_tag,
                dropped_by_overload,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery.delivery_id,
                delivery.event_id,
                delivery.parent_event_id,
                delivery.sender,
                delivery.receiver,
                delivery.channel,
                delivery.raw_text,
                delivery.perceived_text,
                int(delivery.emit_tick),
                int(delivery.deliver_tick),
                delivery.transform_tag,
                0,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def _write_overload_log(
        self,
        agent_id: str,
        tick: int,
        total_pending: int,
        returned_count: int,
        dropped_count: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO inbox_overload_log (
                agent_id,
                tick,
                total_pending,
                returned_count,
                dropped_count,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                int(tick),
                int(total_pending),
                int(returned_count),
                int(dropped_count),
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()


__all__ = [
    "ChannelManager",
    "QueuedDelivery",
    "SYSTEM_OVERLOAD_MESSAGE",
]
