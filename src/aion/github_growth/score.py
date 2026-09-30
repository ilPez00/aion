"""Scoring — rate users for follow-worthiness.

Factors (each 0-1, summed with weights):
  - contribution_score: activity in seed repos
  - engagement_score: followers/following ratio, repo stars
  - quality_score: repo activity, profile completeness
  - spam_score: inverted — penalizes high following/follower ratio
  - network_score: overlap with existing connections

Threshold: min_score_to_queue gates entry to the approval queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import GrowthConfig


@dataclass
class ScoreResult:
    username: str
    score: float
    reason: str
    factors: dict[str, float] = field(default_factory=dict)
    scored_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "score": self.score,
            "reason": self.reason,
            "factors": self.factors,
            "scored_at": self.scored_at,
        }


class Scorer:
    """Scores enriched profiles for follow-worthiness."""

    # Factor weights (sum to 1.0)
    WEIGHTS = {
        "contribution": 0.25,
        "engagement": 0.25,
        "quality": 0.20,
        "spam": 0.15,
        "network": 0.15,
    }

    def __init__(self, config: GrowthConfig | None = None):
        self.config = config or GrowthConfig.from_env()

    def score(self, profile: dict[str, Any], repos: list[dict[str, Any]],
              context: dict[str, Any] | None = None) -> ScoreResult:
        """Score a single user profile. context carries source_type, contributions, etc."""
        username = profile.get("login", profile.get("username", "unknown"))
        ctx = context or {}
        factors: dict[str, float] = {}
        reasons: list[str] = []

        # ── Contribution score ──────────────────────────────────────────────────
        # Based on how they were discovered and their activity in seed repos
        contributions = ctx.get("contributions", 0)
        source_type = ctx.get("source_type", "contributor")

        if source_type == "contributor" and contributions > 0:
            contrib_score = min(1.0, contributions / 20.0)  # cap at 20 contributions
            factors["contribution"] = contrib_score
            reasons.append(f"{contributions} contributions to seed repo")
        elif source_type == "dependent":
            factors["contribution"] = 0.7  # forked = some engagement
            reasons.append("forked seed repo")
        elif source_type == "user_provided":
            factors["contribution"] = 0.5  # neutral, user's explicit choice
            reasons.append("explicitly listed")
        elif source_type == "search":
            factors["contribution"] = 0.3  # search match only
            reasons.append(f"matched search: {ctx.get('search_query', '')}")
        else:
            factors["contribution"] = 0.2
            reasons.append("minimal evidence")

        # ── Engagement score ────────────────────────────────────────────────────
        followers = profile.get("followers", 0) or 0
        following = profile.get("following", 0) or 0

        if followers > 0:
            # Healthy ratio: more followers than following
            ratio = followers / max(following, 1)
            engagement = min(1.0, ratio / 5.0)  # cap at 5:1 ratio
            factors["engagement"] = engagement
            reasons.append(f"followers={followers}, following={following}, ratio={ratio:.2f}")
        else:
            factors["engagement"] = 0.1
            reasons.append("no followers")

        # Bonus for absolute follower count (but capped)
        follower_bonus = min(1.0, followers / 500.0)
        factors["engagement"] = factors.get("engagement", 0) * 0.7 + follower_bonus * 0.3

        # ── Quality score ───────────────────────────────────────────────────────
        public_repos = profile.get("public_repos", 0) or 0
        repo_stars = sum(r.get("stargazers_count", 0) for r in repos)
        repo_forks = sum(r.get("forks_count", 0) for r in repos)
        repo_issues = sum(r.get("open_issues", 0) for r in repos)

        # Quality signals: has repos, has stars, has open issues (engagement, not spam)
        repo_quality = 0.0
        if public_repos >= self.config.min_stars_to_follow:
            repo_quality += 0.4
        if repo_stars > 0:
            repo_quality += min(0.3, repo_stars / 100.0)
        if repo_forks > 0:
            repo_quality += min(0.2, repo_forks / 50.0)
        if repo_issues > 0 and repo_issues < public_repos * 5:
            repo_quality += 0.1  # has issues but not spammy

        # Profile completeness bonus
        completeness = 0.0
        for field_name in ("name", "bio", "company", "blog", "location"):
            if profile.get(field_name):
                completeness += 0.2 / 5
        repo_quality += min(0.3, completeness)

        factors["quality"] = min(1.0, repo_quality)
        reasons.append(f"repos={public_repos}, stars={repo_stars}, profile_complete={completeness:.0%}")

        # ── Spam score (inverted) ──────────────────────────────────────────────
        # Penalize: high following/followers ratio, no bio, very few repos, no stars
        spam_score = 0.0
        if following > 0 and followers > 0:
            spam_ratio = following / followers
            if spam_ratio > self.config.max_following_ratio:
                spam_score += 0.5
                reasons.append(f"spam ratio: following/followers = {spam_ratio:.1f}")
        elif followers == 0 and following > 10:
            spam_score += 0.7
            reasons.append("follows many with no followers")

        if public_repos < 3 and followers < 10:
            spam_score += 0.3
            reasons.append("few repos, few followers")

        if not profile.get("bio") and following > followers:
            spam_score += 0.2

        factors["spam"] = min(1.0, spam_score)
        # Invert for scoring: high spam = bad = low score
        factors["spam"] = 1.0 - factors["spam"]

        # ── Network score ───────────────────────────────────────────────────────
        # Check overlap with existing connections (if we have graph data)
        # This is filled in by queue.py which has access to the store
        factors["network"] = ctx.get("network_score", 0.5)
        reasons.append(f"network overlap: {ctx.get('network_score', 0.5):.2f}")

        # ── Final weighted score ────────────────────────────────────────────────
        total = sum(
            factors.get(k, 0.5) * self.WEIGHTS[k]
            for k in self.WEIGHTS
        )

        # Apply min thresholds as hard gates
        if followers < self.config.min_followers_to_follow and following > 0:
            total *= 0.5
            reasons.append("below min followers threshold")

        if public_repos < 1:
            total *= 0.3
            reasons.append("no public repos")

        # Cap at 1.0
        total = min(1.0, max(0.0, total))

        return ScoreResult(
            username=username,
            score=total,
            reason="; ".join(reasons),
            factors=factors,
        )

    def score_batch(self, profiles: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]]) -> list[ScoreResult]:
        """Score multiple profiles. Each item is (profile_dict, repos_list, context_dict)."""
        return [self.score(p, r, c) for p, r, c in profiles]

    def qualifies(self, score_result: ScoreResult) -> bool:
        """Check if a score meets the threshold for queuing."""
        return score_result.score >= self.config.min_score_to_queue
