"""
core.py — the spine of aion.

Everything else talks through three things defined here:

  Intent   : a device-agnostic action ("navigate left", "activate", "command")
  Bus      : a tiny async pub/sub so harnesses can stream stats/tasks/logs
             without the UI ever blocking on work
  TaskRegistry : the source of truth for "what is running" (progress bars)

Plus Config (loads config/layout.json) and a few shared enums.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Awaitable, Callable


# --------------------------------------------------------------------------
# Intents — the universal input language
# --------------------------------------------------------------------------
class IntentType(Enum):
    NAVIGATE = auto()          # payload: {"dir": "up"|"down"|"left"|"right"}
    ACTIVATE = auto()          # confirm / select
    BACK = auto()              # escape / deselect
    CONTEXT = auto()           # right-click / menu
    SCROLL = auto()            # payload: {"delta": int}
    SELECT = auto()            # payload: {"id": str}
    COMMAND = auto()           # payload: {"text": str}
    MODE_TOGGLE = auto()       # payload: {"mode": str}  e.g. "voice"
    SWITCH_WORKSPACE = auto()  # payload: {"index": int} or {"delta": int}
    PAUSE = auto()             # pause focused running task
    RESUME = auto()            # resume focused paused task
    CANCEL = auto()            # cancel focused task
    RERUN = auto()             # re-run focused interrupted/cancelled/failed task


@dataclass
class Intent:
    type: IntentType
    payload: dict = field(default_factory=dict)

    # constructor helpers keep call sites readable
    @classmethod
    def navigate(cls, direction: str) -> "Intent":
        return cls(IntentType.NAVIGATE, {"dir": direction})

    @classmethod
    def activate(cls) -> "Intent":
        return cls(IntentType.ACTIVATE)

    @classmethod
    def back(cls) -> "Intent":
        return cls(IntentType.BACK)

    @classmethod
    def context(cls) -> "Intent":
        return cls(IntentType.CONTEXT)

    @classmethod
    def switch_workspace(cls, index: int | None = None, delta: int | None = None) -> "Intent":
        return cls(IntentType.SWITCH_WORKSPACE, {"index": index, "delta": delta})

    @classmethod
    def command(cls, text: str) -> "Intent":
        return cls(IntentType.COMMAND, {"text": text})

    @classmethod
    def mode_toggle(cls, mode: str) -> "Intent":
        return cls(IntentType.MODE_TOGGLE, {"mode": mode})


# --------------------------------------------------------------------------
# Bus — async pub/sub. Harnesses publish; UI & stats subscribe.
# --------------------------------------------------------------------------
TOPIC_INTENT = "intent"
TOPIC_TASK = "task"      # payload: {"action": str, "task": Task}
TOPIC_STATS = "stats"    # payload: {"harness": str, "metrics": dict}
TOPIC_LOG = "log"        # payload: {"task_id": str, "line": str}
TOPIC_MODE = "mode"      # payload: {"mode": str, "active": bool}
TOPIC_VOICE = "voice"    # payload: {"text": str, "event": str}


class Bus:
    def __init__(self) -> None:
        self._subs: dict[str, set[Callable]] = defaultdict(set)

    def subscribe(self, topic: str, cb: Callable[[Any], Awaitable[None]]) -> Callable:
        self._subs[topic].add(cb)
        return lambda: self._subs[topic].discard(cb)

    async def publish(self, topic: str, msg: Any) -> None:
        for cb in list(self._subs.get(topic, ())):
            result = cb(msg)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)


# --------------------------------------------------------------------------
# Tasks — the thing that makes progress bars real
# --------------------------------------------------------------------------
class TaskState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"   # was running/pending when the session died


@dataclass
class Task:
    id: str
    label: str
    harness: str
    state: TaskState = TaskState.PENDING
    progress: float = 0.0          # 0.0 .. 1.0
    eta: float | None = None       # seconds remaining (estimate)
    subtasks: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    paused: bool = False           # transient: harness loop is suspended

    def as_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "harness": self.harness,
            "state": self.state.value, "progress": round(self.progress, 3),
            "eta": self.eta, "log": self.log[-50:],
        }


class TaskRegistry:
    """Single source of truth for running work. UI subscribes via the bus."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.tasks: dict[str, Task] = {}
        self._seq = 0

    def create(self, label: str, harness: str) -> Task:
        self._seq += 1
        tid = f"t{self._seq:04d}"
        t = Task(id=tid, label=label, harness=harness)
        self.tasks[tid] = t
        asyncio.create_task(self.bus.publish(TOPIC_TASK, {"action": "create", "task": t}))
        return t

    def set_state(self, task: Task, state: TaskState) -> None:
        task.state = state
        asyncio.create_task(self.bus.publish(TOPIC_TASK, {"action": "update", "task": task}))

    def set_progress(self, task: Task, value: float, eta: float | None = None) -> None:
        task.progress = max(0.0, min(1.0, value))
        if eta is not None:
            task.eta = eta
        asyncio.create_task(self.bus.publish(TOPIC_TASK, {"action": "update", "task": task}))

    def log(self, task: Task, line: str) -> None:
        task.log.append(line)
        asyncio.create_task(self.bus.publish(TOPIC_LOG, {"task_id": task.id, "line": line}))

    def cancel(self, task: Task) -> None:
        self.set_state(task, TaskState.CANCELLED)

    def ingest(self, task: Task) -> None:
        """Re-register a task loaded from disk (e.g. after a crash)."""
        self.tasks[task.id] = task
        # keep the sequence counter ahead of any restored ids
        try:
            n = int(task.id.lstrip("t")) if task.id.startswith("t") else 0
            self._seq = max(self._seq, n)
        except ValueError:
            pass


