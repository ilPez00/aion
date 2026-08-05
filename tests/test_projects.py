"""
test_projects.py — unit tests for the Projects workspace data layer.

Builds throwaway git repos (real `git init` + commits + dirty files) and a
fake sqlite sessions DB, points ProjectsReader at them, and asserts the
aggregation is correct. No real repos, no network, deterministic-ish
(commit timestamps are real but relative deltas are what we assert).
"""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.projects import ProjectsReader  # noqa: E402
from aion.core import Bus, TaskRegistry, TOPIC_STATS  # noqa: E402
from aion.harnesses import ProjectsHarness, HarnessConfig  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   capture_output=True, check=True)


def _make_repo(path: Path, dirty: int = 0, ahead: int = 0) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("# hi\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init commit")
    # craft `ahead` commits on top of the (no-upstream) tip
    for i in range(ahead):
        (path / f"f{i}.txt").write_text("y")
        _git(path, "add", ".")
        _git(path, "commit", "-qm", f"ahead {i}")
    # create N dirty untracked files LAST so they stay unstaged
    for i in range(dirty):
        (path / f"junk{i}.txt").write_text("x")


def _make_sessions_db(path: Path, cwd_for: Path, sessions_today: int,
                      tokens: int) -> None:
    now = time.time()
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    conn = sqlite3.connect(path)
    # mirror the real Hermes sessions table columns we read
    conn.execute("CREATE TABLE sessions ("
                 "id TEXT PRIMARY KEY, cwd TEXT, started_at REAL, "
                 "input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, "
                 "reasoning_tokens INTEGER DEFAULT 0)")
    rows = []
    for i in range(sessions_today):
        # one session earlier today, cwd == the repo path
        per = tokens // sessions_today if sessions_today else 0
        rows.append((f"s{i}", str(cwd_for), midnight + 3600 + i, per, 0, 0))
    # one session in the distant past, must NOT count as today
    rows.append(("old", str(cwd_for), midnight - 86400, 999999, 0, 0))
    conn.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_missing_repo_degrades(tmp_path: Path):
    r = ProjectsReader(repos=[str(tmp_path / "nope")])
    st = r.snapshot()
    assert len(st) == 1 and not st[0].exists, st
    print("ok: missing repo -> exists=False, no crash")


def test_git_status(tmp_path: Path):
    repo = tmp_path / "repo1"
    _make_repo(repo, dirty=3, ahead=0)
    r = ProjectsReader(repos=[str(repo)])
    st = r.snapshot()
    assert len(st) == 1
    s = st[0]
    assert s.is_git and s.branch, s.branch
    assert s.dirty == 3, s.dirty
    # no upstream configured -> ahead/behind correctly read as 0
    assert s.ahead == 0 and s.behind == 0, (s.ahead, s.behind)
    assert s.last_commit_age_s is not None
    assert s.last_commit, "last_commit should be populated"
    print("ok: git status (branch/dirty/last-commit) correct; no-upstream -> 0/0")


def test_ahead_behind_with_upstream(tmp_path: Path):
    repo = tmp_path / "repo2"
    _make_repo(repo, dirty=0, ahead=0)
    # fabricate an upstream: branch 'main' tracks 'base', add 2 commits ahead
    _git(repo, "branch", "base")
    _git(repo, "branch", "--set-upstream-to", "base")
    for i in range(2):
        (repo / f"f{i}.txt").write_text("y")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", f"ahead {i}")
    r = ProjectsReader(repos=[str(repo)])
    s = r.snapshot()[0]
    assert s.ahead == 2, s.ahead
    assert s.behind == 0
    print("ok: ahead/behind reads 2/0 with a real upstream set")


def test_session_join_by_cwd(tmp_path: Path):
    repo = tmp_path / "repo2"
    _make_repo(repo, dirty=0)
    db = tmp_path / "state.db"
    _make_sessions_db(db, repo, sessions_today=4, tokens=40_000)
    r = ProjectsReader(repos=[str(repo)], db_path=db)
    st = r.snapshot()
    s = st[0]
    assert s.sessions_today == 4, s.sessions_today
    assert s.tokens_today == 40_000, s.tokens_today
    assert s.last_session_id == "s3"
    assert s.last_session_age_s is not None
    print("ok: session join by cwd (today count + tokens + last id)")


def test_session_join_prefix(tmp_path: Path):
    # cwd is UNDER the repo, not equal -> should still match (subdir session)
    repo = tmp_path / "repo3"
    _make_repo(repo, dirty=1)
    sub = repo / "subdir"
    sub.mkdir()
    db = tmp_path / "state2.db"
    _make_sessions_db(db, sub, sessions_today=2, tokens=10_000)
    r = ProjectsReader(repos=[str(repo)], db_path=db)
    s = r.snapshot()[0]
    assert s.sessions_today == 2, s.sessions_today
    print("ok: cwd-under-repo prefix match works")


def test_no_state_db_no_crash(tmp_path: Path):
    repo = tmp_path / "repo4"
    _make_repo(repo, dirty=0)
    r = ProjectsReader(repos=[str(repo)], db_path=tmp_path / "absent.db")
    s = r.snapshot()[0]
    assert s.sessions_today == 0 and s.tokens_today == 0
    print("ok: absent state.db -> zero activity, no crash")


def test_harness_poll_publishes(tmp_path: Path):
    repo = tmp_path / "repo5"
    _make_repo(repo, dirty=2)
    db = tmp_path / "state3.db"
    _make_sessions_db(db, repo, sessions_today=1, tokens=500)
    bus = Bus()
    got = {}

    async def cap(msg):
        got.update(msg)

    bus.subscribe(TOPIC_STATS, cap)
    cfg = HarnessConfig.from_dict(
        {"id": "projects", "type": "projects", "interval": 1.0,
         "repos": [str(repo)], "db_path": str(db)})
    h = ProjectsHarness(cfg, bus, TaskRegistry(bus))

    async def drive():
        return await h.poll_once()

    m = asyncio.run(drive())
    import time as _t
    _t.sleep(0.05)
    assert m["ok"] and len(m["projects"]) == 1
    assert got.get("harness") == "projects"
    assert got["metrics"]["projects"][0]["dirty"] == 2
    print("ok: ProjectsHarness.poll_once publishes project cards on the bus")


def _run():
    import tempfile
    test_missing_repo_degrades(Path(tempfile.mkdtemp()))
    d = Path(tempfile.mkdtemp())
    test_git_status(d)
    test_ahead_behind_with_upstream(Path(tempfile.mkdtemp()))
    test_session_join_by_cwd(Path(tempfile.mkdtemp()))
    test_session_join_prefix(Path(tempfile.mkdtemp()))
    test_no_state_db_no_crash(Path(tempfile.mkdtemp()))
    test_harness_poll_publishes(Path(tempfile.mkdtemp()))
    print("\nALL PROJECTS TESTS PASSED")


if __name__ == "__main__":
    _run()
