from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Protocol


class PerceptionModelAdapter(Protocol):
    def transform(
        self,
        message: str,
        sender: str,
        receiver: str,
        channel: str,
    ) -> str: ...


@dataclass
class FilterResult:
    message: str
    delay_ticks: int
    transform_tag: str


class PerceptionFilter:
    """Perception interceptor with rule fallback for cross-community rumor decay."""

    def __init__(
        self,
        cross_community_delay_ticks: int = 2,
        prefix_probability: float = 0.3,
        seed: int = 42,
        model_adapter: PerceptionModelAdapter | None = None,
    ) -> None:
        if cross_community_delay_ticks < 0:
            raise ValueError("cross_community_delay_ticks must be >= 0")
        if prefix_probability < 0 or prefix_probability > 1:
            raise ValueError("prefix_probability must be in [0, 1]")

        self.cross_community_delay_ticks = int(cross_community_delay_ticks)
        self.prefix_probability = float(prefix_probability)
        self.model_adapter = model_adapter
        self._rng = random.Random(seed)

    def transform(
        self,
        message: str,
        sender: str,
        receiver: str,
        channel: str,
        is_cross_community: bool,
        current_tick: int,
    ) -> FilterResult:
        del current_tick  # reserved for future adaptive delay policies
        normalized_channel = str(channel).strip().upper()
        if normalized_channel != "FORUM" or not is_cross_community:
            return FilterResult(
                message=message,
                delay_ticks=0,
                transform_tag="none",
            )

        if self.model_adapter is not None:
            try:
                transformed = self.model_adapter.transform(
                    message=message,
                    sender=sender,
                    receiver=receiver,
                    channel=normalized_channel,
                )
                return FilterResult(
                    message=str(transformed),
                    delay_ticks=self.cross_community_delay_ticks,
                    transform_tag="model",
                )
            except Exception:  # noqa: BLE001
                pass

        return FilterResult(
            message=self._rule_decay(message),
            delay_ticks=self.cross_community_delay_ticks,
            transform_tag="rule",
        )

    def _rule_decay(self, message: str) -> str:
        text = str(message)
        # Collapse concrete numbers to a panic-friendly generalized token.
        text = re.sub(
            r"(?<!\w)(?:\$)?\d+(?:\.\d+)?(?:e[+-]?\d+)?(?:%|x|倍)?",
            "[价格暴跌]",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s{2,}", " ", text).strip()

        if self._rng.random() < self.prefix_probability:
            prefix = self._rng.choice(["[据传]", "[恐慌]"])
            text = f"{prefix} {text}"
        return text


__all__ = [
    "PerceptionFilter",
    "FilterResult",
    "PerceptionModelAdapter",
]
