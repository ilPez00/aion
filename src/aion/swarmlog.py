"""swarmlog.py — what happened, not just what is.

Everything durable about a swarm was a SNAPSHOT. `swarm.json` holds the DAG as
it stands, and each write replaces the last, so the file answers "what is the
state" and can never answer any of the questions that actually come up after a
run:

    how long did the scrape take?  did the writer retry, and how often?
    which step added these three I do not remember planning?
    was the budget spent before or after the failure?

The state was the only record, and state forgets. This is the other half: an
append-only event log, one line per transition, written beside the snapshot.

    started    a step got a task            finished   it produced output
    failed     it did not                   retry      it will run again, in Ns
    gave_up    it will not                  expanded   it added N steps
    cancelled  a human stopped it

Same shape as `hitl.AuditLog`, for the same reasons: appending cannot destroy
what came before, a line torn by a crash costs exactly that line, and the file
is evidence rather than a control channel — nothing reads it back into a swarm.

`timeline()` is the pure half: it folds those events into one row per step,
which is the form every reader wants and none of them should have to derive.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

__all__ = ["EventLog", "timeline", "render_timeline", "duration_text", "KINDS"]

KINDS = ("started", "finished", "failed", "retry", "gave_up", "expanded",
         "cancelled")

# Terminal outcomes, in the order that decides a row's verdict when a step
# somehow has more than one (a cancel racing a finish, say).
_END = ("gave_up", "failed", "cancelled", "finished")


class EventLog:
    """Append-only JSONL of swarm transitions."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from .fleet import instance_path
            path = instance_path("swarm-events.jsonl")
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def record(self, kind: str, step: str = "", **fields) -> None:
        """Append one event. Raises nothing a scheduler has to handle.

        A swarm that stops running because its log cannot be written would be
        a worse outcome than a swarm with an incomplete log, so failures print
        and return. They do not pass silently: an event log with unexplained
        holes is a record nobody can rely on.
        """
        entry = {"ts": time.time(), "kind": str(kind), "step": str(step)}
        entry.update(fields)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError) as e:
            print(f"[swarm] could not record {kind}: {e}")

    def read(self, limit: int = 200) -> list[dict]:
        """The most recent events, oldest first. Bad lines are skipped."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError):
            return []
        out: list[dict] = []
        for line in lines[-max(1, limit):]:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("kind"):
                out.append(d)
        return out


def timeline(events: Sequence[dict]) -> list[dict]:
    """One row per step: when it ran, how long, how many tries, how it ended.

    Duration is measured from the FIRST start to the last terminal event, so a
    step that failed twice and then succeeded reports the wall-clock cost of
    getting it done rather than the cost of the attempt that happened to work.
    That is the number a person is asking for when they ask how long a step
    took, and it is the one a per-attempt measure quietly understates.

    A step still running has `ended: None` and no duration — an unfinished row
    must not be filled in with "now", or a stalled step looks like a slow one.
    """
    rows: dict[str, dict] = {}
    order: list[str] = []
    for e in events:
        step = str(e.get("step") or "")
        if not step:
            continue
        kind = str(e.get("kind") or "")
        ts = float(e.get("ts") or 0.0)
        row = rows.get(step)
        if row is None:
            row = rows[step] = {"step": step, "started": None, "ended": None,
                                "attempts": 0, "outcome": "", "added": 0,
                                "error": ""}
            order.append(step)
        if kind == "started":
            row["attempts"] += 1
            if row["started"] is None:
                row["started"] = ts
            row["ended"] = None          # running again: the old end is stale
            row["outcome"] = ""
        elif kind == "expanded":
            row["added"] += int(e.get("count") or 0)
        elif kind in _END:
            # A later terminal event wins: a step that failed and then finished
            # on a retry ended by finishing.
            row["ended"] = ts
            row["outcome"] = kind
            if e.get("error"):
                row["error"] = str(e["error"])[:200]

    out = []
    for step in order:
        row = dict(rows[step])
        if row["started"] is not None and row["ended"] is not None:
            row["seconds"] = max(0.0, row["ended"] - row["started"])
        else:
            row["seconds"] = None
        out.append(row)
    out.sort(key=lambda r: (r["started"] is None, r["started"] or 0.0))
    return out


def duration_text(seconds) -> str:
    """How long, in the coarsest unit that still says something.

    `None` means the step has not ended, and the word for that is "running" —
    not "0s", which reads as a step that finished instantly.
    """
    if seconds is None:
        return "running"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def render_timeline(rows: Sequence[dict], theme: dict | None = None,
                    limit: int = 12) -> str:
    """The run, after the fact: what ran, in what order, for how long."""
    from .workflows import _theme

    th = _theme(theme)
    if not rows:
        return f"[{th['dim']}](nothing has run yet)[/]"
    glyph = {"finished": ("✓", "ok"), "failed": ("✗", "err"),
             "gave_up": ("✗", "err"), "cancelled": ("—", "dim")}
    lines = []
    for row in list(rows)[-limit:]:
        mark, key = glyph.get(row["outcome"], ("●", "warn"))
        col = th.get(key, th["dim"])
        tries = f" ×{row['attempts']}" if row["attempts"] > 1 else ""
        added = f" +{row['added']}" if row.get("added") else ""
        lines.append(f"  [{col}]{mark}[/] [{th['accent']}]{row['step'][:14]:14s}[/] "
                     f"[{th['dim']}]{duration_text(row['seconds']):>7s}"
                     f"{tries}{added}[/]")
    return "\n".join(lines)
