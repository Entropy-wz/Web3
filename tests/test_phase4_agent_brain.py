from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from ace_sim.agents.agent_profile import AgentProfile, default_agent_profile
from ace_sim.agents.base_agent import RetailAgent
from ace_sim.cognition.llm_brain import LLMBrain
from ace_sim.cognition.llm_router import LLMRouter
from ace_sim.cognition.memory_stream import MemoryStream
from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator
from ace_sim.runtime.agent_runtime import AgentRuntime
from ace_sim.social.network_graph import SocialNetworkGraph


@dataclass
class _FlakyAdapter:
    failures_before_success: int

    def __post_init__(self) -> None:
        self.calls = 0

    def generate(self, *, model: str, prompt: str, timeout: float, schema=None):
        del model, prompt, timeout, schema
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise RuntimeError("429 rate limit")
        return {"thought": "ok", "speak": None, "action": None}


@dataclass
class _AlwaysFailAdapter:
    def __post_init__(self) -> None:
        self.calls = 0

    def generate(self, *, model: str, prompt: str, timeout: float, schema=None):
        del model, prompt, timeout, schema
        self.calls += 1
        raise RuntimeError("503 service unavailable")


@dataclass
class _MalformedAdapter:
    def __post_init__(self) -> None:
        self.calls = 0

    def generate(self, *, model: str, prompt: str, timeout: float, schema=None):
        del model, prompt, timeout, schema
        self.calls += 1
        return {
            "thought": "panic rising",
            "speak": "please be careful",
            "action": "SWAP",
        }


@dataclass
class _MalformedThenValidAdapter:
    def __post_init__(self) -> None:
        self.calls = 0

    def generate(self, *, model: str, prompt: str, timeout: float, schema=None):
        del model, prompt, timeout, schema
        self.calls += 1
        if self.calls == 1:
            return {
                "thought": "panic rising",
                "speak": "is this safe?",
                "action": "SWAP",
            }
        return {
            "thought": "recovered format",
            "speak": {"target": "forum", "message": "stay calm", "mode": "new"},
            "action": None,
        }


def _stack(tmp_path):
    db_path = tmp_path / "phase4.sqlite3"
    engine = ACE_Engine(db_path=db_path)
    orchestrator = Simulation_Orchestrator(engine=engine)
    return engine, orchestrator


def test_default_profile_routes_by_role():
    whale = default_agent_profile("w1", "whale")
    retail = default_agent_profile("r1", "retail")
    project = default_agent_profile("p1", "project")

    assert whale.llm_model == "gpt-4o"
    assert retail.llm_model == "gpt-4o-mini"
    assert project.llm_model == "gpt-4o-mini"


def test_sleep_wake_skips_redundant_llm_calls(tmp_path):
    engine, orchestrator = _stack(tmp_path)
    orchestrator.register_agent("alice", role="retail", community_id="c1")
    engine.create_account("alice", ust="100", usdc="100")

    calls = {"count": 0}

    def llm(_prompt: str):
        calls["count"] += 1
        return {"thought": "hold", "speak": None, "action": None}

    agent = RetailAgent("alice", "c1", llm_callable=llm)

    first = agent.execute_action(orchestrator)
    second = agent.execute_action(orchestrator)

    assert first["status"] == "wake"
    assert second["status"] == "sleep"
    assert calls["count"] == 1


def test_router_retry_then_success_and_fallback():
    router = LLMRouter(max_retries=2, base_backoff_seconds=0.001, jitter_seconds=0.0)
    profile = AgentProfile(
        agent_id="whale1",
        role="whale",
        llm_backend="custom",
        llm_model="x",
        risk_threshold=Decimal("0.5"),
    )

    flaky = _FlakyAdapter(failures_before_success=1)
    router.register_adapter("custom", flaky)
    ok = router.route(profile, prompt="panic", timeout=1.0)
    assert ok.used_fallback is False
    assert flaky.calls == 2

    broken = _AlwaysFailAdapter()
    router.register_adapter("custom", broken)
    degraded = router.route(profile, prompt="panic", timeout=1.0)
    assert degraded.used_fallback is True
    assert degraded.backend_used == "fallback"


def test_router_auto_repairs_common_shape_errors(monkeypatch):
    monkeypatch.setenv("ACE_LLM_AUTO_REPAIR_FORMAT", "1")
    monkeypatch.setenv("ACE_LLM_FORMAT_RETRY_ONCE", "0")

    router = LLMRouter(max_retries=0, base_backoff_seconds=0.001, jitter_seconds=0.0)
    profile = AgentProfile(
        agent_id="retail_bad_shape",
        role="retail",
        llm_backend="custom",
        llm_model="mini",
        risk_threshold=Decimal("0.5"),
    )
    adapter = _MalformedAdapter()
    router.register_adapter("custom", adapter)

    result = router.route(profile, prompt="panic", timeout=1.0)

    assert result.used_fallback is False
    assert result.decision["speak"] == {
        "target": "forum",
        "message": "please be careful",
        "mode": "new",
    }
    assert result.decision["action"] is None
    assert adapter.calls == 1


