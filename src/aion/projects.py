"""
projects.py — the Jarvis "Projects" workspace data layer.

Shows live status of the repos you actually work in, from ground-truth
sources (git itself + Hermes' session DB), never fakes:

  - branch, dirty-file count, ahead/behind vs upstream, last commit
  - open PR count (gh, optional, hard-timeout so CGNAT can't hang the UI)
  - last Hermes session active in the repo + tokens burned there today

projects.db is currently empty on this host, and sessions.git_repo_root is
unpopulated, so we key on the ONE column that IS populated — sessions.cwd —
matching a session to a repo when cwd is at/under the repo path. Repos come
from config (config/layout.json -> "projects": [...]) with a sane default
set. Everything runs through subprocess with short timeouts and degrades to
a partial card rather than crashing.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# repos to track if config doesn't override
DEFAULT_REPOS = [
    "/home/gio/cyclops",
    "/home/gio/aion",
    "/home/gio/praxis_webapp",
]

DEFAULT_DB = Path.home() / ".hermes" / "state.db"


@dataclass
class ProjectStatus:
    path: str
    name: str
    exists: bool = False
    is_git: bool = False
    branch: str = ""
    dirty: int = 0                 # count of modified/untracked entries
    ahead: int = 0
    behind: int = 0
    last_commit: str = ""          # "abc123 subject"
    last_commit_age_s: float | None = None
    open_prs: int | None = None    # None = not checked / gh unavailable
    last_session_id: str = ""
    last_session_age_s: float | None = None
    sessions_today: int = 0
    tokens_today: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path, "name": self.name, "exists": self.exists,
            "is_git": self.is_git, "branch": self.branch, "dirty": self.dirty,
            "ahead": self.ahead, "behind": self.behind,
            "last_commit": self.last_commit,
            "last_commit_age_s": self.last_commit_age_s,
            "open_prs": self.open_prs,
            "last_session_id": self.last_session_id,
            "last_session_age_s": self.last_session_age_s,
            "sessions_today": self.sessions_today,
            "tokens_today": self.tokens_today,
            "error": self.error,
        }


def _git(repo: str, args: list[str], timeout: float = 2.0) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None


class ProjectsReader:
    """Read-only status aggregator over git repos + Hermes sessions."""

    def __init__(self, repos: list[str] | None = None,
                 db_path: str | Path | None = None,
                 check_prs: bool = False, pr_timeout: float = 3.0) -> None:
        self.repos = repos or list(DEFAULT_REPOS)
        self.db_path = Path(db_path or DEFAULT_DB)
        self.check_prs = check_prs
        self.pr_timeout = pr_timeout

    # ---- per-repo git status --------------------------------------------
    def _repo_status(self, path: str) -> ProjectStatus:
        p = Path(path)
        name = p.name
        st = ProjectStatus(path=path, name=name, exists=p.exists())
        if not st.exists:
            return st
        # is it a git worktree?
        top = _git(path, ["rev-parse", "--show-toplevel"])
        if top is None:
            st.error = "not a git repo"
            return st
        st.is_git = True
        st.branch = _git(path, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""
        # dirty count (porcelain lines = modified + untracked)
        porc = _git(path, ["status", "--porcelain"])
        st.dirty = len([ln for ln in porc.splitlines() if ln.strip()]) if porc else 0
        # ahead/behind vs upstream (if an upstream is set)
        ab = _git(path, ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
        if ab and "\t" in ab:
            behind, ahead = ab.split("\t")[:2]
            try:
                st.behind, st.ahead = int(behind), int(ahead)
            except ValueError:
                pass
        # last commit: short hash + subject + relative age
        lc = _git(path, ["log", "-1", "--format=%h %s"])
        if lc:
            st.last_commit = lc[:72]
        ct = _git(path, ["log", "-1", "--format=%ct"])
        if ct and ct.isdigit():
            st.last_commit_age_s = time.time() - int(ct)
        # open PRs (optional, hard timeout — never hang the UI on CGNAT)
        if self.check_prs:
            st.open_prs = self._pr_count(path)
        return st

    def _pr_count(self, path: str) -> int | None:
        try:
            out = subprocess.run(
                ["gh", "pr", "list", "--state", "open", "--json", "number",
                 "-q", "length"],
                cwd=path, capture_output=True, text=True, timeout=self.pr_timeout,
            )
            if out.returncode != 0:
                return None
            s = out.stdout.strip()
            return int(s) if s.isdigit() else None
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None

    # ---- session join (by cwd prefix) -----------------------------------
    def _augment_with_sessions(self, statuses: list[ProjectStatus]) -> None:
        import sqlite3
        if not self.db_path.exists():
            return
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                   timeout=0.5)
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 400")
        except sqlite3.Error:
            return
        try:
            lt = time.localtime()
            midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                    0, 0, 0, 0, 0, -1))
            rows = conn.execute(
                """
                SELECT id, COALESCE(cwd,'') cwd, started_at,
                       COALESCE(input_tokens,0)+COALESCE(output_tokens,0)
                         +COALESCE(reasoning_tokens,0) AS toks
                FROM sessions
                WHERE cwd IS NOT NULL AND cwd != ''
                ORDER BY started_at DESC
                """
            ).fetchall()
        except sqlite3.Error:
            conn.close()
            return
        conn.close()
        now = time.time()
        # longest-path-first so /home/gio/aion beats /home/gio for a cwd
        ordered = sorted(statuses, key=lambda s: len(s.path), reverse=True)
        for sid, cwd, started, toks in rows:
            cwdp = cwd.rstrip("/")
            for st in ordered:
                base = st.path.rstrip("/")
                if cwdp == base or cwdp.startswith(base + "/"):
                    if not st.last_session_id:
                        st.last_session_id = sid
                        st.last_session_age_s = now - started
                    if started >= midnight:
                        st.sessions_today += 1
                        st.tokens_today += int(toks)
                    break

    def snapshot(self) -> list[ProjectStatus]:
        statuses = [self._repo_status(r) for r in self.repos]
        self._augment_with_sessions(statuses)
        return statuses

    def as_items(self) -> list[dict]:
        return [s.as_dict() for s in self.snapshot()]
