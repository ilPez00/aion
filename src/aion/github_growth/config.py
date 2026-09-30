"""Configuration for github_growth subsystem.

All safety limits and toggles live here. Written to config via settings.py
pattern (Section/Field), persisted to config/layout.json under "github_growth".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_PATH = "~/.aion/github_growth.db"


@dataclass
class GrowthConfig:
    """Safety-first configuration for GitHub growth operations.

    All limits default to conservative/off values. User must explicitly opt-in.
    """
    # === Kill switch — if false, nothing runs ===
    enabled: bool = False

    # === API ===
    github_token_env: str = "GITHUB_TOKEN"
    api_base: str = "https://api.github.com"
    graphql_base: str = "https://api.github.com/graphql"

    # === Rate limits ===
    # Respect GitHub's limits; these are ceilings we never exceed
    min_request_interval_ms: int = 500
    max_requests_per_hour: int = 300

    # === Safety limits (per 24h window) ===
    max_follows_per_day: int = 5
    max_unfollows_per_day: int = 5
    max_total_actions_per_day: int = 10

    # === Discovery ===
    # Physis-pro is the seed project; we discover contributors and dependents
    seed_repo_owner: str = "ilPez00"
    seed_repo_name: str = "physis"
    # Also discover from aion's own dependents
    extra_seed_repos: list[str] = field(default_factory=lambda: ["ilPez00/aion"])
    # Exclude bots and known-noise accounts
    exclude_bots: bool = True
    exclude_orgs: list[str] = field(
        default_factory=lambda: ["github", "actions-user", "dependabot[bot]"]
    )

    # === Scoring thresholds ===
    min_score_to_queue: float = 0.3
    min_stars_to_follow: int = 5
    min_followers_to_follow: int = 10
    max_following_ratio: float = 10.0  # following/followers ratio above this = spam

    # === Enrichment ===
    # How much profile data to fetch per user
    fetch_repos: bool = True
    max_repos_per_user: int = 10
    fetch_followers: bool = True
    max_followers_fetch: int = 50
    fetch_following: bool = False  # only on explicit request, expensive

    # === Dry run ===
    dry_run: bool = True

    # === Persistence ===
    db_path: str = DEFAULT_DB_PATH

    # === Allowlist / Blocklist ===
    # If allowlist is non-empty, only those users are considered
    allowlist: list[str] = field(default_factory=list)
    blocklist: list[str] = field(default_factory=lambda: ["github", "actions-user", "dependabot[bot]"])

    @classmethod
    def from_env(cls) -> "GrowthConfig":
        """Build config from environment variables.

        GHGG_ENABLED, GHGG_DRY_RUN, GHGG_MAX_FOLLOWS_PER_DAY, etc.
        """
        return cls(
            enabled=_env_bool("GHGG_ENABLED", False),
            dry_run=_env_bool("GHGG_DRY_RUN", True),
            max_follows_per_day=_env_int("GHGG_MAX_FOLLOWS_PER_DAY", 5),
            max_unfollows_per_day=_env_int("GHGG_MAX_UNFOLLOWS_PER_DAY", 5),
            max_total_actions_per_day=_env_int("GHGG_MAX_TOTAL_ACTIONS_PER_DAY", 10),
            min_score_to_queue=_env_float("GHGG_MIN_SCORE_TO_QUEUE", 0.3),
            db_path=os.environ.get("GHGG_DB_PATH", DEFAULT_DB_PATH),
            allowlist=os.environ.get("GHGG_ALLOWLIST", "").split(",") if os.environ.get("GHGG_ALLOWLIST") else [],
            blocklist=os.environ.get("GHGG_BLOCKLIST", "").split(",") if os.environ.get("GHGG_BLOCKLIST") else ["github", "actions-user", "dependabot[bot]"],
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "max_follows_per_day": self.max_follows_per_day,
            "max_unfollows_per_day": self.max_unfollows_per_day,
            "max_total_actions_per_day": self.max_total_actions_per_day,
            "min_score_to_queue": self.min_score_to_queue,
            "db_path": self.db_path,
        }


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default
