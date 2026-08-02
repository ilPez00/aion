"""Agent control: the rules, and the path from the web HUD to a cockpit.

The point of `agentctl` is that ONE module decides whether an action is legal.
The TUI keybindings and the web HUD both land in `store.control_task`, so the
tests that matter are (a) the state machine directly, and (b) that a request
made over the transport reaches the same decision.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion import agentctl  # noqa: E402
from aion.agentctl import ACTIONS, Outcome, legal  # noqa: E402


# ── the state machine ────────────────────────────────────────────────────────
@pytest.mark.parametrize("state", ["done", "failed", "cancelled", "interrupted"])
def test_finished_tasks_cannot_be_paused(state):
    ok, why = legal("pause", state)
    assert ok is False and state in why


def test_pause_only_when_running_and_not_already_paused():
    assert legal("pause", "running", paused=False)[0] is True
    assert legal("pause", "running", paused=True) == (False, "already paused")
    assert legal("pause", "pending")[0] is False


def test_resume_only_when_actually_paused():
    assert legal("resume", "running", paused=True)[0] is True
    assert legal("resume", "running", paused=False) == (False, "not paused")


@pytest.mark.parametrize("state", ["running", "pending"])
def test_live_work_can_be_cancelled(state):
    assert legal("cancel", state)[0] is True


@pytest.mark.parametrize("state", ["done", "failed", "cancelled", "interrupted"])
def test_finished_work_cannot_be_cancelled_again(state):
    ok, why = legal("cancel", state)
    assert ok is False and why == f"already {state}"


@pytest.mark.parametrize("state", ["interrupted", "cancelled", "failed"])
def test_dead_work_can_be_rerun(state):
    assert legal("rerun", state)[0] is True


def test_running_work_cannot_be_rerun_but_says_how():
    """A refusal that does not say what to do next is barely better than none."""
    ok, why = legal("rerun", "running")
    assert ok is False and "cancel it first" in why


def test_completed_work_is_not_rerunnable():
    """`done` is deliberately not in RERUNNABLE: re-running finished work on a
    button press is how you get two of something you wanted one of."""
    assert legal("rerun", "done")[0] is False


def test_unknown_action_is_refused_by_name():
    ok, why = legal("delete", "running")
    assert ok is False and "delete" in why


def test_every_action_refuses_with_a_reason():
    """A button that silently does nothing is the worst outcome available, so
    no code path may return False with an empty explanation."""
    states = ["pending", "running", "done", "failed", "cancelled", "interrupted"]
    for action in ACTIONS:
        for state in states:
            for paused in (True, False):
                ok, why = legal(action, state, paused)
                assert ok or why, f"{action}/{state}/paused={paused} refused silently"


def test_pause_and_resume_do_not_claim_a_state_change():
    """They flip a transient flag. Reporting a new state would make the graph
    redraw a node into a state the registry never entered."""
    assert agentctl.next_state("pause") is None
    assert agentctl.next_state("resume") is None
    assert agentctl.next_state("cancel") == "cancelled"


def test_outcome_serialises_the_reason():
    d = Outcome(False, "pause", "t1", "already paused", "running").as_dict()
    assert d == {"ok": False, "action": "pause", "task_id": "t1",
                 "reason": "already paused", "state": "running"}


# ── store integration ────────────────────────────────────────────────────────
@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A real Store with a real registry and one fake harness."""
    from aion.core import Bus, TaskRegistry, TaskState
    from aion.store import Store

    monkeypatch.setenv("AION_HOME", str(tmp_path))
    bus = Bus()
    registry = TaskRegistry(bus)

    class FakeHarness:
        id = "demo"
        name = "Demo"
        def __init__(self):
            self.calls = []
        def pause(self, task):
            self.calls.append(("pause", task.id)); task.paused = True
        def resume(self, task):
            self.calls.append(("resume", task.id)); task.paused = False
        def cancel(self, task):
            self.calls.append(("cancel", task.id))
            registry.set_state(task, TaskState.CANCELLED)
        async def run(self, task, prompt=""):
            self.calls.append(("run", prompt))

    s = Store.__new__(Store)
    s.bus, s.registry = bus, registry
    s.harnesses = {"demo": FakeHarness()}
    s._task_prompts = {}
    class _State:
        active_harness = "demo"
    s.state = _State()
    return s


