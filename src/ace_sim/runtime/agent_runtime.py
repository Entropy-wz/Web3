from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agents.base_agent import BaseAgent
from ..execution.orchestrator.time_orchestrator import Simulation_Orchestrator, TickSettlementReport


@dataclass
class AgentDecisionOutcome:
    agent_id: str
    status: str
    used_llm: bool
    result: dict[str, Any]
    error: str | None = None


@dataclass
class RuntimeTickReport:
    tick: int
    agent_outcomes: list[AgentDecisionOutcome]
    settlement: TickSettlementReport
    llm_calls: int
    sleeping_agents: int
    llm_saved_ratio: float


class AgentRuntime:
    """Coordinates agent cognition phase before orchestrator economic settlement."""

    def __init__(
        self,
        orchestrator: Simulation_Orchestrator,
        agents: list[BaseAgent] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self._agents: dict[str, BaseAgent] = {}
        for agent in agents or []:
            self.register_agent(agent)

    def register_agent(self, agent: BaseAgent) -> None:
        self._agents[agent.agent_id] = agent

    def list_agents(self) -> list[str]:
        return sorted(self._agents.keys())

    def run_tick(self, max_inbox_size: int = 5) -> RuntimeTickReport:
        public_state = self.orchestrator.get_public_state()
        outcomes: list[AgentDecisionOutcome] = []
        llm_calls = 0
        sleeping = 0

        for agent_id in self.list_agents():
            agent = self._agents[agent_id]
            try:
                result = agent.execute_action(
                    orchestrator=self.orchestrator,
                    public_state=public_state,
                    max_inbox_size=max_inbox_size,
                )
                status = str(result.get("status", "wake"))
                used_llm = bool(result.get("used_llm", False))
                if used_llm:
                    llm_calls += 1
                if status == "sleep":
                    sleeping += 1
                outcomes.append(
                    AgentDecisionOutcome(
                        agent_id=agent.agent_id,
                        status=status,
                        used_llm=used_llm,
                        result=result,
                        error=None,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                outcomes.append(
                    AgentDecisionOutcome(
                        agent_id=agent.agent_id,
                        status="error",
                        used_llm=False,
                        result={},
                        error=str(exc),
                    )
                )

        settlement = self.orchestrator.step_tick()
        total_agents = len(self._agents)
        saved_ratio = 0.0 if total_agents == 0 else sleeping / total_agents

        return RuntimeTickReport(
            tick=self.orchestrator.current_tick,
            agent_outcomes=outcomes,
            settlement=settlement,
            llm_calls=llm_calls,
            sleeping_agents=sleeping,
            llm_saved_ratio=saved_ratio,
        )


__all__ = [
    "AgentRuntime",
    "RuntimeTickReport",
    "AgentDecisionOutcome",
]
