"""SQLite persistence for github_growth — idempotent sync, audit trail.

Schema:
  users      — enriched profile snapshots
  repos      — repo metadata snapshots
  actions    — follow/unfollow history (audit)
  scores     — scoring history per user
  queue      — pending actions awaiting approval
  metrics    — daily counter snapshots
  graph_edges — follower/following relationships
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    login            TEXT,
    name             TEXT,
    bio              TEXT,
    company          TEXT,
    blog             TEXT,
    location         TEXT,
    email            TEXT,
    twitter_username TEXT,
    avatar_url       TEXT,
    html_url         TEXT,
    followers        INTEGER,
    following        INTEGER,
    public_repos     INTEGER,
    public_gists     INTEGER,
    created_at       TEXT,
    updated_at       TEXT,
    fetched_at       TEXT,
    raw              TEXT
);

CREATE TABLE IF NOT EXISTS repos (
    full_name      TEXT PRIMARY KEY,
    owner          TEXT,
    name           TEXT,
    description    TEXT,
    html_url       TEXT,
    stargazers_count INTEGER,
    forks_count    INTEGER,
    open_issues    INTEGER,
    language       TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    fetched_at     TEXT,
    raw            TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    username       TEXT,
    score          REAL,
    scored_at      TEXT,
    reason         TEXT,
    factors          TEXT,  -- JSON
    PRIMARY KEY (username, scored_at)
);

CREATE TABLE IF NOT EXISTS actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT,
    action         TEXT,      -- 'follow' or 'unfollow'
    dry_run        INTEGER,
    result         TEXT,      -- 'success', 'failed', 'blocked', 'dry_run'
    reason         TEXT,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS queue (
    username       TEXT PRIMARY KEY,
    action         TEXT,
    score          REAL,
    reason         TEXT,
    added_at       TEXT,
    approved       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics (
    day            TEXT,
    metric         TEXT,
    value          INTEGER,
    PRIMARY KEY (day, metric)
);

CREATE TABLE IF NOT EXISTS graph_edges (
    source         TEXT,
    target         TEXT,
    edge_type      TEXT,      -- 'follows', 'contributed_to'
    weight         REAL,
    updated_at     TEXT,
    PRIMARY KEY (source, target, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_scores_user ON scores(username);
CREATE INDEX IF NOT EXISTS idx_actions_day ON actions(created_at);
CREATE INDEX IF NOT EXISTS idx_graph_source ON graph_edges(source);
CREATE INDEX IF NOT EXISTS idx_graph_target ON graph_edges(target);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class GitHubStore:
    """SQLite-backed store for GitHub growth data.

    All writes are idempotent (upsert). Reads return plain dicts.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(Path(db_path).expanduser())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self):
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._connect()
        return self._conn

    # ── Users ─────────────────────────────────────────────────────────────────

    def upsert_user(self, profile: dict[str, Any]) -> None:
        """Store or update a user profile snapshot."""
        raw = json.dumps(profile, ensure_ascii=False)
        login = profile.get("login", "")
        self.conn.execute("""
            INSERT INTO users (username, login, name, bio, company, blog,
                location, email, twitter_username, avatar_url, html_url,
                followers, following, public_repos, public_gists,
                created_at, updated_at, fetched_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                name=excluded.name, bio=excluded.bio, company=excluded.company,
                blog=excluded.blog, location=excluded.location, email=excluded.email,
                twitter_username=excluded.twitter_username, avatar_url=excluded.avatar_url,
                html_url=excluded.html_url, followers=excluded.followers,
                following=excluded.following, public_repos=excluded.public_repos,
                public_gists=excluded.public_gists, created_at=excluded.created_at,
                updated_at=excluded.updated_at, fetched_at=excluded.fetched_at,
                raw=excluded.raw
        """, (
            login, login,
            profile.get("name"), profile.get("bio"), profile.get("company"),
            profile.get("blog"), profile.get("location"), profile.get("email"),
            profile.get("twitter_username"), profile.get("avatar_url"),
            profile.get("html_url"), profile.get("followers"),
            profile.get("following"), profile.get("public_repos"),
            profile.get("public_gists"), profile.get("created_at"),
            profile.get("updated_at"), _utcnow(), raw,
        ))
        self.conn.commit()

    def get_user(self, username: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            return None
        return {**dict(row), "raw": json.loads(row["raw"]) if row["raw"] else {}}

    def all_users(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM users").fetchall()
        return [{**dict(r), "raw": json.loads(r["raw"]) if r["raw"] else {}} for r in rows]

    # ── Repos ─────────────────────────────────────────────────────────────────

    def upsert_repo(self, repo_data: dict[str, Any]) -> None:
        full_name = repo_data.get("full_name", "")
        raw = json.dumps(repo_data, ensure_ascii=False)
        owner = repo_data.get("owner", {}).get("login", "")
        pushes = repo_data.get("pushed_at")
        self.conn.execute("""
            INSERT INTO repos (full_name, owner, name, description, html_url,
                stargazers_count, forks_count, open_issues, language,
                created_at, updated_at, fetched_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(full_name) DO UPDATE SET
                stargazers_count=excluded.stargazers_count,
                forks_count=excluded.forks_count,
                open_issues=excluded.open_issues,
                description=excluded.description,
                language=excluded.language,
                updated_at=excluded.updated_at,
                fetched_at=excluded.fetched_at,
                raw=excluded.raw
        """, (
            full_name, owner, repo_data.get("name"), repo_data.get("description"),
            repo_data.get("html_url"), repo_data.get("stargazers_count"),
            repo_data.get("forks_count"), repo_data.get("open_issues"),
            repo_data.get("language"), repo_data.get("created_at"),
            repo_data.get("updated_at"), _utcnow(), raw,
        ))
        self.conn.commit()

    # ── Scores ────────────────────────────────────────────────────────────────

    def save_score(self, username: str, score: float, reason: str, factors: dict[str, Any]) -> None:
        self.conn.execute("""
            INSERT INTO scores (username, score, scored_at, reason, factors)
            VALUES (?, ?, ?, ?, ?)
        """, (username, score, _utcnow(), reason, json.dumps(factors)))
        self.conn.commit()

    def get_latest_score(self, username: str) -> tuple[float, str, str] | None:
        row = self.conn.execute("""
            SELECT score, reason, scored_at FROM scores
            WHERE username = ? ORDER BY scored_at DESC LIMIT 1
        """, (username,)).fetchone()
        if row is None:
            return None
        return (row["score"], row["reason"], row["scored_at"])

    # ── Queue ─────────────────────────────────────────────────────────────────

    def enqueue(self, username: str, action: str, score: float, reason: str) -> bool:
        """Add to queue. Returns False if already queued or blocked."""
        existing = self.conn.execute(
            "SELECT 1 FROM queue WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            return False
        self.conn.execute("""
            INSERT INTO queue (username, action, score, reason, added_at, approved)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (username, action, score, reason, _utcnow()))
        self.conn.commit()
        return True

    def dequeue(self) -> list[tuple[str, str, float, str]]:
        rows = self.conn.execute("""
            SELECT username, action, score, reason FROM queue
            WHERE approved = 1 ORDER BY score DESC, added_at ASC
        """).fetchall()
        return [(r["username"], r["action"], r["score"], r["reason"]) for r in rows]

    def get_queue(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT username, action, score, reason, added_at, approved FROM queue
            ORDER BY score DESC, added_at ASC
        """).fetchall()
        return [dict(r) for r in rows]

    def approve_in_queue(self, username: str) -> bool:
        cur = self.conn.execute(
            "UPDATE queue SET approved = 1 WHERE username = ? AND approved = 0",
            (username,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_from_queue(self, username: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM queue WHERE username = ?", (username,)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_queue(self) -> int:
        count = self.conn.execute("DELETE FROM queue").rowcount
        self.conn.commit()
        return count or 0

    # ── Actions (audit trail) ────────────────────────────────────────────────

    def log_action(self, username: str, action: str, dry_run: bool,
                   result: str, reason: str = "") -> None:
        self.conn.execute("""
            INSERT INTO actions (username, action, dry_run, result, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (username, action, 1 if dry_run else 0, result, reason, _utcnow()))
        self.conn.commit()

    def actions_today(self) -> int:
        """Count all actions logged today, dry-run or not (audit trail).

        Used by reports and the test suite. Does NOT gate limits — those must
        ignore dry-runs so a dry-run never burns someone's daily quota.
        """
        today = _date_day()
        row = self.conn.execute(
            "SELECT COUNT(*) as c FROM actions WHERE date(created_at) = ?",
            (today,)
        ).fetchone()
        return row["c"] if row else 0

    def real_actions_today(self) -> int:
        """Only non-dry-run actions today — the figure that gates limits."""
        today = _date_day()
        row = self.conn.execute(
            "SELECT COUNT(*) as c FROM actions WHERE dry_run = 0 AND date(created_at) = ?",
            (today,)
        ).fetchone()
        return row["c"] if row else 0

    def actions_today_by_type(self, action: str) -> int:
        """Count all actions of `action` today (dry-run or not)."""
        today = _date_day()
        row = self.conn.execute(
            "SELECT COUNT(*) as c FROM actions WHERE action = ? AND date(created_at) = ?",
            (action, today)
        ).fetchone()
        return row["c"] if row else 0

    def real_actions_today_by_type(self, action: str) -> int:
        """Only non-dry-run actions of `action` today — gates per-type limits."""
        today = _date_day()
        row = self.conn.execute(
            "SELECT COUNT(*) as c FROM actions WHERE action = ? AND dry_run = 0 AND date(created_at) = ?",
            (action, today)
        ).fetchone()
        return row["c"] if row else 0

    def all_actions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT username, action, dry_run, result, reason, created_at
            FROM actions ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── Metrics ──────────────────────────────────────────────────────────────

    def increment_metric(self, metric: str) -> int:
        today = _date_day()
        self.conn.execute("""
            INSERT INTO metrics (day, metric, value) VALUES (?, ?, 1)
            ON CONFLICT(day, metric) DO UPDATE SET value = value + 1
        """, (today, metric))
        self.conn.commit()
        # return new value
        row = self.conn.execute(
            "SELECT value FROM metrics WHERE day = ? AND metric = ?",
            (today, metric)
        ).fetchone()
        return row["value"] if row else 1

    def get_metric(self, metric: str, day: str | None = None) -> int:
        day = day or _date_day()
        row = self.conn.execute(
            "SELECT value FROM metrics WHERE day = ? AND metric = ?",
            (day, metric)
        ).fetchone()
        return row["value"] if row else 0

    def metrics_for_today(self) -> dict[str, int]:
        today = _date_day()
        rows = self.conn.execute(
            "SELECT metric, value FROM metrics WHERE day = ?",
            (today,)
        ).fetchall()
        return {r["metric"]: r["value"] for r in rows}

    # ── Graph ─────────────────────────────────────────────────────────────────

    def upsert_edge(self, source: str, target: str, edge_type: str, weight: float = 1.0) -> None:
        self.conn.execute("""
            INSERT INTO graph_edges (source, target, edge_type, weight, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, target, edge_type) DO UPDATE SET
                weight = excluded.weight, updated_at = excluded.updated_at
        """, (source, target, edge_type, weight, _utcnow()))
        self.conn.commit()

    def get_followers(self, username: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT source FROM graph_edges WHERE target = ? AND edge_type = 'follows'",
            (username,)
        ).fetchall()
        return [r["source"] for r in rows]

    def get_following(self, username: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT target FROM graph_edges WHERE source = ? AND edge_type = 'follows'",
            (username,)
        ).fetchall()
        return [r["target"] for r in rows]

    def get_contributors(self, repo_full_name: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT source FROM graph_edges WHERE target = ? AND edge_type = 'contributed_to'",
            (repo_full_name,)
        ).fetchall()
        return [r["source"] for r in rows]

    # ── Maintenance ──────────────────────────────────────────────────────────

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def stats(self) -> dict[str, int]:
        """Quick stats for dashboard."""
        result = {}
        for table in ("users", "repos", "scores", "actions", "queue", "graph_edges"):
            row = self.conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()
            result[f"{table}_count"] = row["c"] if row else 0
        return result
