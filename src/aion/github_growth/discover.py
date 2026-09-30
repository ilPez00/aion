"""Seed discovery — find GitHub users to consider for engagement.

Seeds come from two sources:
1. Repository contributors to configured seed repos (physis-pro, aion, etc.)
2. Repository dependents/forks of seed repos
3. User-provided seeds (explicit list)
4. GitHub search queries
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .api import GitHubAPI
from .config import GrowthConfig


@dataclass
class SeedCandidate:
    """A discovered user candidate with context on how they were found."""
    username: str
    source_repo: str | None = None
    source_type: str = "contributor"  # contributor, dependent, user_provided, search
    context: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.username)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SeedCandidate):
            return self.username == other.username
        return False


class SeedDiscoverer:
    """Discovers seed candidates from configured repos and user input."""

    def __init__(self, api: GitHubAPI, config: GrowthConfig | None = None):
        self.api = api
        self.config = config or GrowthConfig.from_env()

    def discover_from_repo(self, owner: str, repo: str) -> list[SeedCandidate]:
        """Discover contributors and forks of a repository."""
        full_name = f"{owner}/{repo}"
        candidates: list[SeedCandidate] = []
        seen: set[str] = set()

        # Contributors
        contributors = self.api.list_repo_contributors(owner, repo)
        for c in contributors:
            login = c.get("login")
            if not login or login in seen:
                continue
            if self._should_exclude(login):
                continue
            seen.add(login)
            candidates.append(SeedCandidate(
                username=login,
                source_repo=full_name,
                source_type="contributor",
                context={"contributions": c.get("contributions", 0)},
            ))

        # Dependents (forks)
        forks = self.api.list_repo_dependents(owner, repo)
        for f in forks:
            owner_login = f.get("owner", {}).get("login")
            if not owner_login or owner_login in seen:
                continue
            if self._should_exclude(owner_login):
                continue
            seen.add(owner_login)
            candidates.append(SeedCandidate(
                username=owner_login,
                source_repo=full_name,
                source_type="dependent",
                context={"fork_name": f.get("name", "")},
            ))

        return candidates

    def discover_from_seeds(self) -> list[SeedCandidate]:
        """Run discovery across all configured seed repos."""
        all_candidates: list[SeedCandidate] = []
        seen: set[str] = set()

        seed_repos = [(self.config.seed_repo_owner, self.config.seed_repo_name)]
        for extra in self.config.extra_seed_repos:
            parts = extra.split("/")
            if len(parts) == 2:
                seed_repos.append((parts[0], parts[1]))

        for owner, repo in seed_repos:
            candidates = self.discover_from_repo(owner, repo)
            for c in candidates:
                if c.username not in seen:
                    seen.add(c.username)
                    all_candidates.append(c)

        return all_candidates

    def discover_from_users(self, usernames: list[str]) -> list[SeedCandidate]:
        """Wrap explicit user-provided seeds."""
        candidates = []
        for u in usernames:
            if not self._should_exclude(u):
                candidates.append(SeedCandidate(
                    username=u,
                    source_type="user_provided",
                ))
        return candidates

    def discover_from_search(self, query: str, limit: int = 20) -> list[SeedCandidate]:
        """Search GitHub users by query."""
        results = self.api.search_users(query, limit=limit)
        candidates = []
        seen = set()
        for r in results:
            login = r.get("login")
            if not login or login in seen or self._should_exclude(login):
                continue
            seen.add(login)
            candidates.append(SeedCandidate(
                username=login,
                source_type="search",
                context={"search_query": query},
            ))
        return candidates

    def _should_exclude(self, username: str) -> bool:
        """Check against blocklist, bots, and orgs."""
        if self.config.exclude_bots and username.endswith("[bot]"):
            return True
        if username in self.config.exclude_orgs:
            return True
        if username in self.config.blocklist:
            return True
        if self.config.allowlist and username not in self.config.allowlist:
            return True
        return False
