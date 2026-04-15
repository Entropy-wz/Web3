from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable

from .agent_profile import AgentProfile, default_agent_profile
from ..cognition.llm_brain import LLMBrain
from ..cognition.memory_stream import MemoryStream
from ..execution.action_registry.actions import is_economic_action
from ..execution.guardrails.secretary_auditor import SecretaryAuditor

if TYPE_CHECKING:
    from ..execution.orchestrator.time_orchestrator import Simulation_Orchestrator


LLMCallable = Callable[[str], dict[str, Any] | str]


@dataclass
class BaseAgent:
    agent_id: str
    role: str
    community_id: str
    llm_callable: LLMCallable | None = None
    profile: AgentProfile | None = None
    brain: LLMBrain | None = None
    memory_stream: MemoryStream | None = None
    max_inbox_size: int = 5

    last_wake_tick: int = -1
    last_oracle_price: Decimal | None = None
    sleep_count: int = 0
    wake_count: int = 0
    llm_call_count: int = 0

    def __post_init__(self) -> None:
        if self.profile is None:
            self.profile = default_agent_profile(self.agent_id, self.role)
        if self.brain is None and self.llm_callable is None:
            self.brain = LLMBrain()

    def filter_info(self, inbox_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(inbox_messages)

    def build_prompt(
        self,
        filtered_messages: list[dict[str, Any]],
        public_state: dict[str, Any] | None = None,
    ) -> str:
        return (
            "You are a Web3 market participant.\n"
            f"Role: {self.role}\n"
            f"Inbox: {json.dumps(filtered_messages, ensure_ascii=False)}\n"
            f"Public state: {json.dumps(public_state or {}, ensure_ascii=False)}\n"
            "Return JSON only: "
            '{"thought":"...","speak":{...}|null,"action":{...}|null}'
        )

    def cognition(self, prompt: str) -> dict[str, Any]:
        if self.llm_callable is None:
            return self._fallback_output()
        raw = self.llm_callable(prompt)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            return json.loads(raw)
        raise ValueError("llm_callable must return dict or JSON string")

    def _fallback_output(self) -> dict[str, Any]:
        return {
            "thought": "Market noise is high; stay cautious and observe.",
            "speak": {
                "target": "forum",
                "message": "Volatility is rising. Manage risk carefully.",
                "mode": "new",
            },
            "action": None,
        }

    def execute_action(
        self,
        orchestrator: "Simulation_Orchestrator",
        auditor: SecretaryAuditor | None = None,
        public_state: dict[str, Any] | None = None,
        max_inbox_size: int | None = None,
    ) -> dict[str, Any]:
        auditor_obj = auditor or orchestrator.secretary
        profile = self.profile or default_agent_profile(self.agent_id, self.role)
        inbox_limit = self.max_inbox_size if max_inbox_size is None else int(max_inbox_size)
        inbox = orchestrator.read_inbox(self.agent_id, max_inbox_size=inbox_limit)

        state = public_state or orchestrator.get_public_state()
        price_change = self._price_change_ratio(state, orchestrator)
        risk_signal = self._risk_signal(orchestrator, state)

        should_sleep, sleep_reason = self._should_sleep(
            inbox_messages=inbox,
            price_change_ratio=price_change,
            risk_signal=risk_signal,
            current_tick=orchestrator.current_tick,
            profile=profile,
        )
        if should_sleep:
            self.sleep_count += 1
            orchestrator.log_agent_thought(
                agent_id=self.agent_id,
                role=self.role,
                thought=f"SLEEP: {sleep_reason}",
                speak_payload=None,
                action_payload={"action_type": "SLEEP", "params": {"reason": sleep_reason}},
                audit_status="sleep",
                audit_error=None,
            )
            return {
                "event_ids": [],
                "transaction_ids": [],
                "thought": sleep_reason,
                "inbox_size_used": 0,
                "status": "sleep",
                "used_llm": False,
                "price_change_ratio": str(price_change),
                "risk_signal": str(risk_signal),
            }

        self.wake_count += 1
        self.last_wake_tick = int(orchestrator.current_tick)
        filtered = self.filter_info(inbox)
        self._store_inbox_memories(filtered, orchestrator.current_tick)

        model_output: dict[str, Any]
        used_llm = False
        router_info: dict[str, Any] | None = None

        if self.brain is not None:
            memories = self._recall_memories(
                state=state,
                top_k=profile.attention_policy.memory_top_k,
            )
            decision = self.brain.decide(
                profile=profile,
                public_state=state,
                inbox_messages=filtered,
                recalled_memories=memories,
                allowed_actions=self._allowed_actions(),
            )
            model_output = decision.payload
            used_llm = not decision.used_fallback
            router_info = {
                "backend": decision.backend_used,
                "model": decision.model_used,
                "fallback": decision.used_fallback,
                "error": decision.error,
            }
            self.llm_call_count += 1
        else:
            prompt = self.build_prompt(filtered_messages=filtered, public_state=state)
            model_output = self.cognition(prompt)
            used_llm = self.llm_callable is not None
            if used_llm:
                self.llm_call_count += 1

        audited = auditor_obj.validate_agent_output(model_output)

        emitted_event_ids: list[str] = []
        submitted_tx_ids: list[str] = []
        audit_status = "success"
        audit_error: str | None = None

        try:
            if audited["speak"] is not None:
                auditor_obj.assert_role_permission(self.role, "SPEAK")
                emitted_event_ids.append(
                    orchestrator.submit_event(
                        agent_id=self.agent_id,
                        action_type="SPEAK",
                        params=audited["speak"],
                    )
                )

            action_payload = audited["action"]
            if action_payload is not None:
                auditor_obj.assert_role_permission(self.role, action_payload["action_type"])
                if is_economic_action(action_payload["action_type"]):
                    submitted_tx_ids.append(
                        orchestrator.submit_transaction(
                            agent_id=self.agent_id,
                            action_type=action_payload["action_type"],
                            params=action_payload["params"],
                            gas_price=action_payload["gas_price"],
                        )
                    )
                else:
                    emitted_event_ids.append(
                        orchestrator.submit_event(
                            agent_id=self.agent_id,
                            action_type=action_payload["action_type"],
                            params=action_payload["params"],
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            audit_status = "failed"
            audit_error = str(exc)
            enriched_action = self._enrich_action_payload(audited["action"], router_info)
            orchestrator.log_agent_thought(
                agent_id=self.agent_id,
                role=self.role,
                thought=audited["thought"],
                speak_payload=audited["speak"],
                action_payload=enriched_action,
                audit_status=audit_status,
                audit_error=audit_error,
            )
            raise

        enriched_action = self._enrich_action_payload(audited["action"], router_info)
        orchestrator.log_agent_thought(
            agent_id=self.agent_id,
            role=self.role,
            thought=audited["thought"],
            speak_payload=audited["speak"],
            action_payload=enriched_action,
            audit_status=audit_status,
            audit_error=audit_error,
        )
        return {
            "event_ids": emitted_event_ids,
            "transaction_ids": submitted_tx_ids,
            "thought": audited["thought"],
            "inbox_size_used": len(filtered),
            "status": "wake",
            "used_llm": used_llm,
            "price_change_ratio": str(price_change),
            "risk_signal": str(risk_signal),
            "router": router_info,
        }

    def _allowed_actions(self) -> list[str]:
        return ["SWAP", "UST_TO_LUNA", "LUNA_TO_UST", "SPEAK", "VOTE", "PROPOSE"]

    def _price_change_ratio(
        self,
        public_state: dict[str, Any],
        orchestrator: "Simulation_Orchestrator",
    ) -> Decimal:
        current_price = self._extract_oracle_price(public_state, orchestrator)
        if self.last_oracle_price is None or self.last_oracle_price <= 0:
            self.last_oracle_price = current_price
            return Decimal("0")

        baseline = self.last_oracle_price
        ratio = abs(current_price - baseline) / baseline
        self.last_oracle_price = current_price
        return ratio

    def _extract_oracle_price(
        self,
        public_state: dict[str, Any],
        orchestrator: "Simulation_Orchestrator",
    ) -> Decimal:
        candidates = [
            public_state.get("oracle_price_usdc_per_luna"),
            public_state.get("oracle"),
        ]
        for item in candidates:
            if item is None:
                continue
            try:
                return Decimal(str(item))
            except Exception:  # noqa: BLE001
                continue
        return Decimal(str(orchestrator.engine.get_oracle_price()))

    def _risk_signal(
        self,
        orchestrator: "Simulation_Orchestrator",
        public_state: dict[str, Any],
    ) -> Decimal:
        try:
            ust = Decimal(str(orchestrator.engine.get_account_balance(self.agent_id, "UST")))
            luna = Decimal(str(orchestrator.engine.get_account_balance(self.agent_id, "LUNA")))
            usdc = Decimal(str(orchestrator.engine.get_account_balance(self.agent_id, "USDC")))
        except Exception:  # noqa: BLE001
            return Decimal("0")

        price = self._extract_oracle_price(public_state, orchestrator)
        total_value = ust + usdc + luna * price
        if total_value <= 0:
            return Decimal("0")
        luna_exposure = (luna * price) / total_value
        return max(Decimal("0"), min(Decimal("1"), luna_exposure))

    def _should_sleep(
        self,
        *,
        inbox_messages: list[dict[str, Any]],
        price_change_ratio: Decimal,
        risk_signal: Decimal,
        current_tick: int,
        profile: AgentProfile,
    ) -> tuple[bool, str]:
        if inbox_messages:
            return False, "inbox has new messages"

        if price_change_ratio >= profile.attention_policy.price_change_threshold:
            return False, "price moved above wake threshold"

        risk_limit = min(profile.risk_threshold, profile.attention_policy.risk_wake_threshold)
        if risk_signal >= risk_limit:
            return False, "risk trigger activated"

        if self.last_wake_tick < 0:
            return False, "initial activation"

        if (current_tick - self.last_wake_tick) >= profile.attention_policy.force_wake_interval:
            return False, "periodic wake interval reached"

        return True, "no inbox and low market change"

    def _store_inbox_memories(self, messages: list[dict[str, Any]], tick: int) -> None:
        if self.memory_stream is None:
            return
        for message in messages:
            text = str(message.get("message", "")).strip()
            if not text:
                continue
            channel = str(message.get("channel", "FORUM"))
            metadata = {
                "sender": message.get("sender"),
                "event_id": message.get("event_id"),
                "parent_event_id": message.get("parent_event_id"),
            }
            self.memory_stream.add_memory(
                agent_id=self.agent_id,
                text=text,
                tick=int(tick),
                channel=channel,
                metadata=metadata,
            )

    def _recall_memories(self, state: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
        if self.memory_stream is None:
            return []

        query_parts = [
            str(state.get("oracle_price_usdc_per_luna", "")),
            str(state.get("tick", state.get("current_tick", ""))),
            self.role,
        ]
        query_text = " ".join(part for part in query_parts if part)
        return self.memory_stream.query(
            agent_id=self.agent_id,
            query_text=query_text,
            top_k=max(1, int(top_k)),
            current_tick=int(state.get("tick", state.get("current_tick", 0))),
        )

    def _enrich_action_payload(
        self,
        action_payload: dict[str, Any] | None,
        router_info: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if action_payload is None and router_info is None:
            return None
        payload: dict[str, Any] = {}
        if action_payload is not None:
            payload.update(action_payload)
        if router_info is not None:
            payload["router_info"] = router_info
        return payload


class RetailAgent(BaseAgent):
    def __init__(
        self,
        agent_id: str,
        community_id: str,
        llm_callable: LLMCallable | None = None,
        profile: AgentProfile | None = None,
        brain: LLMBrain | None = None,
        memory_stream: MemoryStream | None = None,
    ):
        super().__init__(
            agent_id=agent_id,
            role="retail",
            community_id=community_id,
            llm_callable=llm_callable,
            profile=profile,
            brain=brain,
            memory_stream=memory_stream,
        )


class WhaleAgent(BaseAgent):
    def __init__(
        self,
        agent_id: str,
        community_id: str,
        llm_callable: LLMCallable | None = None,
        profile: AgentProfile | None = None,
        brain: LLMBrain | None = None,
        memory_stream: MemoryStream | None = None,
    ):
        super().__init__(
            agent_id=agent_id,
            role="whale",
            community_id=community_id,
            llm_callable=llm_callable,
            profile=profile,
            brain=brain,
            memory_stream=memory_stream,
        )


class ProjectAgent(BaseAgent):
    def __init__(
        self,
        agent_id: str,
        community_id: str,
        llm_callable: LLMCallable | None = None,
        profile: AgentProfile | None = None,
        brain: LLMBrain | None = None,
        memory_stream: MemoryStream | None = None,
    ):
        super().__init__(
            agent_id=agent_id,
            role="project",
            community_id=community_id,
            llm_callable=llm_callable,
            profile=profile,
            brain=brain,
            memory_stream=memory_stream,
        )


__all__ = [
    "BaseAgent",
    "RetailAgent",
    "WhaleAgent",
    "ProjectAgent",
]
