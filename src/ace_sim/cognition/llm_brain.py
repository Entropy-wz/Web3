from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..agents.agent_profile import AgentProfile
from ..config.llm_config import load_llm_config
from .llm_router import LLMRouter, RouteResult


class BrainOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thought: str
    speak: dict[str, Any] | None = None
    action: dict[str, Any] | None = None

    @field_validator("thought")
    @classmethod
    def thought_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("thought must be non-empty")
        return value.strip()


@dataclass
class BrainDecision:
    payload: dict[str, Any]
    backend_used: str
    model_used: str
    used_fallback: bool
    error: str | None


class LLMBrain:
    """Prompt assembly + structured output enforcement + resilient fallback."""

    def __init__(
        self,
        router: LLMRouter | None = None,
        default_timeout: float | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        self.router = router or LLMRouter(config_path=config_path)
        cfg = load_llm_config(config_path)
        resolved_timeout = (
            cfg.router.default_timeout if default_timeout is None else default_timeout
        )
        self.default_timeout = float(resolved_timeout)

    def decide(
        self,
        *,
        profile: AgentProfile,
        public_state: dict[str, Any],
        inbox_messages: list[dict[str, Any]],
        recalled_memories: list[dict[str, Any]],
        allowed_actions: list[str],
        timeout: float | None = None,
    ) -> BrainDecision:
        prompt = self.build_prompt(
            profile=profile,
            public_state=public_state,
            inbox_messages=inbox_messages,
            recalled_memories=recalled_memories,
            allowed_actions=allowed_actions,
        )

        route_result = self.router.route(
            profile=profile,
            prompt=prompt,
            schema=self.output_schema(),
            timeout=self.default_timeout if timeout is None else float(timeout),
        )

        try:
            output = BrainOutputModel(**route_result.decision).model_dump()
            return BrainDecision(
                payload=output,
                backend_used=route_result.backend_used,
                model_used=route_result.model_used,
                used_fallback=route_result.used_fallback,
                error=route_result.error,
            )
        except ValidationError as exc:
            fallback = self._rule_fallback(profile=profile, reason=str(exc))
            return BrainDecision(
                payload=fallback,
                backend_used="fallback",
                model_used="rule",
                used_fallback=True,
                error=str(exc),
            )

    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["thought", "speak", "action"],
            "properties": {
                "thought": {"type": "string"},
                "speak": {"type": ["object", "null"]},
                "action": {"type": ["object", "null"]},
            },
            "additionalProperties": False,
        }

    def build_prompt(
        self,
        *,
        profile: AgentProfile,
        public_state: dict[str, Any],
        inbox_messages: list[dict[str, Any]],
        recalled_memories: list[dict[str, Any]],
        allowed_actions: list[str],
    ) -> str:
        state_view = self._compress_public_state(public_state)
        memory_view = recalled_memories[: profile.attention_policy.memory_top_k]

        sections = [
            "You are a Web3 market participant in a multi-agent crisis simulation.",
            f"Role: {profile.role}",
            f"Hidden goals: {json.dumps(profile.hidden_goals, ensure_ascii=False)}",
            f"Risk threshold: {profile.risk_threshold}",
            f"Current state: {json.dumps(state_view, ensure_ascii=False)}",
            f"Inbox: {json.dumps(inbox_messages, ensure_ascii=False)}",
            f"Relevant memory: {json.dumps(memory_view, ensure_ascii=False)}",
            f"Allowed actions: {json.dumps(allowed_actions, ensure_ascii=False)}",
            (
                "Return strict JSON only: "
                '{"thought":"...","speak":{...}|null,"action":{...}|null}. '
                "No markdown, no extra keys."
            ),
        ]
        return "\n".join(sections)

    def _compress_public_state(self, public_state: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(public_state, dict):
            return {}

        oracle = public_state.get("oracle_price_usdc_per_luna")
        if oracle is not None:
            try:
                oracle = str(Decimal(str(oracle)))
            except Exception:  # noqa: BLE001
                oracle = str(oracle)

        return {
            "tick": public_state.get("tick") or public_state.get("current_tick"),
            "oracle_price_usdc_per_luna": oracle,
            "pool_a": public_state.get("Pool_A") or public_state.get("pool_a"),
            "pool_b": public_state.get("Pool_B") or public_state.get("pool_b"),
            "protocol_fees": public_state.get("protocol_fee_vault")
            or public_state.get("fee_vault"),
        }

    def _rule_fallback(self, profile: AgentProfile, reason: str) -> dict[str, Any]:
        del profile
        return {
            "thought": f"Fallback rule used due to parser/router issue: {reason}",
            "speak": None,
            "action": None,
        }


__all__ = [
    "LLMBrain",
    "BrainDecision",
    "BrainOutputModel",
]
