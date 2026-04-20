from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
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
class ExecutionPolicyResult:
    ordered_transactions: list[Any]
    crisis_mode: bool
    panic_signal: Decimal
    capped_count: int


class BaseExecutionMitigation:
    def apply_policy(
        self,
        *,
        transactions: list[Any],
        current_tick: int,
        last_tick_panic_word_freq: Decimal,
        current_semantic_panic_word_freq: Decimal,
        account_first_seen_tick: dict[str, int],
        account_roles: dict[str, str],
    ) -> ExecutionPolicyResult:
        raise NotImplementedError


class ExecutionCircuitBreaker(BaseExecutionMitigation):
    def __init__(
        self,
        *,
        panic_threshold: Decimal | str = Decimal("0.5"),
        crisis_gas_cap: Decimal | str = Decimal("50.0"),
        gas_weight: Decimal | str = Decimal("0.2"),
        age_weight: Decimal | str = Decimal("0.8"),
        age_norm_ticks: int = 100,
        role_bias: dict[str, Decimal] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.panic_threshold = Decimal(str(panic_threshold))
        self.crisis_gas_cap = Decimal(str(crisis_gas_cap))
        self.gas_weight = Decimal(str(gas_weight))
        self.age_weight = Decimal(str(age_weight))
        self.age_norm_ticks = int(age_norm_ticks)
        if self.panic_threshold < 0:
            raise ValueError("panic_threshold must be >= 0")
        if self.crisis_gas_cap <= 0:
            raise ValueError("crisis_gas_cap must be > 0")
        if self.age_norm_ticks <= 0:
            raise ValueError("age_norm_ticks must be > 0")
        if self.gas_weight < 0 or self.age_weight < 0:
            raise ValueError("gas_weight and age_weight must be >= 0")
        if (self.gas_weight + self.age_weight) <= 0:
            raise ValueError("gas_weight + age_weight must be > 0")

        self.role_bias = role_bias or {
            "retail": Decimal("1.0"),
            "project": Decimal("0.6"),
            "whale": Decimal("0.2"),
        }
        self._logger = logger or logging.getLogger(__name__)

    def apply_policy(
        self,
        *,
        transactions: list[Any],
        current_tick: int,
        last_tick_panic_word_freq: Decimal,
        current_semantic_panic_word_freq: Decimal,
        account_first_seen_tick: dict[str, int],
        account_roles: dict[str, str],
    ) -> ExecutionPolicyResult:
        if not transactions:
            return ExecutionPolicyResult(
                ordered_transactions=[],
                crisis_mode=False,
                panic_signal=Decimal("0"),
                capped_count=0,
            )

        panic_signal = max(
            Decimal(str(last_tick_panic_word_freq)),
            Decimal(str(current_semantic_panic_word_freq)),
        )
        crisis_mode = panic_signal > self.panic_threshold
        capped_count = 0

        if crisis_mode:
            self._logger.info(
                "[MITIGATION-B-ACTIVATE] Social Panic Detected! Switching to Fair-Weight Sorting. "
                "tick=%d panic_signal=%s threshold=%s",
                int(current_tick),
                str(panic_signal),
                str(self.panic_threshold),
            )

        for tx in transactions:
            raw_gas = Decimal(str(getattr(tx, "raw_gas_price", getattr(tx, "gas_price", "0"))))
            tx.raw_gas_price = raw_gas
            tx.mitigation_flags = list(getattr(tx, "mitigation_flags", []))

            if not crisis_mode:
                tx.effective_gas_price = raw_gas
                continue

            effective = min(raw_gas, self.crisis_gas_cap)
            tx.effective_gas_price = effective
            if effective < raw_gas:
                capped_count += 1
                if "gas_capped" not in tx.mitigation_flags:
                    tx.mitigation_flags.append("gas_capped")
                self._logger.info(
                    "[GAS-CAPPED] tx_id=%s original_gas=%s capped_to=%s",
                    str(getattr(tx, "tx_id", "")),
                    str(raw_gas),
                    str(self.crisis_gas_cap),
                )

        if not crisis_mode:
            ordered = sorted(
                transactions,
                key=lambda item: (
                    -Decimal(str(item.effective_gas_price)),
                    int(getattr(item, "enqueue_seq", 0)),
                ),
            )
            return ExecutionPolicyResult(
                ordered_transactions=ordered,
                crisis_mode=False,
                panic_signal=panic_signal,
                capped_count=0,
            )

        def _score(tx: Any) -> Decimal:
            effective_gas = Decimal(str(tx.effective_gas_price))
            gas_norm = effective_gas / self.crisis_gas_cap
            if gas_norm > 1:
                gas_norm = Decimal("1")
            if gas_norm < 0:
                gas_norm = Decimal("0")

            first_seen_tick = int(account_first_seen_tick.get(str(tx.agent_id), 0))
            age_ticks = max(0, int(current_tick) - first_seen_tick)
            age_norm = Decimal(age_ticks) / Decimal(self.age_norm_ticks)
            if age_norm > 1:
                age_norm = Decimal("1")

            role = str(account_roles.get(str(tx.agent_id), "retail")).strip().lower()
            # role_bias is a Reputation QoS prior based on historical on-chain profile.
            role_bias = Decimal(str(self.role_bias.get(role, Decimal("0"))))
            age_score = age_norm + role_bias
            return self.gas_weight * gas_norm + self.age_weight * age_score

        ordered = sorted(
            transactions,
            key=lambda item: (-_score(item), int(getattr(item, "enqueue_seq", 0))),
        )
        return ExecutionPolicyResult(
            ordered_transactions=ordered,
            crisis_mode=True,
            panic_signal=panic_signal,
            capped_count=capped_count,
        )


def semantic_panic_ratio_from_deliveries(deliveries: list[dict[str, Any]]) -> Decimal:
    if not deliveries:
        return Decimal("0")
    hits = 0
    seen = 0
    for item in deliveries:
        text = str(item.get("perceived_text", "")).lower().strip()
        if not text:
            continue
        seen += 1
        if any(keyword in text for keyword in PANIC_KEYWORDS):
            hits += 1
    if seen == 0:
        return Decimal("0")
    return Decimal(hits) / Decimal(seen)


__all__ = [
    "BaseExecutionMitigation",
    "ExecutionCircuitBreaker",
    "ExecutionPolicyResult",
    "semantic_panic_ratio_from_deliveries",
]
