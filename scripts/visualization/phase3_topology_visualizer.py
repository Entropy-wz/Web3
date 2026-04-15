from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ace_sim.engine.ace_engine import ACE_Engine
from ace_sim.execution.orchestrator.time_orchestrator import Simulation_Orchestrator
from ace_sim.social.perception_filter import PerceptionFilter


def create_agents(orchestrator: Simulation_Orchestrator, communities: int, retail_per_community: int) -> list[str]:
    agent_ids: list[str] = []
    for c_idx in range(communities):
        project = f"project_{c_idx}"
        whale = f"whale_{c_idx}"
        orchestrator.register_agent(project, role="project", community_id=f"c{c_idx}")
        orchestrator.register_agent(whale, role="whale", community_id=f"c{c_idx}")
        agent_ids.extend([project, whale])
        for r_idx in range(retail_per_community):
            retail = f"retail_{c_idx}_{r_idx}"
            orchestrator.register_agent(retail, role="retail", community_id=f"c{c_idx}")
            agent_ids.append(retail)
    orchestrator.build_social_topology(seed=19)
    return agent_ids


def seed_accounts(engine: ACE_Engine, agent_ids: list[str]) -> None:
    for agent_id in agent_ids:
        if agent_id.startswith("whale"):
            engine.create_account(agent_id, ust="80000", luna="5000", usdc="80000")
        elif agent_id.startswith("project"):
            engine.create_account(agent_id, ust="50000", luna="2000", usdc="50000")
        else:
            engine.create_account(agent_id, ust="6000", luna="300", usdc="6000")


def run_simulation(
    db_path: Path,
    ticks: int,
    communities: int,
    retail_per_community: int,
    seed: int,
) -> dict[str, Any]:
    random.seed(seed)
    if db_path.exists():
        db_path.unlink()

    engine = ACE_Engine(db_path=db_path)
    orchestrator = Simulation_Orchestrator(
        engine=engine,
        perception_filter=PerceptionFilter(seed=seed),
    )
    agent_ids = create_agents(orchestrator, communities=communities, retail_per_community=retail_per_community)
    seed_accounts(engine, agent_ids)

    focus_target = "retail_0_0"
    for sender in agent_ids:
        if sender != focus_target:
            orchestrator.connect_agents(sender, focus_target)

    previous_event_id: str | None = None
    for tick in range(ticks):
        # Heavy forum chatter to trigger cognitive overload for focus_target.
        noisy_senders = random.sample([a for a in agent_ids if a != focus_target], k=min(10, len(agent_ids) - 1))
        for sender in noisy_senders:
            event_id = orchestrator.submit_event(
                sender,
                "SPEAK",
                {
                    "target": "forum",
                    "message": f"UST {0.9 - 0.001 * tick:.3f}, dump {1000 + tick}",
                    "mode": "new",
                },
            )
            previous_event_id = event_id

        if tick % 5 == 0 and previous_event_id:
            relay_sender = random.choice([a for a in agent_ids if a != focus_target])
            orchestrator.submit_event(
                relay_sender,
                "SPEAK",
                {
                    "target": "forum",
                    "message": f"relay rumor tick={tick}",
                    "mode": "relay",
                    "parent_event_id": previous_event_id,
                },
            )

        for whale in [a for a in agent_ids if a.startswith("whale_")]:
            orchestrator.submit_transaction(
                whale,
                "SWAP",
                {
                    "pool_name": "Pool_A",
                    "token_in": "UST",
                    "amount": str(random.randint(100, 800)),
                    "slippage_tolerance": "0.2",
                },
                gas_price=str(random.randint(5, 20)),
            )

        orchestrator.step_tick()

        # Read all inboxes to materialize overload logs per tick.
        for agent in agent_ids:
            orchestrator.read_inbox(agent, max_inbox_size=5)

    engine.check_global_invariants()
    orchestrator.close()
    engine.close()

    return {
        "focus_target": focus_target,
        "agent_ids": agent_ids,
    }


