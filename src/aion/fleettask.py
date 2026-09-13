"""fleettask.py — read-only cockpit view over the fleet's durable task queue.

The queue itself lives in randomesh `scripts/fleet/agent-task.sh` under
`~/.local/state/randomesh/tasks/<id>/` (meta.json, task.md, log.txt). That
script owns durability: submit/run/reap/wait and the honest terminal states
(done / failed / timed-out / lost — a vanished process is "lost", not "failed").

This module owns *viewing*: parse `agent-task.sh status [--all]` tables and
read meta.json dirs directly, so the Fleet workspace can render one session
table across the fleet. It never mutates queue state (no refresh, no reap —
those are the queue's job), never shells out on import, and takes paths and
script output as arguments so every branch unit-tests without a fleet.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Statuses the queue reports. Terminal set must match agent-task.sh refresh().
TERMINAL = ("done", "failed", "timed-out", "lost")
LIVE = ("queued", "running")

DEFAULT_SCRIPT = os.path.expanduser("~/dev/randomesh/scripts/fleet/agent-task.sh")


@dataclass
class FleetTask:
    """One row of the cross-fleet session table (Agent-Deck-shaped)."""
    id: str = ""
    title: str = ""
    status: str = ""
    engine: str = ""
    rc: str = ""
    host: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status,
                "engine": self.engine, "rc": self.rc, "host": self.host,
                "terminal": self.terminal}


def _parse_row(line: str, host: str) -> FleetTask | None:
    """Parse one fixed-width status row. Pure; None for header/filler.

    Columns follow the script's `printf '  %-24s %-10s %-34s %-4s %s'`
    layout, so fields are sliced by position — an empty RC column collapses
    under split() and eats the title's first word. Short/ragged lines fall
    back to whitespace split.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("ID") or stripped.startswith("("):
        return None
    if len(line) >= 78:
        task_id, status = line[2:26].strip(), line[27:37].strip()
        engine, rc = line[38:72].strip(), line[73:77].strip()
        title = line[78:].strip()
    else:
        parts = stripped.split(None, 4)
        if len(parts) < 2:
            return None
        task_id, status = parts[0], parts[1]
        engine = parts[2] if len(parts) > 2 else ""
        rc = parts[3] if len(parts) > 3 and parts[3].lstrip("-").isdigit() else ""
        title = parts[4] if len(parts) > 4 else (
            "" if rc else (parts[3] if len(parts) > 3 else ""))
    if not task_id or status not in TERMINAL + LIVE + ("?",):
        return None
    return FleetTask(id=task_id, title=title, status=status,
                     engine=engine, rc=rc, host=host)


def parse_status(text: str, host: str = "") -> list[FleetTask]:
    """Parse `agent-task.sh status` (one host). Never raises on weird lines."""
    tasks: list[FleetTask] = []
    for line in text.splitlines():
        try:
            row = _parse_row(line, host)
        except Exception:
            continue
        if row is not None:
            tasks.append(row)
    return tasks


def parse_status_all(text: str) -> list[FleetTask]:
    """Parse `agent-task.sh status --all`: `== host ==` sections tag rows.

    Unreachable hosts report `(unreachable)` and yield no rows — absence of a
    signal is not a signal, so they are simply absent, never "all done".
    """
    tasks: list[FleetTask] = []
    host = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("==") and s.endswith("=="):
            host = s.strip("= ").strip()
            continue
        try:
            row = _parse_row(line, host)
        except Exception:
            continue
        if row is not None:
            tasks.append(row)
    return tasks


def read_task_dir(path: str | Path) -> FleetTask | None:
    """Read one task dir's meta.json straight. No refresh, no mutation."""
    try:
        m = json.loads(Path(path, "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(m, dict) or not m.get("id"):
        return None
    rc = m.get("rc")
    return FleetTask(id=str(m["id"]), title=str(m.get("title", "")),
                     status=str(m.get("status", "?")),
                     engine=str(m.get("engine", "")),
                     rc="" if rc is None else str(rc),
                     host=str(m.get("host", "")))


def read_local_tasks(state_root: str | Path | None = None) -> list[FleetTask]:
    """All task dirs under a state root (default: this host's queue)."""
    root = Path(state_root or Path.home() / ".local/state/randomesh/tasks")
    tasks: list[FleetTask] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for d in entries:
        if d.is_dir():
            t = read_task_dir(d)
            if t is not None:
                tasks.append(t)
    return tasks


def submit_queued(task_text: str, *, title: str = "", engine: str = "shell",
                  cwd: str = ".", model: str = "",
                  script: str = DEFAULT_SCRIPT,
                  env: dict | None = None) -> str:
    """Queue one task WITHOUT starting it (no --run). Returns the task id.

    Starting work is an operator action (cockpit control / mesh CLI), never a
    side effect of viewing. `env` may override HOME to isolate the queue.
    """
    cmd = [script, "submit", task_text, "--title", title or task_text[:60],
           "--engine", engine, "--cwd", cwd]
    if model:
        cmd += ["--model", model]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                       env=env)
    if p.returncode != 0:
        raise RuntimeError(f"agent-task submit failed: {p.stderr.strip()[-300:]}")
    return p.stdout.strip().splitlines()[-1].strip()
