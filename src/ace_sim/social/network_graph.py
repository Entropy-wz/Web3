from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

import networkx as nx


class SocialNetworkGraph:
    """Directed attention graph: sender -> receiver (who can hear whom)."""

    def __init__(self) -> None:
        self.graph = nx.DiGraph()

    def add_agent(self, agent_id: str, role: str, community_id: str) -> None:
        agent = str(agent_id).strip()
        if not agent:
            raise ValueError("agent_id must be non-empty")
        role_norm = str(role).strip().lower()
        if role_norm not in {"retail", "whale", "project"}:
            raise ValueError("role must be retail/whale/project")
        community = str(community_id).strip()
        if not community:
            raise ValueError("community_id must be non-empty")
        self.graph.add_node(
            agent,
            agent_id=agent,
            role=role_norm,
            community_id=community,
        )

    def connect(self, sender: str, receiver: str, weight: float = 1.0) -> None:
        sender_id = str(sender).strip()
        receiver_id = str(receiver).strip()
        if sender_id == receiver_id:
            return
        if sender_id not in self.graph or receiver_id not in self.graph:
            raise ValueError("both sender and receiver must be registered agents")
        self.graph.add_edge(sender_id, receiver_id, weight=float(weight))

    def listeners_of(self, sender: str) -> list[str]:
        sender_id = str(sender).strip()
        if sender_id not in self.graph:
            return []
        return sorted(self.graph.successors(sender_id))

    def all_agents(self) -> list[str]:
        return sorted(self.graph.nodes())

    def is_cross_community(self, sender: str, receiver: str) -> bool:
        sender_id = str(sender).strip()
        receiver_id = str(receiver).strip()
        if sender_id not in self.graph or receiver_id not in self.graph:
            return False
        sender_c = str(self.graph.nodes[sender_id]["community_id"])
        receiver_c = str(self.graph.nodes[receiver_id]["community_id"])
        return sender_c != receiver_c

    def get_agent_meta(self, agent_id: str) -> dict[str, Any]:
        agent = str(agent_id).strip()
        if agent not in self.graph:
            raise ValueError(f"unknown agent in topology: {agent}")
        return dict(self.graph.nodes[agent])

    def build_layered_mixed_topology(self, seed: int = 42) -> None:
        if self.graph.number_of_nodes() <= 1:
            return

        rng = random.Random(seed)
        self.graph.remove_edges_from(list(self.graph.edges()))

        by_community: dict[str, list[str]] = defaultdict(list)
        for agent_id, attrs in self.graph.nodes(data=True):
            by_community[str(attrs["community_id"])].append(str(agent_id))

        for community_id in sorted(by_community):
            members = sorted(by_community[community_id])
            projects = [
                a for a in members if self.graph.nodes[a]["role"] == "project"
            ]
            whales = [a for a in members if self.graph.nodes[a]["role"] == "whale"]
            retails = [a for a in members if self.graph.nodes[a]["role"] == "retail"]
            anchors = projects + whales

            # Project nodes can broadcast to everyone in their community.
            for project in projects:
                for member in members:
                    if member != project:
                        self.connect(project, member)

            # Whales observe/broadcast to key neighbors.
            for whale in whales:
                targets = [m for m in members if m != whale]
                for target in rng.sample(targets, k=min(6, len(targets))):
                    self.connect(whale, target)

            # Retail nodes mainly circulate locally, plus one anchor link.
            for retail in retails:
                local_retail_targets = [r for r in retails if r != retail]
                for target in rng.sample(
                    local_retail_targets, k=min(2, len(local_retail_targets))
                ):
                    self.connect(retail, target)
                if anchors:
                    self.connect(retail, rng.choice(anchors))

        # Sparse cross-community bridges (mostly whales/projects).
        leaders = [
            n
            for n in self.graph.nodes()
            if self.graph.nodes[n]["role"] in {"whale", "project"}
        ]
        for leader in sorted(leaders):
            leader_community = str(self.graph.nodes[leader]["community_id"])
            external_targets = [
                n
                for n in self.graph.nodes()
                if n != leader
                and str(self.graph.nodes[n]["community_id"]) != leader_community
            ]
            for target in rng.sample(
                external_targets, k=min(2, len(external_targets))
            ):
                self.connect(leader, target)

        # Ensure every node has at least one outgoing listener if possible.
        all_agents = self.all_agents()
        for agent in all_agents:
            if self.graph.out_degree(agent) == 0 and len(all_agents) > 1:
                fallback = rng.choice([a for a in all_agents if a != agent])
                self.connect(agent, fallback)


__all__ = ["SocialNetworkGraph"]
