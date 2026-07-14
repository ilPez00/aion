"""
test_stats.py — unit tests for the Jarvis HUD data layer.

Builds a throwaway sqlite DB shaped like Hermes' sessions table, points a
StatsReader at it, and asserts the aggregation is correct. Zero UI, zero
network, deterministic. Also drives StatsHarness.poll_once through the bus.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.stats import StatsReader, human_tokens, StatsSnapshot  # noqa: E402
from aion.core import Bus, TaskRegistry, TOPIC_STATS  # noqa: E402
from aion.harnesses import StatsHarness, HarnessConfig  # noqa: E402


SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    model TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    git_branch TEXT,
    git_repo_root TEXT
);
"""


def _make_db(path: Path) -> None:
    now = time.time()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    rows = [
        # id, model, started, ended, msgs, in, out, reason, cost, branch, repo
        ("s1", "z-ai/glm-5.2", now - 100, None, 40, 1000, 500, 50, 0.02, "main", "/home/gio/cyclops"),
        ("s2", "z-ai/glm-5.2", now - 200, now - 50, 20, 2000, 300, 0, 0.01, "main", "/home/gio/aion"),
        ("s3", "tencent/hy3:free", now - 300, None, 10, 5000, 800, 0, 0.0, "feat/x", "/home/gio/praxis_webapp"),
        # old open session (beyond LIVE_WINDOW) — must NOT count as live
        ("s4", "old/model", now - 999999, None, 5, 100, 100, 0, 0.0, "", ""),
        # zero-token session — filtered out of model list
        ("s5", "empty/model", now - 10, now - 5, 1, 0, 0, 0, 0.0, "", ""),
    ]
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_human_tokens():
    assert human_tokens(500) == "500"
    assert human_tokens(1500) == "1.5K"
    assert human_tokens(2_400_000) == "2.4M"
    print("ok: human_tokens")


def test_missing_db_degrades():
    r = StatsReader(db_path="/nonexistent/nope.db")
    snap = r.snapshot()
    assert isinstance(snap, StatsSnapshot)
    assert snap.ok is False
    assert snap.total_tokens == 0
    print("ok: missing db degrades to empty snapshot")


def test_aggregation(tmp_path: Path):
    db = tmp_path / "state.db"
    _make_db(db)
    r = StatsReader(db_path=db, window="all")
    snap = r.snapshot()
    assert snap.ok is True
    # glm rolls up two sessions: in 3000, out 800, reasoning 50
    glm = next(m for m in snap.models if m.model == "z-ai/glm-5.2")
    assert glm.input_tokens == 3000, glm.input_tokens
    assert glm.output_tokens == 800, glm.output_tokens
    assert glm.reasoning_tokens == 50
    assert glm.sessions == 2
    assert abs(glm.cost_usd - 0.03) < 1e-6
    # zero-token session excluded
    assert all(m.model != "empty/model" for m in snap.models)
    # ordered by (in+out) desc: hy3 (5800) > glm (3800) > old (200)
    assert snap.models[0].model == "tencent/hy3:free"
    # totals
    assert snap.total_input == 1000 + 2000 + 5000 + 100  # 8100
    # live agents: s1 + s3 (open & recent); s4 too old, s2/s5 ended
    live_ids = {a.session_id for a in snap.live_agents}
    assert live_ids == {"s1", "s3"}, live_ids
    a1 = next(a for a in snap.live_agents if a.session_id == "s1")
    assert a1.repo == "cyclops"        # basename of git_repo_root
    assert a1.branch == "main"
    print("ok: aggregation + live-agent census correct")


def test_window_today(tmp_path: Path):
    db = tmp_path / "state2.db"
    _make_db(db)
    r = StatsReader(db_path=db, window="today")
    snap = r.snapshot()
    # all seeded sessions except s4 started 'now-ish' -> counted today
    assert snap.ok and snap.total_tokens > 0
    print("ok: today window works")


def test_harness_poll_publishes(tmp_path: Path):
    db = tmp_path / "state3.db"
    _make_db(db)
    bus = Bus()
    got = {}

    async def cap(msg):
        got.update(msg)

    bus.subscribe(TOPIC_STATS, cap)
    cfg = HarnessConfig.from_dict(
        {"id": "stats", "type": "stats", "window": "all",
         "interval": 1.0, "db_path": str(db)})
    h = StatsHarness(cfg, bus, TaskRegistry(bus))

    async def drive():
        metrics = await h.poll_once()
        await asyncio.sleep(0.05)  # let bus deliver
        return metrics

    metrics = asyncio.run(drive())
    assert metrics["ok"] is True
    assert metrics["live"] == 2
    assert got.get("harness") == "stats"
    assert got["metrics"]["live"] == 2
    print("ok: StatsHarness.poll_once publishes real metrics on the bus")


def _run():
    import tempfile
    test_human_tokens()
    test_missing_db_degrades()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_aggregation(tmp)
        test_window_today(tmp)
        test_harness_poll_publishes(tmp)
    print("\nALL STATS TESTS PASSED")


if __name__ == "__main__":
    _run()
