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

    def __init__(self, is_safe: Callable[[str], bool] | None = None,
                 audit: Callable[[dict], None] | None = None) -> None:
        # is_safe(action) -> True auto-approves without a human. Default: never.
        self.is_safe = is_safe or (lambda action: False)
        # audit(entry) -> None. Injected, because this class is a pure state
        # machine and the durable record is a file. Default: record nothing,
        # which is what every existing caller already got.
        self.audit = audit or (lambda entry: None)
        self._gates: dict[str, Gate] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._seq = 0

    def _record(self, gate: Gate, by: str) -> None:
        """Write the decision down BEFORE anything acts on it.

        Ordering is the point: `resolve()` sets the event that releases a task
        blocked in `wait()`, and a crash between releasing and recording leaves
        an action performed with nothing saying who allowed it. Recording first
        can at worst leave a decision on record that never took effect, which
        is the harmless direction.
        """
        try:
            self.audit({
                "ts": time.time(), "gate": gate.id, "task": gate.task_id,
                "action": gate.action, "risk": gate.risk,
                "decision": "approved" if gate.approved else "rejected",
                "auto": gate.auto, "by": by,
            })
        except Exception as e:  # noqa: BLE001
            # A gate must still resolve when the audit sink is broken — a
            # cockpit that deadlocks on a full disk is worse than one with a
            # gap in its log. Loud, though: a silent gap is the failure this
            # whole file exists to prevent.
            print(f"[hitl] audit failed for {gate.id}: {e}")

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
            # Audited like any other approval, and it is the MOST important
            # kind to have on record: nobody was asked, so the log is the only
            # evidence the action was ever allowed.
            self._record(gate, by="policy")
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
            # Unanswered == denied, and recorded as such. A denial nobody made
            # is exactly the entry a later "why did this not run" needs.
            self.resolve(gate.id, approved=False, by="timeout")
        # read the gate object directly: resolve() mutates this same instance,
        # and the store may have pruned it from _gates via clear_resolved().
        return gate.approved

    def resolve(self, gate_id: str, approved: bool,
                by: str = "human") -> Gate | None:
        """Answer a gate. `by` is WHO — that is most of what an audit is for.

        "cockpit" (a keypress here), "remote" (the HUD over the authenticated
        fleet transport), "policy" (is_safe vouched, nobody was asked) and
        "timeout" (fail-closed) are decisions with very different weight, and a
        log that flattens them answers "was this approved" but not "by whom".
        """
        gate = self._gates.get(gate_id)
        if gate is None or gate.resolved:
            return None
        gate.resolved = True
        gate.approved = approved
        self._record(gate, by=by)
        ev = self._events.pop(gate_id, None)
        if ev is not None:
            ev.set()
        return gate

    def resolve_latest(self, approved: bool, by: str = "human") -> Gate | None:
        """Resolve the newest still-pending gate (what ACTIVATE/BACK target)."""
        for gate in reversed(list(self._gates.values())):
            if not gate.resolved:
                return self.resolve(gate.id, approved, by=by)
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


class AuditLog:
    """Every gate decision, on disk, append-only.

    Until this existed, "who approved what" was answerable for about as long as
    the process lived: the decision went into a task's in-memory log, the gate
    itself was dropped by `clear_resolved()`, and `gates.json` only ever holds
    what is still PENDING — so the moment a gate was answered, the answer
    stopped existing anywhere durable. For a fleet that runs privileged actions
    on other people's machines, that is the one record worth keeping.

    Append-only JSONL, deliberately:

      * A log that gets rewritten is not evidence of anything. `write_json_atomic`
        replaces a file wholesale, which is right for display state and wrong
        here — one bad write would take the history with it.
      * A torn line from a crash costs exactly that line. `read()` skips what it
        cannot parse rather than refusing to show the rest.
      * Ordering is arrival order, which is what a reader wants.

    Like `GateStore`, this is display/evidence state and never a control
    channel: nothing reads it back into a `GateBook`, so writing "approved"
    into the file approves nothing.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from .fleet import instance_path
            path = instance_path("approvals.jsonl")
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def record(self, entry: dict) -> None:
        """Append one decision. Raises nothing a caller has to handle."""
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[hitl] could not record approval: {e}")

    def read(self, limit: int = 50) -> list[dict]:
        """The most recent decisions, newest last. Bad lines are skipped."""
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
                continue        # a torn write costs its own line, nothing more
            if isinstance(d, dict) and "gate" in d:
                out.append(d)
        return out


def read_all_recent(root: Path | None = None, limit: int = 50) -> list[dict]:
    """Recent gate decisions across every instance on this machine.

    The fleet question is "what has been approved anywhere", so entries carry
    the instance they came from and are merged by time.
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
        for e in AuditLog(d / "approvals.jsonl").read(limit=limit):
            out.append({**e, "instance": d.name})
    out.sort(key=lambda e: float(e.get("ts") or 0))
    return out[-max(1, limit):]


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