# The store tests below run in a loop: TaskRegistry publishes every state
# change with asyncio.create_task. Not a test artefact -- control_task is only
# ever called from the cockpit's loop or from a transport handler.


def make_task(store, state="running"):
    from aion.core import TaskState
    task = store.registry.create("a task", "demo")
    store.registry.set_state(task, TaskState[state.upper()])
    return task


@pytest.mark.asyncio
async def test_control_task_pauses_a_running_task(store):
    task = make_task(store)
    out = store.control_task(task.id, "pause")
    assert out["ok"] is True
    assert ("pause", task.id) in store.harnesses["demo"].calls
    assert task.paused is True


@pytest.mark.asyncio
async def test_control_task_refuses_with_the_engine_reason(store):
    task = make_task(store, "done")
    out = store.control_task(task.id, "pause")
    assert out["ok"] is False
    assert "done" in out["reason"]
    assert store.harnesses["demo"].calls == [], "harness touched on a refusal"


@pytest.mark.asyncio
async def test_control_task_on_a_missing_task(store):
    out = store.control_task("t999", "cancel")
    assert out["ok"] is False and out["reason"] == "no such task"


@pytest.mark.asyncio
async def test_control_task_reports_the_new_state(store):
    task = make_task(store)
    assert store.control_task(task.id, "cancel")["state"] == "cancelled"


@pytest.mark.asyncio
async def test_control_task_logs_what_it_did(store):
    """The task log is where you look to find out why something stopped."""
    task = make_task(store)
    store.control_task(task.id, "cancel")
    assert any("cancelled" in line for line in task.log)


@pytest.mark.asyncio
async def test_control_task_refuses_an_unknown_action(store):
    task = make_task(store)
    out = store.control_task(task.id, "rm -rf")
    assert out["ok"] is False and store.harnesses["demo"].calls == []


@pytest.mark.asyncio
async def test_missing_harness_is_reported_not_crashed(store):
    task = store.registry.create("orphan", "gone")
    from aion.core import TaskState
    store.registry.set_state(task, TaskState.RUNNING)
    store.harnesses.clear()
    out = store.control_task(task.id, "pause")
    assert out["ok"] is False and "not loaded" in out["reason"]


@pytest.mark.asyncio
async def test_spawn_task_refuses_an_empty_prompt(store):
    assert store.spawn_task("demo", "   ")["ok"] is False


@pytest.mark.asyncio
async def test_spawn_task_refuses_an_unknown_harness(store):
    out = store.spawn_task("nope", "do a thing")
    assert out["ok"] is False and "nope" in out["reason"]


# ── routing a swarm action ───────────────────────────────────────────────────
# A swarm agent is not a process: it owns a task id, and the task is what a
# harness can suspend. `route` is the pure half of that — no store, no peer.
def test_agent_only_actions_stay_at_the_agent():
    for action in ("start", "cancel", "retry", "remove"):
        assert agentctl.route(action, "idle") == ("agent", "")


def test_pause_needs_a_working_agent():
    where, why = agentctl.route("pause", "idle", "running")
    assert where == "" and "idle" in why


def test_pause_refuses_an_agent_with_no_task_yet():
    """A remote spawn still in flight: WORKING, but nothing to pause. Saying
    so beats a button that silently does nothing."""
    where, why = agentctl.route("pause", "working", "")
    assert where == "" and "not attached to a task" in why


def test_pause_delegates_to_a_running_task():
    assert agentctl.route("pause", "working", "running") == ("task", "")


def test_delegation_inherits_the_task_rules_exactly():
    """No second state machine: an already-paused task refuses in the same
    words whether it was reached through an agent or directly."""
    assert agentctl.route("pause", "working", "running", paused=True) == (
        "", legal("pause", "running", paused=True)[1])
    assert agentctl.route("resume", "working", "running", paused=False) == (
        "", legal("resume", "running", paused=False)[1])


def test_resume_delegates_when_the_task_is_paused():
    assert agentctl.route("resume", "working", "running", paused=True) == ("task", "")


def test_every_agent_status_has_a_task_meaning():
    """The two vocabularies meet in AGENT_AS_TASK. A status added to the swarm
    without a mapping here would fall through as an unknown task state and be
    judged by rules that never saw it."""
    from aion.swarm import AgentStatus
    missing = [s.value for s in AgentStatus if s.value not in agentctl.AGENT_AS_TASK]
    assert missing == []
