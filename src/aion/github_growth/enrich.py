"""Profile enrichment — fetch full profile + repos + network for a user.

Respects config limits on repos fetched, followers/following fetched.
All results stored via GitHubStore.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .api import GitHubAPI
from .config import GrowthConfig
from .store_sqlite import GitHubStore


@dataclass
class EnrichedProfile:
    """A fully enriched user profile."""
    username: str
    profile: dict[str, Any]
    repos: list[dict[str, Any]] = field(default_factory=list)
    followers: list[str] = field(default_factory=list)
    following: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "profile": self.profile,
            "repos": self.repos,
            "followers": self.followers,
            "following": self.following,
            "fetched_at": self.fetched_at,
        }


class ProfileEnricher:
    """Enriches seed candidates with full GitHub profile data."""

    def __init__(self, api: GitHubAPI, store: GitHubStore, config: GrowthConfig | None = None):
        self.api = api
        self.store = store
        self.config = config or GrowthConfig.from_env()

    def enrich(self, username: str) -> EnrichedProfile | None:
        """Fetch and store full profile for a user. Returns None if not found."""
        # Check if we have a recent profile
        existing = self.store.get_user(username)
        if existing:
            fetched_at = existing.get("fetched_at", "")
            if fetched_at:
                try:
                    dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                    if age_hours < 24:
                        return self._reconstruct(existing)
                except (ValueError, TypeError):
                    pass

        # Fetch fresh data
        profile = self.api.get_user(username)
        if profile is None:
            return None

        self.store.upsert_user(profile)

        # Fetch repos if configured
        repos: list[dict[str, Any]] = []
        if self.config.fetch_repos:
            repos = self.api.list_user_repos(username, self.config.max_repos_per_user)
            for r in repos:
                self.store.upsert_repo(r)

        # Fetch followers if configured
        followers: list[str] = []
        if self.config.fetch_followers:
            followers = self._fetch_followers(username)
            for f in followers:
                self.store.upsert_edge(f, username, "follows", 1.0)

        # Fetch following if configured (expensive, off by default)
        following: list[str] = []
        if self.config.fetch_following:
            following = self._fetch_following(username)
            for f in following:
                self.store.upsert_edge(username, f, "follows", 1.0)

        return EnrichedProfile(
            username=username,
            profile=profile,
            repos=repos,
            followers=followers,
            following=following,
        )

    def enrich_batch(self, usernames: list[str]) -> list[EnrichedProfile]:
        """Enrich multiple users. Skips existing recent profiles."""
        results = []
        for u in usernames:
            ep = self.enrich(u)
            if ep:
                results.append(ep)
        return results

    def _fetch_followers(self, username: str) -> list[str]:
        """Fetch follower usernames (paginated, limited)."""
        self.api._respect_interval()
        import requests
        headers = self.api._headers()
        followers = []
        api_base = self.config.api_base
        for page in range(1, 6):  # max 5 pages * 50 = 250
            if len(followers) >= self.config.max_followers_fetch:
                break
            resp = requests.get(
                f"{api_base}/users/{username}/followers",
                params={"per_page": 50, "page": page},
                headers=headers,
            )
            self.api._check_rate_limit(resp)
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data:
                break
            followers.extend(f["login"] for f in data if "login" in f)
        return followers[:self.config.max_followers_fetch]

    def _fetch_following(self, username: str) -> list[str]:
        """Fetch following usernames (limited)."""
        self.api._respect_interval()
        import requests
        headers = self.api._headers()
        api_base = self.config.api_base
        following = []
        resp = requests.get(
            f"{api_base}/users/{username}/following",
            params={"per_page": 50},
            headers=headers,
        )
        self.api._check_rate_limit(resp)
        if resp.status_code == 200:
            following.extend(f["login"] for f in resp.json() if "login" in f)
        return following[:50]

    def _reconstruct(self, stored: dict[str, Any]) -> EnrichedProfile:
        """Reconstruct EnrichedProfile from stored DB row."""
        raw = stored.get("raw", {})
        return EnrichedProfile(
            username=stored.get("username", ""),
            profile=raw if isinstance(raw, dict) else {},
            fetched_at=stored.get("fetched_at", ""),
            stale=True,
        )
