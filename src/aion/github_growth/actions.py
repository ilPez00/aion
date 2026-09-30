"""Action executor — carries out follow/unfollow with safety checks.

Every action is logged to the audit trail in store_sqlite.GitHubStore.actions.
Dry-run mode is always the default; no API calls made without explicit opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .api import GitHubAPI
from .config import GrowthConfig
from .queue import ActionQueue, QueuedAction, ApprovalStatus
from .store_sqlite import GitHubStore


class ActionResult(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"


@dataclass
class ActionResultData:
    username: str
    action: str
    result: ActionResult
    reason: str
    dry_run: bool
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "action": self.action,
            "result": self.result.value,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "timestamp": self.timestamp,
        }


class ActionExecutor:
    """Executes follow/unfollow actions with safety gates and audit logging."""

    def __init__(self, api: GitHubAPI, store: GitHubStore,
                 queue: ActionQueue, config: GrowthConfig | None = None):
        self.api = api
        self.store = store
        self.queue = queue
        self.config = config or GrowthConfig.from_env()

    def execute_approved(self) -> list[ActionResultData]:
        """Execute all approved actions in the queue.

        Honors daily limits and dry-run mode.
        """
        results = []
        # Read approved from store (persistent), not just in-memory
        store_queue = self.store.get_queue()
        store_approved = [q for q in store_queue if q.get("approved")]

        # Also include in-memory approved (for same-session use)
        memory_approved = {q.username for q in self.queue._approved}
        store_seen = {q["username"] for q in store_approved}

        approved_usernames = store_seen | memory_approved
        if not approved_usernames:
            return results

        # Build QueuedAction objects from store data
        for q in store_approved:
            action = QueuedAction(
                username=q["username"],
                action=q["action"],
                score=q["score"],
                reason=q["reason"],
            )
            result = self._execute_one(action)
            results.append(result)

            if result.result == ActionResult.BLOCKED:
                break

            self.store.remove_from_queue(action.username)
            if action.username in memory_approved:
                self.queue._approved = [a for a in self.queue._approved if a.username != action.username]

        return results

    def _execute_one(self, action: QueuedAction) -> ActionResultData:
        """Execute a single action with all safety checks."""
        # Kill switch
        if not self.config.enabled:
            return ActionResultData(
                username=action.username,
                action=action.action,
                result=ActionResult.BLOCKED,
                reason="github_growth disabled",
                dry_run=False,
            )

        # Daily limit check
        daily = self.store.actions_today()
        if daily >= self.config.max_total_actions_per_day:
            return ActionResultData(
                username=action.username,
                action=action.action,
                result=ActionResult.BLOCKED,
                reason=f"daily action limit reached ({daily}/{self.config.max_total_actions_per_day})",
                dry_run=False,
            )

        # Per-action type check
        if action.action == "follow":
            follows_today = self.store.actions_today_by_type("follow")
            if follows_today >= self.config.max_follows_per_day:
                return ActionResultData(
                    username=action.username,
                    action=action.action,
                    result=ActionResult.BLOCKED,
                    reason=f"daily follow limit reached ({follows_today}/{self.config.max_follows_per_day})",
                    dry_run=False,
                )
        elif action.action == "unfollow":
            unfollows_today = self.store.actions_today_by_type("unfollow")
            if unfollows_today >= self.config.max_unfollows_per_day:
                return ActionResultData(
                    username=action.username,
                    action=action.action,
                    result=ActionResult.BLOCKED,
                    reason=f"daily unfollow limit reached ({unfollows_today}/{self.config.max_unfollows_per_day})",
                    dry_run=False,
                )

        # Check if already following (for follow actions)
        if action.action == "follow":
            if self._is_already_following(action.username):
                return ActionResultData(
                    username=action.username,
                    action=action.action,
                    result=ActionResult.SKIPPED,
                    reason="already following",
                    dry_run=False,
                )

        # Dry-run: log but don't execute
        if self.config.dry_run:
            self.store.log_action(
                action.username, action.action, dry_run=True,
                result=ActionResult.DRY_RUN.value,
                reason=action.reason,
            )
            self.store.increment_metric(f"dr_{action.action}")
            return ActionResultData(
                username=action.username,
                action=action.action,
                result=ActionResult.DRY_RUN,
                reason="dry-run enabled",
                dry_run=True,
            )

        # Execute the real action
        if action.action == "follow":
            success = self.api.follow(action.username, dry_run=False)
        elif action.action == "unfollow":
            success = self.api.unfollow(action.username, dry_run=False)
        else:
            return ActionResultData(
                username=action.username,
                action=action.action,
                result=ActionResult.BLOCKED,
                reason=f"unknown action: {action.action}",
                dry_run=False,
            )

        result_enum = ActionResult.SUCCESS if success else ActionResult.FAILED
        self.store.log_action(
            action.username, action.action, dry_run=False,
            result=result_enum.value,
            reason=action.reason if success else "API returned failure",
        )
        self.store.increment_metric(f"real_{action.action}")

        return ActionResultData(
            username=action.username,
            action=action.action,
            result=result_enum,
            reason=action.reason if success else "API call failed",
            dry_run=False,
        )

    def _is_already_following(self, username: str) -> bool:
        """Check if we're already following this user via graph or API."""
        # Check graph first (fast)
        following = self.store.get_following("self")
        # Note: 'self' would be the authenticated user's login — for now, check store
        if username in following:
            return True
        # Fallback: would need to fetch authenticated user's following list
        # This is a simplification — full impl would cache the authed user's follow list
        return False

    def follow(self, username: str, reason: str = "", dry_run: bool | None = None) -> ActionResultData:
        """Directly follow a user (bypasses queue). Respects all safety gates."""
        if not self.config.enabled:
            return ActionResultData(
                username=username,
                action="follow",
                result=ActionResult.BLOCKED,
                reason="github_growth disabled",
                dry_run=False,
            )

        daily = self.store.real_actions_today()
        if daily >= self.config.max_total_actions_per_day:
            return ActionResultData(
                username=username,
                action="follow",
                result=ActionResult.BLOCKED,
                reason=f"daily action limit reached ({daily}/{self.config.max_total_actions_per_day})",
                dry_run=False,
            )

        follows_today = self.store.real_actions_today_by_type("follow")
        if follows_today >= self.config.max_follows_per_day:
            return ActionResultData(
                username=username,
                action="follow",
                result=ActionResult.BLOCKED,
                reason=f"daily follow limit reached ({follows_today}/{self.config.max_follows_per_day})",
                dry_run=False,
            )

        effective_dry_run = dry_run if dry_run is not None else self.config.dry_run

        if effective_dry_run:
            self.store.log_action(username, "follow", dry_run=True,
                                  result=ActionResult.DRY_RUN.value, reason=reason)
            self.store.increment_metric("dr_follow")
            return ActionResultData(
                username=username,
                action="follow",
                result=ActionResult.DRY_RUN,
                reason="dry-run enabled",
                dry_run=True,
            )

        success = self.api.follow(username, dry_run=False)
        result_enum = ActionResult.SUCCESS if success else ActionResult.FAILED
        self.store.log_action(username, "follow", dry_run=False,
                              result=result_enum.value,
                              reason=reason if success else "API returned failure")
        self.store.increment_metric("real_follow")

        return ActionResultData(
            username=username,
            action="follow",
            result=result_enum,
            reason=reason if success else "API call failed",
            dry_run=False,
        )

    def unfollow(self, username: str, reason: str = "", dry_run: bool | None = None) -> ActionResultData:
        """Directly unfollow a user (bypasses queue). Respects all safety gates."""
        if not self.config.enabled:
            return ActionResultData(
                username=username,
                action="unfollow",
                result=ActionResult.BLOCKED,
                reason="github_growth disabled",
                dry_run=False,
            )

        daily = self.store.real_actions_today()
        if daily >= self.config.max_total_actions_per_day:
            return ActionResultData(
                username=username,
                action="unfollow",
                result=ActionResult.BLOCKED,
                reason=f"daily action limit reached ({daily}/{self.config.max_total_actions_per_day})",
                dry_run=False,
            )

        unfollows_today = self.store.real_actions_today_by_type("unfollow")
        if unfollows_today >= self.config.max_unfollows_per_day:
            return ActionResultData(
                username=username,
                action="unfollow",
                result=ActionResult.BLOCKED,
                reason=f"daily unfollow limit reached ({unfollows_today}/{self.config.max_unfollows_per_day})",
                dry_run=False,
            )

        effective_dry_run = dry_run if dry_run is not None else self.config.dry_run

        if effective_dry_run:
            self.store.log_action(username, "unfollow", dry_run=True,
                                  result=ActionResult.DRY_RUN.value, reason=reason)
            self.store.increment_metric("dr_unfollow")
            return ActionResultData(
                username=username,
                action="unfollow",
                result=ActionResult.DRY_RUN,
                reason="dry-run enabled",
                dry_run=True,
            )

        success = self.api.unfollow(username, dry_run=False)
        result_enum = ActionResult.SUCCESS if success else ActionResult.FAILED
        self.store.log_action(username, "unfollow", dry_run=False,
                              result=result_enum.value,
                              reason=reason if success else "API returned failure")
        self.store.increment_metric("real_unfollow")

        return ActionResultData(
            username=username,
            action="unfollow",
            result=result_enum,
            reason=reason if success else "API call failed",
            dry_run=False,
        )
