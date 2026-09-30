"""Network graph — tracks follower/following relationships between users.

Uses store_sqlite graph_edges table. Updates on enrich and action execution.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .store_sqlite import GitHubStore


class NetworkGraph:
    """Builds and queries a follower/following network graph."""

    def __init__(self, store: GitHubStore):
        self.store = store

    def record_follow(self, follower: str, followee: str) -> None:
        """Record that `follower` follows `followee`."""
        self.store.upsert_edge(follower, followee, "follows", 1.0)

    def record_contribution(self, contributor: str, repo_full_name: str, weight: float = 1.0) -> None:
        """Record that `contributor` contributed to `repo_full_name`."""
        self.store.upsert_edge(contributor, repo_full_name, "contributed_to", weight)

    def get_mutual_followers(self, username: str, candidates: list[str]) -> set[str]:
        """Find which candidates also follow `username` (mutual follows)."""
        followers_of_user = set(self.store.get_followers(username))
        return set(candidates) & followers_of_user

    def network_score(self, username: str, candidates: list[str]) -> float:
        """Compute network overlap score for a candidate.

        Higher score = more mutual connections with known good users.
        """
        if not candidates:
            return 0.5  # neutral when no context

        # How many of the candidate's known connections are in the candidate list
        following = set(self.store.get_following(username))
        followers = set(self.store.get_followers(username))

        overlap = (following & set(candidates)) | (followers & set(candidates))
        if not overlap:
            return 0.5

        return min(1.0, len(overlap) / max(len(candidates), 1))

    def build_subgraph(self, usernames: list[str], depth: int = 2) -> dict[str, Any]:
        """Build a subgraph around a set of users.

        Returns nodes and edges within `depth` hops.
        """
        nodes: set[str] = set(usernames)
        edges: list[tuple[str, str, str]] = []

        # Collect all edges touching the seed set
        conn = self.store.conn
        placeholders = ",".join("?" for _ in usernames)

        # Direct edges (following)
        rows = conn.execute(f"""
            SELECT source, target FROM graph_edges
            WHERE edge_type = 'follows'
              AND (source IN ({placeholders}) OR target IN ({placeholders}))
        """, (*usernames, *usernames)).fetchall()

        for r in rows:
            edges.append((r["source"], r["target"], "follows"))
            if depth > 1:
                nodes.add(r["source"])
                nodes.add(r["target"])

        # Contribution edges
        rows = conn.execute(f"""
            SELECT source, target FROM graph_edges
            WHERE edge_type = 'contributed_to'
              AND source IN ({placeholders})
        """, tuple(usernames)).fetchall()

        for r in rows:
            edges.append((r["source"], r["target"], "contributed_to"))
            nodes.add(r["target"])

        # BFS for deeper levels (simplified)
        current = list(nodes)
        for _ in range(depth - 1):
            new_nodes = set()
            for node in current:
                # Following edges
                f_rows = conn.execute(
                    "SELECT target FROM graph_edges WHERE source = ? AND edge_type = 'follows'",
                    (node,)
                ).fetchall()
                for r in f_rows:
                    new_nodes.add(r["target"])
                # Follower edges
                rev_rows = conn.execute(
                    "SELECT source FROM graph_edges WHERE target = ? AND edge_type = 'follows'",
                    (node,)
                ).fetchall()
                for r in rev_rows:
                    new_nodes.add(r["source"])
            if not new_nodes - nodes:
                break
            nodes |= new_nodes
            current = list(new_nodes)

        return {
            "nodes": list(nodes),
            "edges": edges,
        }

    def clustering_coefficient(self, username: str) -> float:
        """Compute local clustering coefficient for a user.

        Ratio of actual connections between a user's neighbors to possible connections.
        """
        following = set(self.store.get_following(username))
        followers = set(self.store.get_followers(username))
        neighbors = following | followers

        if len(neighbors) < 2:
            return 0.0

        # Count how many neighbors are connected to each other
        edges_between = 0
        for a in neighbors:
            a_following = set(self.store.get_following(a))
            edges_between += len(a_following & neighbors)

        possible = len(neighbors) * (len(neighbors) - 1)
        return min(1.0, edges_between / max(possible, 1))
