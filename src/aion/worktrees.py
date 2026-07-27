"""worktrees.py — git repositories and their worktrees, as graph data.

Why aion cares
--------------
Worktrees are the natural unit of *agent isolation*: give each autonomous loop
its own checkout and two agents can work the same repo without fighting over
the index. The question an operator then has is structural — which agent is in
which tree, which trees are dirty, which are stale leftovers nobody pruned —
and that is a graph:

    repo ── worktree ── branch
                └── the task currently working in it

Pure and testable
-----------------
`parse_worktree_list` and `link_tasks` take text/data and return data, so the
interesting logic is covered without a git fixture. Only the thin
`_git()` wrapper touches a subprocess.

Locale and locks
----------------
Output is parsed by **porcelain key**, never by message text: this box reports
git in Italian, so `prunable`'s human-readable reason is not a stable thing to
match on. `git` is invoked with `LC_ALL=C` anyway for the non-porcelain paths,
and with `GIT_OPTIONAL_LOCKS=0` so merely looking at a repo never takes a lock
out from under an agent that is mid-commit.

Read-only
---------
Nothing here creates, moves or prunes a worktree. Adding a worktree is a
write to somebody's repo; the HUD is a viewer, and a viewer that can mutate
your checkouts is a different security question entirely.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT_TIMEOUT = 8.0
MAX_REPOS = 40
# Directories that never contain a repo worth showing, and are expensive to
# walk. Mirrors fsgraph.IGNORE_DIRS in spirit.
SKIP_DIRS = {
    "node_modules", ".venv", "venv", "target", "dist", "build", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".cache", "site-packages", ".tox",
}


class GitError(RuntimeError):
    """git missing, timed out, or refused. Callers degrade, never crash."""


@dataclass
class Worktree:
    path: str
    head: str = ""
    branch: str = ""          # short name; empty when detached
    bare: bool = False
    detached: bool = False
    locked: bool = False
    prunable: bool = False
    is_main: bool = False
    dirty: int = 0            # changed paths, -1 when not probed
    ahead: int = 0
    behind: int = 0
    tasks: list[str] = field(default_factory=list)   # task ids working here

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def state(self) -> str:
        """One word for the UI. Ordered by what most wants attention."""
        if self.prunable:
            return "prunable"
        if self.locked:
            return "locked"
        if self.dirty > 0:
            return "dirty"
        if self.detached:
            return "detached"
        return "clean"


def _git(args: list[str], cwd: str | Path | None = None) -> str:
    """Run git and return stdout. Never shells out through a shell."""
    env = {
        **os.environ,
        "LC_ALL": "C",              # stable output for anything non-porcelain
        "GIT_OPTIONAL_LOCKS": "0",  # looking must not lock a busy repo
        "GIT_TERMINAL_PROMPT": "0",  # never block waiting for credentials
    }
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=GIT_TIMEOUT,
                           env=env)
    except FileNotFoundError as e:
        raise GitError("git is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git timed out after {GIT_TIMEOUT}s") from e
    if p.returncode != 0:
        raise GitError((p.stderr or p.stdout or "git failed").strip()[:200])
    return p.stdout


def parse_worktree_list(text: str) -> list[Worktree]:
    """Parse `git worktree list --porcelain`.

    Records are blank-line separated; each line is `key` or `key value`. The
    first record is always the main worktree. Unknown keys are ignored rather
    than treated as errors, so a newer git that adds an attribute does not
    break the parse.
    """
    out: list[Worktree] = []
    cur: Worktree | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur = Worktree(path=val, is_main=not out)
            out.append(cur)
        elif cur is None:
            continue
        elif key == "HEAD":
            cur.head = val
        elif key == "branch":
            # refs/heads/foo/bar -> foo/bar
            cur.branch = val[len("refs/heads/"):] if val.startswith("refs/heads/") else val
        elif key == "bare":
            cur.bare = True
        elif key == "detached":
            cur.detached = True
        elif key == "locked":
            cur.locked = True       # the value is a localised reason; ignore it
        elif key == "prunable":
            cur.prunable = True     # ditto
    return out


def worktrees_of(repo: str | Path) -> list[Worktree]:
    return parse_worktree_list(_git(["worktree", "list", "--porcelain"], cwd=repo))


def probe_status(wt: Worktree) -> Worktree:
    """Fill in dirty/ahead/behind for one worktree. Best effort.

    A prunable worktree's directory is gone, so probing it would just raise;
    it keeps `dirty=-1` meaning "not measured" rather than a misleading 0.
    """
    wt.dirty = -1
    if wt.prunable or wt.bare or not Path(wt.path).is_dir():
        return wt
    try:
        porcelain = _git(["status", "--porcelain"], cwd=wt.path)
        wt.dirty = sum(1 for line in porcelain.splitlines() if line.strip())
    except GitError:
        return wt
    try:
        counts = _git(["rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
                      cwd=wt.path).split()
        if len(counts) == 2:
            wt.behind, wt.ahead = int(counts[0]), int(counts[1])
    except (GitError, ValueError):
        pass        # no upstream configured is the common, uninteresting case
    return wt


def find_repos(root: str | Path, *, depth: int = 3,
               max_repos: int = MAX_REPOS) -> list[Path]:
    """Breadth-first hunt for git repositories under `root`.

    BFS for the same reason fsgraph walks that way: one enormous subtree must
    not eat the whole budget and hide its siblings. A directory containing
    `.git` is a repo and is not descended into — worktrees are found through
    git itself, not by walking.
    """
    root = Path(root)
    found: list[Path] = []
    frontier = [(root, 0)]
    while frontier and len(found) < max_repos:
        nxt: list[tuple[Path, int]] = []
        for d, lvl in frontier:
            if (d / ".git").exists():
                found.append(d)
                continue                      # do not descend into a repo
            if lvl >= depth:
                continue
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if not e.name.startswith(".") and e.name not in SKIP_DIRS:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            nxt.append((Path(e.path), lvl + 1))
                    except OSError:
                        continue
        frontier = nxt
    return found[:max_repos]


def link_tasks(worktrees: list[Worktree], tasks: list[dict]) -> list[Worktree]:
    """Attach tasks to the worktree they are plausibly working in.

    aion does not record a task's working directory, so this matches the
    worktree's path or directory name against the task's label and log. It is
    a heuristic and is deliberately conservative — matching on the full path
    or on the directory name as a whole word, never on a bare substring, so
    a worktree called `api` does not claim every task that says "api".
    """
    for wt in worktrees:
        name = wt.name.lower()
        path = wt.path.lower()
        for t in tasks:
            blob = f"{t.get('label', '')} {' '.join(t.get('log') or [])}".lower()
            if path in blob or _word_in(name, blob):
                wt.tasks.append(t.get("id", "?"))
    return worktrees


def _word_in(word: str, text: str) -> bool:
    """Whole-token match, so `api` does not match `rapid`."""
    if not word:
        return False
    import re
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text) is not None


def graph(root: str | Path, *, depth: int = 3, probe: bool = True,
          tasks: list[dict] | None = None) -> dict:
    """Repos and their worktrees, ready for the HUD adapter.

    A repo that cannot be read (permissions, corrupt, git missing) contributes
    an `error` entry instead of vanishing — a repo silently missing from the
    view is worse than one shown as broken.
    """
    found = find_repos(root, depth=depth)

    def scan_one(rp: Path) -> dict:
        entry = {"path": str(rp), "name": rp.name, "worktrees": [], "error": None}
        try:
            wts = worktrees_of(rp)
            if probe:
                wts = [probe_status(w) for w in wts]
            if tasks:
                wts = link_tasks(wts, tasks)
            entry["worktrees"] = [
                {**vars(w), "name": w.name, "state": w.state} for w in wts]
        except GitError as e:
            entry["error"] = str(e)
        return entry

    # Each repo costs 2-3 subprocess round trips, and they are pure I/O wait —
    # serially that measured 4.1s across 40 repos, which is far too slow for a
    # view someone taps into. Threads (not processes) because the work is
    # `subprocess.run` blocking on a pipe, so the GIL is released throughout.
    if len(found) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(16, len(found))) as pool:
            repos = list(pool.map(scan_one, found))
    else:
        repos = [scan_one(rp) for rp in found]

    total = sum(len(r["worktrees"]) for r in repos)
    return {
        "root": str(root),
        "repos": repos,
        "summary": {
            "repos": len(repos),
            "worktrees": total,
            "dirty": sum(1 for r in repos for w in r["worktrees"] if w["state"] == "dirty"),
            "prunable": sum(1 for r in repos for w in r["worktrees"] if w["prunable"]),
            "errors": sum(1 for r in repos if r["error"]),
        },
    }


def search(query: str, snap: dict | None = None, limit: int = 20) -> list[dict]:
    """Palette hits for repos, worktrees and branches."""
    q = (query or "").strip().lower()
    if not q or snap is None:
        return []
    hits: list[dict] = []
    for r in snap.get("repos", []):
        if q in r["name"].lower():
            hits.append({"type": "repo", "label": r["name"], "sub": r["path"],
                         "module": "repos", "node": f"r{r['path']}"})
        for w in r["worktrees"]:
            if q in w["name"].lower() or q in (w["branch"] or "").lower():
                hits.append({"type": "worktree",
                             "label": w["branch"] or w["name"],
                             "sub": f"{w['state']} · {w['path']}",
                             "module": "repos", "node": f"w{w['path']}"})
    return hits[:limit]
