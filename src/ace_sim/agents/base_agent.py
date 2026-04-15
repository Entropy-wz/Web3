from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

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

    def filter_info(self, inbox_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(inbox_messages)

    def build_prompt(
        self,
        filtered_messages: list[dict[str, Any]],
        public_state: dict[str, Any] | None = None,
    ) -> str:
        return (
            "你是一个Web3市场参与者。\n"
            f"角色: {self.role}\n"
            f"接收消息: {json.dumps(filtered_messages, ensure_ascii=False)}\n"
            f"公开状态: {json.dumps(public_state or {}, ensure_ascii=False)}\n"
            '请仅返回JSON: {"thought":"...","speak":{...}|null,"action":{...}|null}'
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
            "thought": "市场噪音较大，先保持谨慎并观察资金流向。",
            "speak": {
                "target": "forum",
                "message": "波动加剧，注意风险。",
                "mode": "new",
            },
            "action": None,
        }

    def execute_action(
        self,
        orchestrator: "Simulation_Orchestrator",
        auditor: SecretaryAuditor | None = None,
        public_state: dict[str, Any] | None = None,
        max_inbox_size: int = 5,
    ) -> dict[str, Any]:
        auditor_obj = auditor or orchestrator.secretary
        inbox = orchestrator.read_inbox(self.agent_id, max_inbox_size=max_inbox_size)
        filtered = self.filter_info(inbox)
        prompt = self.build_prompt(filtered_messages=filtered, public_state=public_state)
        model_output = self.cognition(prompt)
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
            orchestrator.log_agent_thought(
                agent_id=self.agent_id,
                role=self.role,
                thought=audited["thought"],
                speak_payload=audited["speak"],
                action_payload=audited["action"],
                audit_status=audit_status,
                audit_error=audit_error,
            )
            raise

        orchestrator.log_agent_thought(
            agent_id=self.agent_id,
            role=self.role,
            thought=audited["thought"],
            speak_payload=audited["speak"],
            action_payload=audited["action"],
            audit_status=audit_status,
            audit_error=audit_error,
        )
        return {
            "event_ids": emitted_event_ids,
            "transaction_ids": submitted_tx_ids,
            "thought": audited["thought"],
            "inbox_size_used": len(filtered),
        }


class RetailAgent(BaseAgent):
    def __init__(self, agent_id: str, community_id: str, llm_callable: LLMCallable | None = None):
        super().__init__(agent_id=agent_id, role="retail", community_id=community_id, llm_callable=llm_callable)


class WhaleAgent(BaseAgent):
    def __init__(self, agent_id: str, community_id: str, llm_callable: LLMCallable | None = None):
        super().__init__(agent_id=agent_id, role="whale", community_id=community_id, llm_callable=llm_callable)


class ProjectAgent(BaseAgent):
    def __init__(self, agent_id: str, community_id: str, llm_callable: LLMCallable | None = None):
        super().__init__(agent_id=agent_id, role="project", community_id=community_id, llm_callable=llm_callable)


__all__ = [
    "BaseAgent",
    "RetailAgent",
    "WhaleAgent",
    "ProjectAgent",
]