def query_table(db_path: Path, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def plot_topology(orchestrator_graph: nx.DiGraph, output_path: Path) -> None:
    plt.figure(figsize=(11, 8))
    communities = {n: orchestrator_graph.nodes[n]["community_id"] for n in orchestrator_graph.nodes()}
    unique_communities = sorted(set(communities.values()))
    color_map = {c: idx for idx, c in enumerate(unique_communities)}
    node_colors = [color_map[communities[n]] for n in orchestrator_graph.nodes()]
    pos = nx.spring_layout(orchestrator_graph, seed=15, k=0.35)
    nx.draw_networkx_nodes(orchestrator_graph, pos, node_color=node_colors, cmap=plt.cm.Set2, node_size=220)
    nx.draw_networkx_edges(orchestrator_graph, pos, alpha=0.25, arrows=False, width=0.8)
    nx.draw_networkx_labels(orchestrator_graph, pos, font_size=6)
    plt.title("Phase 3 Social Topology")
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close()


def plot_timeline(delivery_rows: list[sqlite3.Row], output_path: Path) -> None:
    if not delivery_rows:
        return
    emit_ticks = [int(r["emit_tick"]) for r in delivery_rows]
    deliver_ticks = [int(r["deliver_tick"]) for r in delivery_rows]
    delays = [d - e for e, d in zip(emit_ticks, deliver_ticks)]

    plt.figure(figsize=(10, 6))
    scatter = plt.scatter(emit_ticks, deliver_ticks, c=delays, cmap="viridis", alpha=0.55, s=12)
    max_tick = max(deliver_ticks) if deliver_ticks else 1
    plt.plot([0, max_tick], [0, max_tick], linestyle="--", linewidth=1.0, color="#444444")
    plt.colorbar(scatter, label="Delay (ticks)")
    plt.xlabel("Emit Tick")
    plt.ylabel("Deliver Tick")
    plt.title("Semantic Message Timeline")
    plt.grid(alpha=0.25)
    plt.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close()


def plot_cascade_tree(delivery_rows: list[sqlite3.Row], output_path: Path) -> None:
    event_parent: dict[str, str | None] = {}
    for row in delivery_rows:
        event_id = str(row["event_id"])
        parent_event_id = row["parent_event_id"]
        if event_id not in event_parent:
            event_parent[event_id] = parent_event_id

    if not event_parent:
        return

    graph = nx.DiGraph()
    for event_id, parent_event_id in event_parent.items():
        graph.add_node(event_id)
        if parent_event_id:
            graph.add_edge(parent_event_id, event_id)

    if graph.number_of_nodes() == 0:
        return

    plt.figure(figsize=(11, 8))
    roots = [n for n in graph.nodes() if graph.in_degree(n) == 0]
    root_order = {node: idx for idx, node in enumerate(sorted(roots))}
    node_colors = []
    for n in graph.nodes():
        if n in root_order:
            node_colors.append(root_order[n])
        else:
            preds = list(graph.predecessors(n))
            parent = preds[0] if preds else None
            node_colors.append(root_order.get(parent, 0))

    pos = nx.spring_layout(graph, seed=20, k=0.7)
    nx.draw_networkx_nodes(graph, pos, node_color=node_colors, cmap=plt.cm.tab20, node_size=260)
    nx.draw_networkx_edges(graph, pos, arrows=True, width=0.9, alpha=0.4)
    nx.draw_networkx_labels(graph, pos, font_size=6)
    plt.title("Information Cascade Tree (event_id lineage)")
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close()


def plot_overload_heatmap(overload_rows: list[sqlite3.Row], output_path: Path) -> None:
    if not overload_rows:
        return

    agents = sorted({str(r["agent_id"]) for r in overload_rows})
    ticks = sorted({int(r["tick"]) for r in overload_rows})
    agent_index = {a: idx for idx, a in enumerate(agents)}
    tick_index = {t: idx for idx, t in enumerate(ticks)}

    grid = np.zeros((len(agents), len(ticks)), dtype=float)
    for row in overload_rows:
        a_idx = agent_index[str(row["agent_id"])]
        t_idx = tick_index[int(row["tick"])]
        grid[a_idx, t_idx] += float(row["dropped_count"])

    plt.figure(figsize=(12, max(4, len(agents) * 0.25)))
    plt.imshow(grid, aspect="auto", interpolation="nearest", cmap="magma")
    plt.colorbar(label="Dropped Messages")
    plt.yticks(range(len(agents)), agents, fontsize=6)
    plt.xticks(range(len(ticks)), ticks, fontsize=7)
    plt.xlabel("Tick")
    plt.ylabel("Agent")
    plt.title("Cognitive Overload Heatmap")
    plt.tight_layout()
    plt.savefig(output_path, dpi=180)
    plt.close()


def write_summary(
    summary_path: Path,
    delivery_rows: list[sqlite3.Row],
    overload_rows: list[sqlite3.Row],
    metadata: dict[str, Any],
) -> None:
    total_deliveries = len(delivery_rows)
    delayed_deliveries = sum(
        1 for row in delivery_rows if int(row["deliver_tick"]) > int(row["emit_tick"])
    )
    total_dropped = int(sum(int(row["dropped_count"]) for row in overload_rows))

    by_transform: dict[str, int] = defaultdict(int)
    for row in delivery_rows:
        by_transform[str(row["transform_tag"])] += 1

    summary = {
        "total_deliveries": total_deliveries,
        "delayed_deliveries": delayed_deliveries,
        "delayed_ratio": (delayed_deliveries / total_deliveries) if total_deliveries else 0,
        "total_overload_dropped": total_dropped,
        "transform_breakdown": dict(sorted(by_transform.items())),
        "focus_target": metadata["focus_target"],
        "agents": len(metadata["agent_ids"]),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase3 topology and channel visualizer")
    parser.add_argument("--ticks", type=int, default=50)
    parser.add_argument("--communities", type=int, default=3)
    parser.add_argument("--retail-per-community", type=int, default=8)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--output-dir", type=str, default="artifacts/phase3")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "phase3_trace.sqlite3"

    metadata = run_simulation(
        db_path=db_path,
        ticks=args.ticks,
        communities=args.communities,
        retail_per_community=args.retail_per_community,
        seed=args.seed,
    )

    # Rebuild orchestrator only for reading topology layout deterministically.
    engine = ACE_Engine(db_path=db_path)
    orchestrator = Simulation_Orchestrator(engine=engine, perception_filter=PerceptionFilter(seed=args.seed))
    create_agents(
        orchestrator,
        communities=args.communities,
        retail_per_community=args.retail_per_community,
    )
    topology_path = output_dir / "topology.png"
    plot_topology(orchestrator.topology.graph, topology_path)
    orchestrator.close()
    engine.close()

    delivery_rows = query_table(
        db_path,
        """
        SELECT event_id, parent_event_id, sender, receiver, channel, emit_tick, deliver_tick, transform_tag
        FROM semantic_delivery_log
        ORDER BY id ASC
        """,
    )
    overload_rows = query_table(
        db_path,
        """
        SELECT agent_id, tick, dropped_count
        FROM inbox_overload_log
        ORDER BY id ASC
        """,
    )

    timeline_path = output_dir / "message_timeline.png"
    cascade_path = output_dir / "cascade_tree.png"
    overload_path = output_dir / "overload_heatmap.png"
    summary_path = output_dir / "phase3_summary.json"

    plot_timeline(delivery_rows, timeline_path)
    plot_cascade_tree(delivery_rows, cascade_path)
    plot_overload_heatmap(overload_rows, overload_path)
    write_summary(summary_path, delivery_rows, overload_rows, metadata=metadata)

    print(f"db: {db_path}")
    print(f"topology: {topology_path}")
    print(f"timeline: {timeline_path}")
    print(f"cascade: {cascade_path}")
    print(f"overload: {overload_path}")
    print(f"summary: {summary_path}")


if __name__ == "__main__":
    main()
