"""task_board.py — mesh agent-task board (AION-002).

Answers "what are the agents doing" as an always-on panel.

Mechanism (documented choice): read each task state dir's meta.json
(~/.local/state/randomesh/tasks/<id>/meta.json) directly — files, not
shelling out to `mesh task status`. Same journal the queue itself writes,
so the board cannot disagree with the queue; no SSH in the UI thread
(local dirs only — remote nodes surface when their state syncs here).

Pure engine (TaskBoard over list[dict]) + pure render. Polling lives in
the harness (app background collector), never here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..fleet import TaskRow

# Harness poll cadence. Never faster than MIN_POLL_S: the queue journals,
# re-reading it more often burns disk for identical bytes.
POLL_INTERVAL_S = 30.0
MIN_POLL_S = 15.0

WIDTH = 54


def _age_label(submitted_at: str) -> str:
    """Relative age from an ISO-8601 stamp. Garbage → '?' (never crash)."""
    try:
        ts = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return "?"
    if delta < 0:
        return "0s"
    if delta < 60:
        return f"{delta:.0f}s"
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    if delta < 86400:
        return f"{delta / 3600:.0f}h"
    return f"{delta / 86400:.0f}d"


@dataclass
class BoardRow:
    task: TaskRow
    age: str = ""


@dataclass
class TaskBoard:
    """Grouped view over queue rows. Loud (lost/timed-out) first, then recency."""
    rows: list = field(default_factory=list)  # list[BoardRow]

    @classmethod
    def from_dicts(cls, items: list | None) -> "TaskBoard":
        from ..fleet import FleetView
        view = FleetView.from_tasks(items)
        return cls(rows=[BoardRow(task=t, age=_age_label(t.submitted_at))
                         for t in view.tasks])

    def by_node(self) -> dict:
        grouped: dict = {}
        for r in self.rows:
            grouped.setdefault(r.task.host or "?", []).append(r)
        return grouped

    def running_only(self) -> "TaskBoard":
        return TaskBoard(rows=[r for r in self.rows
                               if r.task.status in ("running", "queued", "pending")])

    @property
    def loud(self) -> list:
        return [r for r in self.rows if r.task.loud]

    def summary(self) -> str:
        n_run = sum(1 for r in self.rows
                    if r.task.status in ("running", "queued", "pending"))
        return (f"{len(self.rows)} tasks · {n_run} active · "
                f"{len(self.loud)} need attention")


def render_task_board(board: TaskBoard, theme: dict,
                      running_only: bool = False) -> str:
    """Pure render. Missing data degrades to a placeholder row."""
    di = theme.get("dim", "#9aabbb")
    a = theme.get("accent", "#5ad1ff")
    warn = theme.get("warn", "#FFD479")
    err = theme.get("err", "#FF8A8A")
    rows = board.running_only().rows if running_only else board.rows
    out = [f"[{a}]▦ TASK BOARD[/]  [{di}]{board.summary()}[/]"]
    if not rows:
        out.append(f"  [{di}]queue empty — submit with mesh task submit[/]")
        return "\n".join(out)
    last_node = None
    for r in rows:
        t = r.task
        node = t.host or "?"
        if node != last_node:
            out.append(f"  [{di}]— {node} —[/]")
            last_node = node
        rc = f" rc={t.rc}" if t.rc not in (None, 0) else ""
        if t.loud:
            out.append(f"    [{err}]◆ {t.id} {t.status.upper()} {r.age} — {t.title}{rc}[/]")
        elif t.status in ("running", "queued", "pending"):
            out.append(f"    [{warn}]● {t.id} {t.status} {r.age} — {t.title}[/]")
        else:
            out.append(f"    [{di}]· {t.id} {t.status} {r.age} — {t.title}{rc}[/]")
    return "\n".join(out)