def test_router_format_retry_recovers_when_repair_disabled(monkeypatch):
    monkeypatch.setenv("ACE_LLM_AUTO_REPAIR_FORMAT", "0")
    monkeypatch.setenv("ACE_LLM_FORMAT_RETRY_ONCE", "1")

    router = LLMRouter(max_retries=0, base_backoff_seconds=0.001, jitter_seconds=0.0)
    profile = AgentProfile(
        agent_id="retail_retry",
        role="retail",
        llm_backend="custom",
        llm_model="mini",
        risk_threshold=Decimal("0.5"),
    )
    adapter = _MalformedThenValidAdapter()
    router.register_adapter("custom", adapter)

    result = router.route(profile, prompt="panic", timeout=1.0)

    assert result.used_fallback is False
    assert result.decision["thought"] == "recovered format"
    assert adapter.calls == 2


def test_panic_prompt_has_hard_output_contract():
    brain = LLMBrain()
    profile = AgentProfile(
        agent_id="retail_panic_zz",
        role="retail",
        llm_backend="openai",
        llm_model="gpt-4o-mini",
        risk_threshold=Decimal("0.3"),
        persona_type="retail_panic_prone",
        hidden_goals=["preserve principal"],
    )
    prompt = brain.build_prompt(
        profile=profile,
        public_state={"tick": 1, "oracle_price_usdc_per_luna": "1.0"},
        inbox_messages=[],
        recalled_memories=[],
        allowed_actions=["SWAP", "SPEAK"],
    )

    assert "Output contract (must follow exactly):" in prompt
    assert "Never output speak as plain string." in prompt
    assert "Never output action as plain string." in prompt
    assert "Panic persona strictness override:" in prompt


def test_memory_stream_local_embedding_and_query(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    memory = MemoryStream(db_path=db_path)

    memory.add_memory(
        agent_id="alice",
        text="UST depeg rumor spread quickly",
        tick=1,
        channel="PUBLIC_CHANNEL",
        metadata={"source": "forum"},
        price_shock=0.12,
        risk_relevance=0.7,
    )
    memory.add_memory(
        agent_id="alice",
        text="Project announced reserve support",
        tick=2,
        channel="SYSTEM_NEWS",
        metadata={"source": "system"},
        price_shock=0.02,
        risk_relevance=0.3,
    )

    hits = memory.query(
        agent_id="alice",
        query_text="depeg panic",
        top_k=2,
        current_tick=3,
        price_shock=0.15,
        risk_relevance=0.8,
    )

    memory.close()

    assert len(hits) >= 1
    assert all("text" in hit for hit in hits)


def test_public_channel_distance_decay_and_private_clean_delivery(tmp_path):
    _, orchestrator = _stack(tmp_path)
    orchestrator.register_agent("a", role="retail", community_id="c1")
    orchestrator.register_agent("b", role="retail", community_id="c1")
    orchestrator.register_agent("c", role="retail", community_id="c2")

    orchestrator.connect_agents("a", "b")
    orchestrator.connect_agents("b", "c")

    orchestrator.submit_event(
        "a",
        "SPEAK",
        {
            "target": "public",
            "channel": "PUBLIC_CHANNEL",
            "message": "UST 0.99 near depeg",
            "mode": "new",
        },
    )

    inbox_b = orchestrator.read_inbox("b")
    inbox_c_now = orchestrator.read_inbox("c")
    assert len(inbox_b) == 1
    assert inbox_c_now == []

    orchestrator.step_tick()
    inbox_c = orchestrator.read_inbox("c")
    assert len(inbox_c) == 1
    assert "[PRICE_SHOCK]" in inbox_c[0]["message"]

    orchestrator.submit_event(
        "a",
        "SPEAK",
        {
            "target": "private",
            "channel": "PRIVATE_CHANNEL",
            "receiver": "c",
            "message": "UST 0.95 privately discussed",
            "mode": "new",
        },
    )
    direct = orchestrator.read_inbox("c")
    assert len(direct) == 1
    assert "0.95" in direct[0]["message"]


def test_agent_runtime_reports_sleep_saving(tmp_path):
    engine, orchestrator = _stack(tmp_path)
    for agent_id in ["a1", "a2"]:
        orchestrator.register_agent(agent_id, role="retail", community_id="c1")
        engine.create_account(agent_id, ust="100", usdc="100")

    def llm(_prompt: str):
        return {"thought": "hold", "speak": None, "action": None}

    runtime = AgentRuntime(
        orchestrator=orchestrator,
        agents=[
            RetailAgent("a1", "c1", llm_callable=llm),
            RetailAgent("a2", "c1", llm_callable=llm),
        ],
    )

    first = runtime.run_tick()
    second = runtime.run_tick()

    assert first.sleeping_agents == 0
    assert second.sleeping_agents == 2
    assert second.llm_saved_ratio == 1.0


def test_scale_free_topology_builder_creates_hub_structure():
    topo = SocialNetworkGraph()
    for i in range(24):
        role = "retail"
        if i in {0, 1}:
            role = "whale"
        if i == 2:
            role = "project"
        topo.add_agent(f"agent_{i}", role=role, community_id=f"c{i % 3}")

    topo.build_scale_free_topology(seed=42, m=2)
    out_degrees = sorted([topo.graph.out_degree(n) for n in topo.graph.nodes()])

    assert topo.graph.number_of_edges() >= 24
    assert out_degrees[-1] >= max(3, out_degrees[len(out_degrees) // 2])
