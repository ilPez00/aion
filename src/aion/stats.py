"""
stats.py — the Jarvis HUD data layer.

Reads REAL numbers from Hermes' own session database
(~/.hermes/state.db) so aion's header + right rail show live token burn,
spend, and which agents are running right now — not fakes.

Everything here is READ-ONLY: we open the DB in immutable/query-only mode with
a short busy-timeout so we never block or corrupt the Hermes writer that owns
the file. If the DB is missing or unreadable we degrade to an empty snapshot;
the UI just shows zeros instead of crashing.

The single entry point is `StatsReader.snapshot()`, which returns a plain
`StatsSnapshot` dataclass the UI renders from. `harnesses.StatsHarness` polls
this on an interval and republishes onto the bus.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_DB = Path.home() / ".hermes" / "state.db"

# a session with no ended_at that has been touched within this many seconds is
# considered a "live agent"; older open sessions are almost always crashed/
# abandoned rows Hermes never finalized, so we don't count them as running.
LIVE_WINDOW_S = 90 * 60  # 90 min


@dataclass
class ModelUsage:
    model: str
    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens


@dataclass
class LiveAgent:
    session_id: str
    model: str
    branch: str
    repo: str
    messages: int
    age_s: float


@dataclass
class StatsSnapshot:
    """Plain snapshot the UI renders from. Safe to publish on the bus."""
    ok: bool = False                       # False => DB unavailable, show dim
    window_label: str = "today"
    total_input: int = 0
    total_output: int = 0
    total_reasoning: int = 0
    total_cost_usd: float = 0.0
    models: list[ModelUsage] = field(default_factory=list)   # sorted desc
    live_agents: list[LiveAgent] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output + self.total_reasoning

    @property
    def live_count(self) -> int:
        return len(self.live_agents)

    def as_metrics(self) -> dict:
        """Compact dict for TOPIC_STATS (what the right rail consumes)."""
        return {
            "ok": self.ok,
            "window": self.window_label,
            "in": self.total_input,
            "out": self.total_output,
            "reasoning": self.total_reasoning,
            "cost_usd": round(self.total_cost_usd, 4),
            "live": self.live_count,
            "models": [
                {"model": m.model, "in": m.input_tokens, "out": m.output_tokens,
                 "tot": m.total_tokens, "cost": round(m.cost_usd, 4),
                 "sessions": m.sessions}
                for m in self.models
            ],
            "agents": [
                {"id": a.session_id, "model": a.model, "branch": a.branch,
                 "repo": a.repo, "msgs": a.messages, "age_s": int(a.age_s)}
                for a in self.live_agents
            ],
        }


# window presets -> seconds back from now (None = all time)
WINDOWS = {
    "today": None,      # resolved to local midnight in _window_start
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "all": -1,
}


class StatsReader:
    """Read-only aggregator over Hermes state.db. Never writes."""

    def __init__(self, db_path: str | Path | None = None,
                 window: str = "today") -> None:
        self.db_path = Path(db_path or DEFAULT_DB)
        self.window = window if window in WINDOWS else "today"

    # ---- connection (read-only, non-blocking) ---------------------------
    def _connect(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        try:
            # immutable=1 would forbid seeing concurrent writes; we want the
            # live view, so open read-only (mode=ro) with a short timeout and
            # query_only so we can never mutate the writer's file.
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 400")
            return conn
        except sqlite3.Error:
            return None

    def _window_start(self) -> float | None:
        w = WINDOWS[self.window]
        if w is None:  # "today" -> local midnight
            lt = time.localtime()
            midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                    0, 0, 0, 0, 0, -1))
            return midnight
        if w < 0:  # all time
            return None
        return time.time() - w

    # ---- the one method the UI/harness calls ----------------------------
    def snapshot(self) -> StatsSnapshot:
        conn = self._connect()
        if conn is None:
            return StatsSnapshot(ok=False, window_label=self.window)
        try:
            return self._read(conn)
        except sqlite3.Error:
            return StatsSnapshot(ok=False, window_label=self.window)
        finally:
            conn.close()

    def _read(self, conn: sqlite3.Connection) -> StatsSnapshot:
        start = self._window_start()
        where = ""
        params: tuple = ()
        if start is not None:
            where = "WHERE started_at >= ?"
            params = (start,)

        rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(model), ''), 'unknown') AS model,
                   COUNT(*)                       AS sessions,
                   COALESCE(SUM(input_tokens), 0)     AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)    AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd
            FROM sessions
            {where}
            GROUP BY model
            HAVING input_tokens + output_tokens > 0
            ORDER BY (input_tokens + output_tokens) DESC
            """,
            params,
        ).fetchall()

        models = [
            ModelUsage(
                model=r["model"], sessions=r["sessions"],
                input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                reasoning_tokens=r["reasoning_tokens"], cost_usd=r["cost_usd"],
            )
            for r in rows
        ]
        snap = StatsSnapshot(
            ok=True, window_label=self.window, models=models,
            total_input=sum(m.input_tokens for m in models),
            total_output=sum(m.output_tokens for m in models),
            total_reasoning=sum(m.reasoning_tokens for m in models),
            total_cost_usd=sum(m.cost_usd for m in models),
        )
        snap.live_agents = self._live_agents(conn)
        return snap

    def _live_agents(self, conn: sqlite3.Connection) -> list[LiveAgent]:
        now = time.time()
        cutoff = now - LIVE_WINDOW_S
        rows = conn.execute(
            """
            SELECT id, COALESCE(model, '') AS model,
                   COALESCE(git_branch, '') AS branch,
                   COALESCE(git_repo_root, '') AS repo,
                   COALESCE(message_count, 0) AS msgs,
                   started_at
            FROM sessions
            WHERE ended_at IS NULL AND started_at >= ?
            ORDER BY started_at DESC
            LIMIT 12
            """,
            (cutoff,),
        ).fetchall()
        out: list[LiveAgent] = []
        for r in rows:
            repo = r["repo"].rsplit("/", 1)[-1] if r["repo"] else ""
            out.append(LiveAgent(
                session_id=r["id"], model=r["model"], branch=r["branch"],
                repo=repo, messages=r["msgs"], age_s=now - r["started_at"],
            ))
        return out


def human_tokens(n: int) -> str:
    """1234 -> '1.2K', 3400000 -> '3.4M' — compact for a 1-line header."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000_000:.1f}B"
