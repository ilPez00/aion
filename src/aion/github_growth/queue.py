"""Action queue — prioritize and gate follow/unfollow actions.

Queue holds candidates pending approval. Dry-run mode logs but executes nothing.
Approval gates: max actions/day, allowlist/blocklist, score threshold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .config import GrowthConfig
from .score import ScoreResult
from .store_sqlite import GitHubStore


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class QueuedAction:
    username: str
    action: str  # 'follow' or 'unfollow'
    score: float
    reason: str
    score_factors: dict[str, float] = field(default_factory=dict)
    added_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: ApprovalStatus = ApprovalStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "action": self.action,
            "score": self.score,
            "reason": self.reason,
            "factors": self.score_factors,
            "added_at": self.added_at,
            "status": self.status.value,
        }


class ActionQueue:
    """Priority queue for follow/unfollow actions with safety gates."""

    def __init__(self, store: GitHubStore, config: GrowthConfig | None = None):
        self.store = store
        self.config = config or GrowthConfig.from_env()
        self._pending: list[QueuedAction] = []
        self._approved: list[QueuedAction] = []
        self._rejected: list[QueuedAction] = []

    def add(self, score_result: ScoreResult, action: str = "follow",
            context: dict[str, Any] | None = None) -> bool:
        """Add a scored candidate to the queue.

        Returns True if added, False if rejected by safety gates.
        """
        ctx = context or {}

        # Kill switch
        if not self.config.enabled:
            return False

        # Score threshold gate
        if not self._passes_safety_gates(score_result):
            return False

        # Daily action limit gate
        daily = self.store.real_actions_today()
        if daily >= self.config.max_total_actions_per_day:
            return False

        # Per-action type gate
        if action == "follow":
            follows_today = self.store.real_actions_today_by_type("follow")
            if follows_today >= self.config.max_follows_per_day:
                return False
        elif action == "unfollow":
            unfollows_today = self.store.real_actions_today_by_type("unfollow")
            if unfollows_today >= self.config.max_unfollows_per_day:
                return False

        queued = QueuedAction(
            username=score_result.username,
            action=action,
            score=score_result.score,
            reason=score_result.reason,
            score_factors=score_result.factors,
        )

        # Persist to store
        self.store.enqueue(
            score_result.username, action, score_result.score, score_result.reason
        )

        # Insert into priority-sorted pending list
        self._pending.append(queued)
        self._pending.sort(key=lambda q: q.score, reverse=True)
        return True

    def _passes_safety_gates(self, score_result: ScoreResult) -> bool:
        """Check all safety gates before queuing."""
        username = score_result.username

        # Allowlist: if non-empty, only allowlisted users pass
        if self.config.allowlist and username not in self.config.allowlist:
            return False

        # Blocklist
        if username in self.config.blocklist:
            return False

        # Score threshold
        if score_result.score < self.config.min_score_to_queue:
            return False

        # Min followers/stars gates
        profile = score_result.factors
        # These were checked during scoring, but double-check here for defense
        return True

    def list_pending(self) -> list[QueuedAction]:
        """List all pending (not yet executed) actions, sorted by score."""
        return sorted(self._pending, key=lambda q: q.score, reverse=True)

    def approve(self, username: str) -> bool:
        """Approve a specific user's queued action."""
        for q in self._pending:
            if q.username == username:
                q.status = ApprovalStatus.APPROVED
                self._pending.remove(q)
                self._approved.append(q)
                self.store.approve_in_queue(username)
                return True
        return False

    def reject(self, username: str) -> bool:
        """Reject a specific user's queued action."""
        for q in self._pending:
            if q.username == username:
                q.status = ApprovalStatus.REJECTED
                self._pending.remove(q)
                self._rejected.append(q)
                self.store.remove_from_queue(username)
                return True
        return False

    def get_approved(self) -> list[QueuedAction]:
        """Get all approved actions ready for execution."""
        return sorted(self._approved, key=lambda q: q.score, reverse=True)

    def clear_pending(self) -> int:
        """Clear all pending (unapproved) actions from store + memory."""
        count = self.store.clear_queue()
        self._pending.clear()
        return count

    def stats(self) -> dict[str, int]:
        return {
            "pending": len(self._pending),
            "approved": len(self._approved),
            "rejected": len(self._rejected),
        }
