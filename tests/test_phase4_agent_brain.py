from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ace_sim.agents.agent_profile import AgentProfile, default_agent_profile
from ace_sim.agents.base_agent import RetailAgent
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
