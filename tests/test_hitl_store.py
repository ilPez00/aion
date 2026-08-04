"""HITL store integration — real Store, gate capture, gated spawn.

Complements test_hitl.py (the pure GateBook). Here the gate is driven through
the Store the way the cockpit drives it: destructive prompts open a gate, the
ACTIVATE/BACK that any device emits resolves it, and _gated_run runs or cancels.
"""
import asyncio
import types

import pytest

from aion.core import Bus, Intent, IntentType, TaskRegistry, TaskState, load_config
from aion.store import Store
from aion.harnesses import build_harnesses


def _store() -> Store:
    cfg = load_config()
    bus = Bus()
    registry = TaskRegistry(bus)
    harnesses = build_harnesses(cfg["harnesses"], bus, registry)
    return Store(cfg, bus, harnesses=harnesses)


class _FakeH:
    """Minimal harness stand-in: records whether run() was reached."""
    def __init__(self, extra=None, name="fake"):
        self.name = name
        self.cfg = types.SimpleNamespace(extra=extra or {})
        self.ran = False

    async def run(self, task, prompt):
        self.ran = True


# ── _needs_gate ────────────────────────────────────────────────────────────
def test_needs_gate_on_destructive_prompt():
    s = _store()
    assert s._needs_gate(_FakeH(), "please rm -rf build/") is True
    assert s._needs_gate(_FakeH(), "drop table users") is True
    assert s._needs_gate(_FakeH(), "git push --force origin main") is True


def test_needs_gate_false_for_benign_prompt():
    assert _store()._needs_gate(_FakeH(), "summarise the readme") is False


def test_needs_gate_honours_requires_approval_flag():
    s = _store()
    assert s._needs_gate(_FakeH(extra={"requires_approval": True}), "hello") is True


# ── gate capture in handle() ────────────────────────────────────────────────
def test_activate_approves_pending_gate():
    # bare task_id: _resolve_gate null-guards a missing task, so no registry
    # create (which would schedule a bus publish and need a running loop).
    s = _store()
    g = s.gates.request("t1", "danger")
    s.handle(Intent(IntentType.ACTIVATE))
    assert g.resolved and g.approved and not s.gates.has_pending()
    assert s.state.stats["hitl"] == []          # published empty after resolve


def test_back_rejects_pending_gate():
    s = _store()
    g = s.gates.request("t1", "danger")
    s.handle(Intent(IntentType.BACK))
    assert g.resolved and not g.approved


def test_hitl_intents_no_op_without_a_gate():
    s = _store()
    # must not raise and must not fall through to normal activate handling
    s.handle(Intent(IntentType.HITL_APPROVE))
    s.handle(Intent(IntentType.HITL_REJECT))
    assert not s.gates.has_pending()


# ── _gated_run full path ─────────────────────────────────────────────────────
def test_gated_run_rejected_cancels_task():
    async def scenario():
        s = _store()
        h = _FakeH()
        task = s.registry.create("x", "fake")
        run = asyncio.create_task(s._gated_run(h, task, "rm -rf build"))
        await asyncio.sleep(0.02)
        assert s.gates.has_pending()                 # waiting on a human
        s.handle(Intent(IntentType.BACK))            # reject
        await run
        assert h.ran is False
        assert task.state is TaskState.CANCELLED
    asyncio.run(scenario())


def test_gated_run_approved_runs_task():
    async def scenario():
        s = _store()
        h = _FakeH()
        task = s.registry.create("x", "fake")
        run = asyncio.create_task(s._gated_run(h, task, "rm -rf build"))
        await asyncio.sleep(0.02)
        s.handle(Intent(IntentType.ACTIVATE))        # approve
        await run
        assert h.ran is True
    asyncio.run(scenario())


def test_gated_run_benign_prompt_runs_without_a_gate():
    async def scenario():
        s = _store()
        h = _FakeH()
        task = s.registry.create("y", "fake")
        await s._gated_run(h, task, "summarise the readme")
        assert h.ran is True and not s.gates.has_pending()
    asyncio.run(scenario())


# ── the durable record ──────────────────────────────────────────────────────
def test_a_keypress_approval_lands_in_the_log(tmp_path):
    """The task log dies with the process and the gate is dropped by
    `clear_resolved()`. This is the copy that outlives both."""
    from aion.hitl import AuditLog

    s = _store()
    s._audit_log = AuditLog(tmp_path / "approvals.jsonl")
    s.gates.request("t1", "run rm -rf build")
    s.handle(Intent(IntentType.ACTIVATE))

    entries = s.approval_log()
    assert len(entries) == 1
    assert entries[0]["decision"] == "approved"
    assert entries[0]["by"] == "cockpit"
    assert entries[0]["action"] == "run rm -rf build"


def test_a_remote_answer_is_recorded_as_remote(tmp_path):
    from aion.hitl import AuditLog

    s = _store()
    s._audit_log = AuditLog(tmp_path / "approvals.jsonl")
    g = s.gates.request("t1", "deploy to prod")
    s.resolve_gate_by_id(g.id, True)
    assert s.approval_log()[0]["by"] == "remote"


def test_the_record_outlives_the_gate(tmp_path):
    """`clear_resolved()` runs on every resolution — the point of the log is
    that the evidence does not go with it."""
    from aion.hitl import AuditLog

    s = _store()
    s._audit_log = AuditLog(tmp_path / "approvals.jsonl")
    s.gates.request("t1", "danger")
    s.handle(Intent(IntentType.BACK))
    assert s.gates.pending() == [] and s.gates._gates == {}
    assert s.approval_log()[0]["decision"] == "rejected"
