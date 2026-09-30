"""Report generation — summary reports for github_growth operations.

Generates human-readable text reports from store data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from typing import Any

from .store_sqlite import GitHubStore


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class ReportGenerator:
    """Generates summary reports from github_growth data."""

    def __init__(self, store: GitHubStore):
        self.store = store

    def generate_summary(self) -> str:
        """Generate a summary report of all github_growth activity."""
        buf = StringIO()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        stats = self.store.stats()
        actions_today = self.store.actions_today()
        follows_today = self.store.actions_today_by_type("follow")
        unfollows_today = self.store.actions_today_by_type("unfollow")

        buf.write("=" * 60 + "\n")
        buf.write("GitHub Growth Summary Report\n")
        buf.write(f"Generated: {today}\n")
        buf.write("=" * 60 + "\n\n")

        buf.write("--- Data Stats ---\n")
        buf.write(f"  Users tracked:  {stats.get('users_count', 0)}\n")
        buf.write(f"  Repos tracked:  {stats.get('repos_count', 0)}\n")
        buf.write(f"  Scores recorded:{stats.get('scores_count', 0)}\n")
        buf.write(f"  Graph edges:    {stats.get('graph_edges_count', 0)}\n\n")

        buf.write("--- Actions (Today) ---\n")
        buf.write(f"  Total actions:  {actions_today}\n")
        buf.write(f"  Follows:        {follows_today}\n")
        buf.write(f"  Unfollows:      {unfollows_today}\n\n")

        buf.write("--- Queue Status ---\n")
        queued = self.store.get_queue()
        pending = [q for q in queued if not q.get("approved")]
        approved = [q for q in queued if q.get("approved")]
        buf.write(f"  Pending approval: {len(pending)}\n")
        buf.write(f"  Approved, ready:  {len(approved)}\n\n")

        buf.write("--- Recent Actions ---\n")
        recent = self.store.all_actions(10)
        for a in recent:
            status = a["result"]
            dry = "DRY-RUN" if a["dry_run"] else "REAL"
            buf.write(f"  [{status:10s}] [{dry:8s}] {a['action']:8s} {a['username']:<20s} {a['created_at']}\n")
        if not recent:
            buf.write("  (no actions recorded)\n")

        buf.write("\n" + "=" * 60 + "\n")
        return buf.getvalue()

    def generate_queue_report(self) -> str:
        """Generate a report of the current queue."""
        buf = StringIO()
        queued = self.store.get_queue()
        pending = [q for q in queued if not q.get("approved")]
        approved = [q for q in queued if q.get("approved")]

        buf.write("=" * 60 + "\n")
        buf.write("GitHub Growth — Action Queue\n")
        buf.write("=" * 60 + "\n\n")

        buf.write("--- Pending Approval ---\n")
        if not pending:
            buf.write("  (empty)\n")
        for q in pending:
            buf.write(f"  [{q['score']:.3f}] {q['action']:8s} {q['username']:<20s} {q['added_at']}\n")
            buf.write(f"         reason: {q['reason'][:80]}\n")

        buf.write("\n--- Approved (Ready to Execute) ---\n")
        if not approved:
            buf.write("  (empty)\n")
        for q in approved:
            buf.write(f"  [{q['score']:.3f}] {q['action']:8s} {q['username']:<20s} {q['added_at']}\n")

        buf.write("\n" + "=" * 60 + "\n")
        return buf.getvalue()

    def generate_user_profile(self, username: str) -> str:
        """Generate a detailed profile report for one user."""
        buf = StringIO()
        user = self.store.get_user(username)
        if not user:
            return f"No profile data for '{username}'\n"

        buf.write("=" * 60 + "\n")
        buf.write(f"User Profile: {username}\n")
        buf.write("=" * 60 + "\n\n")

        buf.write(f"  Name:       {user.get('name', '') or '(none)'}\n")
        buf.write(f"  Bio:        {user.get('bio', '') or '(none)'}\n")
        buf.write(f"  Company:    {user.get('company', '') or '(none)'}\n")
        buf.write(f"  Location:   {user.get('location', '') or '(none)'}\n")
        buf.write(f"  Blog:       {user.get('blog', '') or '(none)'}\n")
        buf.write(f"  Twitter:    {user.get('twitter_username', '') or '(none)'}\n")
        buf.write(f"  Profile:    {user.get('html_url', '') or '(none)'}\n\n")

        buf.write(f"  Followers:  {user.get('followers', 0)}\n")
        buf.write(f"  Following:  {user.get('following', 0)}\n")
        buf.write(f"  Public Repos: {user.get('public_repos', 0)}\n")
        buf.write(f"  Public Gists: {user.get('public_gists', 0)}\n")
        buf.write(f"  Created:    {user.get('created_at', 'unknown')}\n")
        buf.write(f"  Updated:    {user.get('updated_at', 'unknown')}\n")
        buf.write(f"  Fetched:    {user.get('fetched_at', 'unknown')}\n\n")

        # Latest score
        score = self.store.get_latest_score(username)
        if score:
            s_val, s_reason, s_time = score
            buf.write(f"  Latest Score: {s_val:.3f} (at {s_time})\n")
            buf.write(f"  Score Reason: {s_reason}\n\n")

        # Recent actions
        actions = self.store.conn.execute(
            "SELECT * FROM actions WHERE username = ? ORDER BY created_at DESC LIMIT 10",
            (username,)
        ).fetchall()
        if actions:
            buf.write("  Recent Actions:\n")
            for a in actions:
                dry = "DRY" if a["dry_run"] else "REAL"
                buf.write(f"    [{a['created_at']}] {a['action']} ({dry}): {a['result']}\n")

        buf.write("\n" + "=" * 60 + "\n")
        return buf.getvalue()

    def generate_metrics_report(self, days: int = 7) -> str:
        """Generate a metrics report for the last N days."""
        buf = StringIO()

        buf.write("=" * 60 + "\n")
        buf.write(f"GitHub Growth — Metrics (Last {days} days)\n")
        buf.write("=" * 60 + "\n\n")

        rows = self.store.conn.execute("""
            SELECT day, metric, value FROM metrics
            ORDER BY day DESC, metric
        """).fetchall()

        # Group by day
        by_day: dict[str, dict[str, int]] = {}
        for r in rows:
            day = r["day"]
            if day not in by_day:
                by_day[day] = {}
            by_day[day][r["metric"]] = r["value"]

        # Show last N days
        days_to_show = list(by_day.keys())[:days]
        for day in days_to_show:
            buf.write(f"  {day}:\n")
            metrics = by_day[day]
            for k in sorted(metrics.keys()):
                buf.write(f"    {k:25s} {metrics[k]}\n")
            buf.write("\n")

        if not days_to_show:
            buf.write("  (no data)\n")

        buf.write("=" * 60 + "\n")
        return buf.getvalue()
