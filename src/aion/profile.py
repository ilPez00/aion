"""
profile.py — who is this computer for, and what should aion watch.

First-run onboarding: the desktop DATA panel asks the user to declare the
scope of their computer use (`setup dev writing media ...`). aion then scans
the disk in a background thread and generates *live trackers* — small
scope-specific metrics (git repos, document counts, media/download sizes,
disk headroom) refreshed on an interval and rendered on the dashboard.

Persisted at ~/.aion/profile.json:
    {"scopes": [...], "scanned_at": ..., "disk": [...], "trackers": [...]}

Each tracker keeps the previous value so the dashboard can show a delta
arrow (the "live" part). Pure stdlib; scans are budgeted, never recursive
without a depth cap, and swallow permission errors.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

PROFILE_PATH = Path.home() / ".aion" / "profile.json"
RESCAN_AFTER_S = 600          # dashboard-triggered background refresh
_SKIP = {".cache", ".venv", "node_modules", "__pycache__", ".local",
         ".cargo", ".rustup", ".npm", ".git"}

SCOPES: dict[str, str] = {
    "dev": "code + git repos",
    "writing": "documents, notes, LaTeX",
    "media": "pictures / video / music",
    "data": "downloads + datasets",
    "comms": "mail + messaging",
    "finance": "invoices + accounting",
}


# ---- persistence ---------------------------------------------------------
def load(path: Path | str | None = None) -> dict | None:
    p = Path(path) if path else PROFILE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(profile: dict, path: Path | str | None = None) -> None:
    p = Path(path) if path else PROFILE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile, indent=2), encoding="utf-8")


# ---- disk scan (budgeted) ------------------------------------------------
def _dir_size(root: Path, max_entries: int = 20000) -> int:
    """Approximate recursive size; stops after max_entries files."""
    total, seen = 0, 0
    stack = [root]
    while stack and seen < max_entries:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen >= max_entries:
                        break
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir():
                            if e.name not in _SKIP:
                                stack.append(Path(e.path))
                        else:
                            total += e.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _count_files(root: Path, exts: tuple[str, ...], max_depth: int = 3) -> int:
    n = 0
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        if len(Path(dirpath).parts) - base >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
        n += sum(1 for f in filenames if f.lower().endswith(exts))
    return n


def _find_git_repos(home: Path, max_depth: int = 3) -> list[Path]:
    repos = []
    base = len(home.parts)
    for dirpath, dirnames, _ in os.walk(home):
        p = Path(dirpath)
        if len(p.parts) - base >= max_depth:
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            repos.append(p)
            dirnames[:] = []      # don't descend into a repo
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
    return repos


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# ---- tracker generation --------------------------------------------------
def _build_trackers(scopes: list[str], home: Path) -> list[dict]:
    """One pass over the declared scopes -> list of tracker dicts."""
    tr: list[dict] = []

    def add(tid: str, label: str, value: float, unit: str = ""):
        tr.append({"id": tid, "label": label, "value": value, "unit": unit})

    if "dev" in scopes:
        repos = _find_git_repos(home)
        add("repos", "git repos", len(repos))
        recent = sum(1 for r in repos
                     if (r / ".git").exists()
                     and time.time() - (r / ".git").stat().st_mtime < 7 * 86400)
        add("repos_active", "active repos (7d)", recent)
    if "writing" in scopes:
        docs = home / "Documents"
        if docs.exists():
            add("docs_size", "Documents", _dir_size(docs), "bytes")
        add("notes", "md/tex files", _count_files(home, (".md", ".tex")))
    if "media" in scopes:
        size = sum(_dir_size(home / d) for d in ("Pictures", "Videos", "Music")
                   if (home / d).exists())
        add("media_size", "media", size, "bytes")
    if "data" in scopes:
        dl = home / "Downloads"
        if dl.exists():
            add("dl_size", "Downloads", _dir_size(dl), "bytes")
    # always-on: disk headroom
    try:
        st = os.statvfs(home)
        add("disk_free", "disk free", st.f_bavail * st.f_frsize, "bytes")
    except OSError:
        pass
    return tr


def scan(scopes: list[str], home: Path | str | None = None,
         prev: dict | None = None) -> dict:
    """Full profile build: top-level dir sizes + scope trackers.

    prev (the last saved profile) supplies previous tracker values so the
    dashboard can render deltas. Runs in a worker thread — pure CPU/IO,
    no state mutation.
    """
    home = Path(home) if home else Path.home()
    disk = []
    try:
        with os.scandir(home) as it:
            dirs = [e for e in it if e.is_dir(follow_symlinks=False)
                    and not e.name.startswith(".")]
    except OSError:
        dirs = []
    for e in dirs:
        disk.append({"name": e.name, "size": _dir_size(Path(e.path))})
    disk.sort(key=lambda d: d["size"], reverse=True)

    trackers = _build_trackers(scopes, home)
    prev_vals = {t["id"]: t["value"] for t in (prev or {}).get("trackers", [])}
    for t in trackers:
        t["prev"] = prev_vals.get(t["id"], t["value"])

    return {"scopes": scopes, "scanned_at": time.time(),
            "disk": disk[:8], "trackers": trackers}


def tracker_line(t: dict) -> str:
    """'label value [delta-arrow]' — plain text, UI adds color."""
    if t.get("unit") == "bytes":
        val = human_size(t["value"])
        delta = t["value"] - t.get("prev", t["value"])
        arrow = f" ↑{human_size(delta)}" if delta > 1024 * 1024 else \
                f" ↓{human_size(-delta)}" if delta < -1024 * 1024 else ""
    else:
        val = f"{int(t['value'])}"
        d = int(t["value"] - t.get("prev", t["value"]))
        arrow = f" ↑{d}" if d > 0 else f" ↓{-d}" if d < 0 else ""
    return f"{t['label']} {val}{arrow}"
