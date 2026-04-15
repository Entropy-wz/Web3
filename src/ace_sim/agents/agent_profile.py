from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..config.llm_config import resolve_role_route


@dataclass
class AttentionPolicy:
    price_change_threshold: Decimal = Decimal("0.01")
    risk_wake_threshold: Decimal = Decimal("0.70")
    force_wake_interval: int = 12
    memory_top_k: int = 6


@dataclass
class AgentProfile:
    agent_id: str
    role: str
    llm_backend: str
    llm_model: str
    risk_threshold: Decimal
    hidden_goals: list[str] = field(default_factory=list)
    attention_policy: AttentionPolicy = field(default_factory=AttentionPolicy)


def default_agent_profile(agent_id: str, role: str) -> AgentProfile:
    role_norm = role.strip().lower()
    if role_norm == "whale":
        backend, model = resolve_role_route(
            role="whale",
            default_backend="openai",
            default_model="gpt-4o",
        )
        return AgentProfile(
            agent_id=agent_id,
            role="whale",
            llm_backend=backend,
            llm_model=model,
            risk_threshold=Decimal("0.65"),
            hidden_goals=[
                "maximize pnl while minimizing visible slippage",
                "front-run weak liquidity windows",
            ],
            attention_policy=AttentionPolicy(
                price_change_threshold=Decimal("0.008"),
                risk_wake_threshold=Decimal("0.60"),
                force_wake_interval=8,
                memory_top_k=8,
            ),
        )
    if role_norm == "project":
        backend, model = resolve_role_route(
            role="project",
            default_backend="openai",
            default_model="gpt-4o-mini",
        )
        return AgentProfile(
            agent_id=agent_id,
            role="project",
            llm_backend=backend,
            llm_model=model,
            risk_threshold=Decimal("0.55"),
            hidden_goals=[
                "reduce panic spread",
                "defend peg confidence narrative",
            ],
            attention_policy=AttentionPolicy(
                price_change_threshold=Decimal("0.009"),
                risk_wake_threshold=Decimal("0.55"),
                force_wake_interval=10,
                memory_top_k=7,
            ),
        )
    backend, model = resolve_role_route(
        role="retail",
        default_backend="openai",
        default_model="gpt-4o-mini",
    )
    return AgentProfile(
        agent_id=agent_id,
        role="retail",
        llm_backend=backend,
        llm_model=model,
        risk_threshold=Decimal("0.75"),
        hidden_goals=[
            "avoid large drawdowns",
            "follow strong social signals quickly",
        ],
        attention_policy=AttentionPolicy(
            price_change_threshold=Decimal("0.01"),
            risk_wake_threshold=Decimal("0.75"),
            force_wake_interval=15,
            memory_top_k=5,
        ),
    )


__all__ = [
    "AgentProfile",
    "AttentionPolicy",
    "default_agent_profile",
]
