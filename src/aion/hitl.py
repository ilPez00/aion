"""hitl.py — human-in-the-loop approval gates (pure engine).

A gate is a privileged action an agent wants to take (run a destructive shell
command, overwrite a file, hit an external API) that pauses for a human yes/no.
The GateBook is the pure state machine; the store bridges it to Intents and the
HUD, exactly like fleet.py / factory.py sit behind their harnesses.

Two design rules, both fail-closed — a permission gate that defaults open is
worse than none:

  - is_safe() defaults to "nothing is auto-safe". Auto-approval only happens for
    actions an explicit policy vouches for.
  - wait() on a timeout REJECTS, never approves. A gate nobody answered stays
    denied.

The book is async-testable with no UI: request() returns a pending Gate, wait()
blocks on it, resolve() (or the Intent-driven resolve_latest()) releases it.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

RISK_LOW = "low"
RISK_MED = "med"
RISK_HIGH = "high"


@dataclass
class Gate:
    id: str
    task_id: str
    action: str                 # human-readable, e.g. "run: rm -rf build/"
    risk: str = RISK_MED
    created: float = field(default_factory=time.time)
    resolved: bool = False
    approved: bool = False
    auto: bool = False          # resolved by policy, not a human

    def as_dict(self) -> dict:
        return {
            "id": self.id, "task_id": self.task_id, "action": self.action,
            "risk": self.risk, "resolved": self.resolved,
            "approved": self.approved, "auto": self.auto,
        }


class GateBook:
    """Pending-approval registry. One book per cockpit (held by the Store)."""

    def __init__(self, is_safe: Callable[[str], bool] | None = None) -> None:
        # is_safe(action) -> True auto-approves without a human. Default: never.
        self.is_safe = is_safe or (lambda action: False)
        self._gates: dict[str, Gate] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._seq = 0

    def request(self, task_id: str, action: str, risk: str = RISK_MED) -> Gate:
        """Open a gate. If policy vouches for the action, it opens pre-approved."""
        self._seq += 1
        gid = f"g{self._seq:04d}"
        gate = Gate(id=gid, task_id=task_id, action=action, risk=risk)
        self._gates[gid] = gate
        if self.is_safe(action):
            gate.resolved = True
            gate.approved = True
            gate.auto = True
        else:
            self._events[gid] = asyncio.Event()
        return gate

    async def wait(self, gate: Gate, timeout: float | None = None) -> bool:
        """Block until the gate resolves; return whether it was approved.

        Auto-approved gates return immediately. A timeout rejects (fail-closed).
        """
        if gate.resolved:
            return gate.approved
        ev = self._events.get(gate.id)
        if ev is None:
            return gate.approved
        try:
            if timeout is not None:
                await asyncio.wait_for(ev.wait(), timeout)
            else:
                await ev.wait()
        except asyncio.TimeoutError:
            self.resolve(gate.id, approved=False)     # unanswered == denied
        # read the gate object directly: resolve() mutates this same instance,
        # and the store may have pruned it from _gates via clear_resolved().
        return gate.approved

    def resolve(self, gate_id: str, approved: bool) -> Gate | None:
        gate = self._gates.get(gate_id)
        if gate is None or gate.resolved:
            return None
        gate.resolved = True
        gate.approved = approved
        ev = self._events.pop(gate_id, None)
        if ev is not None:
            ev.set()
        return gate

    def resolve_latest(self, approved: bool) -> Gate | None:
        """Resolve the newest still-pending gate (what ACTIVATE/BACK target)."""
        for gate in reversed(list(self._gates.values())):
            if not gate.resolved:
                return self.resolve(gate.id, approved)
        return None

    def pending(self) -> list[Gate]:
        return [g for g in self._gates.values() if not g.resolved]

    def has_pending(self) -> bool:
        return any(not g.resolved for g in self._gates.values())

    def clear_resolved(self) -> None:
        """Drop resolved gates so the book doesn't grow unbounded."""
        self._gates = {k: g for k, g in self._gates.items() if not g.resolved}


class GateStore:
    """Publishes pending gates so other processes can SEE them.

    The web HUD runs in a different process from the cockpit, so a gate that
    exists only in `GateBook._gates` is invisible there — and an unnoticed
    gate is indistinguishable from a denial, because `wait()` is fail-closed.
    This writes the pending set to `~/.aion/instances/<id>/gates.json` on
    every change.

    THE FILE IS DISPLAY STATE, NOT A CONTROL CHANNEL
    ------------------------------------------------
    Nothing reads this file back into the book. Writing `"approved": true`
    into it approves nothing: the only thing that can release a gate is
    `GateBook.resolve()` running inside the cockpit, reached over the
    authenticated fleet transport. That asymmetry is deliberate — the file
    lives in the user's home directory with ordinary permissions, so if it
    were an approval channel then anything that could write a file could
    approve a destructive action. Reading is safe; writing must not be.

    `GateBook` stays a pure state machine; this is the only part that touches
    a disk.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from .fleet import instance_path
            path = instance_path("gates.json")
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def publish(self, book: "GateBook") -> None:
        """Write the pending set. Never raises into a harness loop."""
        from .fleet import write_json_atomic
        try:
            write_json_atomic(self.path, [g.as_dict() for g in book.pending()])
        except Exception as e:  # noqa: BLE001
            print(f"[hitl] publish failed: {e}")

    def read(self) -> list[dict]:
        """Pending gates as plain dicts. For DISPLAY in another process."""
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return [g for g in raw if isinstance(g, dict) and "id" in g] \
            if isinstance(raw, list) else []

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[hitl] clear failed: {e}")


def read_all_pending(root: Path | None = None) -> list[dict]:
    """Every pending gate across every instance on this machine.

    Used by the web HUD, which does not know or care which cockpit raised a
    gate — only that something is blocked waiting for a human.
    """
    import os
    root = root or (Path(os.environ.get("AION_HOME", os.path.expanduser("~/.aion")))
                    / "instances")
    out: list[dict] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        for g in GateStore(d / "gates.json").read():
            out.append({**g, "instance": d.name})
    return out
