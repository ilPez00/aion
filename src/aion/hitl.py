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
import time
from dataclasses import dataclass, field
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
        return self._gates[gate.id].approved

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
