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

SYSTEM_OVERLOAD_MESSAGE = "[System] You missed a large batch of noisy unread messages..."


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

        message = str(event.params["message"])
        sender = str(event.agent_id)
        emit_tick = int(event.emit_tick)
        parent_event_id = event.parent_event_id

        channel = self._normalize_channel(event.params)
        recipients = self._resolve_recipients(sender, channel, event.params)

        scheduled: list[QueuedDelivery] = []
        for receiver, distance in recipients:
            is_cross = self.topology.is_cross_community(sender, receiver)
            apply_cross_decay = is_cross and channel != "PUBLIC_CHANNEL"

            filtered = self.perception_filter.transform(
                message=message,
                sender=sender,
                receiver=receiver,
                channel=channel,
                is_cross_community=apply_cross_decay,
                current_tick=current_tick,
            )

            distance_filtered = self.perception_filter.transmit_info(
                message=filtered.message,
                distance=int(distance),
                channel=channel,
            )

            combined_delay = int(filtered.delay_ticks) + int(distance_filtered.delay_ticks)
            combined_tag = self._combine_tags(filtered.transform_tag, distance_filtered.transform_tag)

            delivery = QueuedDelivery(
                delivery_id=str(uuid4()),
                event_id=str(event.event_id),
                parent_event_id=parent_event_id,
                sender=sender,
                receiver=receiver,
                channel=channel,
                raw_text=message,
                perceived_text=distance_filtered.message,
                emit_tick=emit_tick,
                deliver_tick=current_tick + combined_delay,
                transform_tag=combined_tag,
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

    def _normalize_channel(self, params: dict[str, Any]) -> str:
        explicit = params.get("channel")
        if explicit is not None and str(explicit).strip():
            label = str(explicit).strip().upper()
        else:
            label = str(params.get("target", "forum")).strip().upper()

        mapping = {
            "SYSTEM": "SYSTEM_NEWS",
            "SYSTEM_NEWS": "SYSTEM_NEWS",
            "NEWS": "SYSTEM_NEWS",
            "FORUM": "FORUM",
            "PUBLIC": "PUBLIC_CHANNEL",
            "PUBLIC_CHANNEL": "PUBLIC_CHANNEL",
            "TWITTER": "PUBLIC_CHANNEL",
            "PRIVATE": "PRIVATE_CHANNEL",
            "PRIVATE_CHANNEL": "PRIVATE_CHANNEL",
            "DM": "PRIVATE_CHANNEL",
            "DIRECT_MESSAGE": "PRIVATE_CHANNEL",
        }
        return mapping.get(label, "FORUM")

    def _resolve_recipients(
        self,
        sender: str,
        channel: str,
        params: dict[str, Any],
    ) -> list[tuple[str, int]]:
        if channel == "SYSTEM_NEWS":
            return [(agent, 1) for agent in self.topology.all_agents() if agent != sender]

        if channel == "PRIVATE_CHANNEL":
            receiver = str(params.get("receiver", "")).strip()
            if not receiver:
                return []
            if receiver == sender:
                return []
            distance = self.topology.shortest_distance(sender, receiver)
            return [(receiver, 1 if distance is None else max(1, int(distance)))]

        if channel == "PUBLIC_CHANNEL":
            return self.topology.reachable_listeners(sender)

        return [(agent, 1) for agent in self.topology.listeners_of(sender) if agent != sender]

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
        normalized = str(channel).upper()
        if normalized == "SYSTEM_NEWS":
            return 3
        if normalized == "PRIVATE_CHANNEL":
            return 2
        return 1

    def _combine_tags(self, first: str, second: str) -> str:
        seen: set[str] = set()
        tags: list[str] = []
        for tag in [first, second]:
            if not tag or tag == "none" or tag in seen:
                continue
            tags.append(tag)
            seen.add(tag)
        if not tags:
            return "none"
        if len(tags) == 1:
            return tags[0]
        return "+".join(tags)

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