class SessionStore:
    """Crash-safe persistence for the task registry (lesson #1).

    The TaskRegistry keeps tasks in memory; this dumps them to
    ~/.aion/session.json on every change and reloads on launch. Tasks that
    were still RUNNING/PENDING when we died are marked INTERRUPTED (we can't
    resurrect the coroutine) so the user can re-run them.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or (Path.home() / ".aion" / "session.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, tasks: dict[str, Task]) -> None:
        try:
            data = [t.as_dict() for t in tasks.values()]
            self.path.write_text(json.dumps(data, indent=2))
        except Exception as e:  # noqa: BLE001
            print(f"[session] save failed: {e}")

    def load(self) -> list[Task]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            return []
        out: list[Task] = []
        for d in raw:
            try:
                st = TaskState(d.get("state", "interrupted"))
                if st in (TaskState.RUNNING, TaskState.PENDING):
                    st = TaskState.INTERRUPTED
                out.append(Task(
                    id=d["id"], label=d["label"], harness=d["harness"],
                    state=st, progress=d.get("progress", 0.0),
                    log=d.get("log", []),
                ))
            except Exception:  # noqa: BLE001
                continue
        return out


# --------------------------------------------------------------------------
# Config — loads config/layout.json, falls back to sane defaults
# --------------------------------------------------------------------------
DEFAULT_LAYOUT = {
    "app_name": "aion",
    "theme": {
        "accent": "#5ad1ff",
        "ok": "#7CFFB2",
        "warn": "#FFD479",
        "err": "#FF6B6B",
        "dim": "#5a6b7b",
    },
    "workspaces": [
        {"id": "models", "title": "Models",    "icon": "◈"},
        {"id": "tasks",  "title": "Tasks",     "icon": "▤"},
        {"id": "agent",  "title": "Agent",     "icon": "✦"},
    ],
    "keybindings": {
        "workspace_1": "1", "workspace_2": "2", "workspace_3": "3",
        "nav_up": ["up", "k"], "nav_down": ["down", "j"],
        "nav_left": ["left", "h"], "nav_right": ["right", "l"],
        "activate": ["enter", "space"], "back": ["escape", "b"],
        "context": ["c"], "palette": "ctrl+k", "voice": "v",
    },
    "harnesses": [
        {"id": "demo",   "type": "demo",   "name": "Demo Harness",   "enabled": True,  "vram_mb": 0},
        {"id": "shell",  "type": "shell",  "name": "Shell Agent",    "enabled": True,  "vram_mb": 0,  "command": "echo step {n}: working && sleep 0.3"},
        {"id": "cyclops","type": "cyclops","name": "Cyclops Agent",  "enabled": True,  "vram_mb": 1200},
    ],
}


def load_config(path: str | Path | None = None) -> dict:
    if path is None:
        # repo layout: .../aion/src/aion/core.py  ->  config at repo root /config
        path = Path(__file__).resolve().parents[2] / "config" / "layout.json"
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            # shallow-merge onto defaults so missing keys stay valid
            cfg = {**DEFAULT_LAYOUT, **data}
            cfg["theme"] = {**DEFAULT_LAYOUT["theme"], **data.get("theme", {})}
            cfg["workspaces"] = data.get("workspaces", DEFAULT_LAYOUT["workspaces"])
            cfg["keybindings"] = data.get("keybindings", DEFAULT_LAYOUT["keybindings"])
            cfg["harnesses"] = data.get("harnesses", DEFAULT_LAYOUT["harnesses"])
            return cfg
        except Exception as e:  # noqa: BLE001
            print(f"[config] failed to load {p}: {e}; using defaults")
    return DEFAULT_LAYOUT
