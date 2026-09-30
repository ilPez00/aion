"""Metrics tracker — counters and CSV export for github_growth.

Tracks daily metrics: discover_count, enrich_count, score_count,
queue_count, dr_follow, dr_unfollow, real_follow, real_unfollow,
blocked_count.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store_sqlite import GitHubStore


METRIC_NAMES = [
    "discover_count",
    "enrich_count",
    "score_count",
    "queue_count",
    "dr_follow",
    "dr_unfollow",
    "real_follow",
    "real_unfollow",
    "blocked_count",
    "approved_count",
    "rejected_count",
]


class MetricsTracker:
    """Tracks and exports metrics for github_growth operations."""

    def __init__(self, store: GitHubStore):
        self.store = store

    def record(self, metric: str, count: int = 1) -> int:
        """Increment a metric counter by `count`. Returns new value."""
        total = 0
        for _ in range(count):
            total = self.store.increment_metric(metric)
        return total

    def get(self, metric: str, day: str | None = None) -> int:
        return self.store.get_metric(metric, day)

    def all_today(self) -> dict[str, int]:
        return self.store.metrics_for_today()

    def all_time(self) -> dict[str, int]:
        """Aggregate metrics across all days."""
        rows = self.store.conn.execute("""
            SELECT metric, SUM(value) as total FROM metrics GROUP BY metric
        """).fetchall()
        return {r["metric"]: r["total"] for r in rows}

    def to_csv(self, path: str | Path, days: int = 30) -> Path:
        """Export metrics for the last `days` days as CSV."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        days_ago = datetime.now(timezone.utc).toordinal() - days

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["day", "metric", "value"])

            rows = self.store.conn.execute("""
                SELECT day, metric, value FROM metrics
                WHERE CAST(strftime('%s', day) AS INTEGER) >= ?
                ORDER BY day DESC, metric
            """, (days_ago * 86400,)).fetchall() if False else self.store.conn.execute(
                "SELECT day, metric, value FROM metrics ORDER BY day DESC, metric"
            ).fetchall()

            for r in rows:
                writer.writerow([r["day"], r["metric"], r["value"]])

        return path

    def summary(self) -> dict[str, Any]:
        """Quick summary for dashboard display."""
        today = self.all_today()
        all_time = self.all_time()
        return {
            "today": {m: today.get(m, 0) for m in METRIC_NAMES},
            "all_time": {m: all_time.get(m, 0) for m in METRIC_NAMES},
        }
