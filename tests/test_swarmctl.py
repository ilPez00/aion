"""Swarm control: per-agent verbs over a dependency DAG.

`swarm run` / `swarm stop` were all-or-nothing. These add start / cancel /
retry / remove for one agent, which means the interesting question stops being
"what state is this in" and becomes "what upstream of it is in the way" — so
most of what is tested here is that a refusal names the dependency.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.swarm import AgentStatus, SwarmOrchestrator  # noqa: E402


@pytest.fixture()
def swarm():
    """scout → writer → editor, a chain so blocking actually propagates."""
    o = SwarmOrchestrator()
    o.add_agent("scout", "find sources")
    o.add_agent("writer", "draft it", deps=["scout"])
    o.add_agent("editor", "polish it", deps=["writer"])
    return o


def by_name(o, name):
    return o.agent_by_name(name)


# ── readiness vs blocked: they used to contradict each other ─────────────────
def test_a_failed_dependency_no_longer_counts_as_satisfied(swarm):
    """agents_ready() asked `_is_done`, which means "reached a terminal state",
    so a failed dependency read as satisfied and `swarm run` would start a step
    whose input never arrived — while blocked_agents() called the very same
    agent blocked."""
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.FAILED)
    ready = [a.name for a in swarm.agents_ready()]
    blocked = [a.name for a in swarm.blocked_agents()]
    assert "writer" not in ready
    assert "writer" in blocked
    assert not (set(ready) & set(blocked)), "an agent is both ready and blocked"


def test_a_cancelled_dependency_also_blocks(swarm):
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.CANCELLED)
    assert [a.name for a in swarm.blocked_agents()] == ["writer"]


def test_a_done_dependency_satisfies(swarm):
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.DONE)
    assert [a.name for a in swarm.agents_ready()] == ["writer"]
    assert swarm.blocked_agents() == []


def test_a_missing_dependency_blocks_and_says_so(swarm):
    o = SwarmOrchestrator()
    o.add_agent("writer", "draft", deps=["ghost"])
    kind, why = o.dep_state(by_name(o, "writer"))
    assert kind == "blocked" and "ghost" in why


def test_waiting_is_distinct_from_blocked(swarm):
    """An unstarted dependency is not a problem, it is a queue position. Calling
    it blocked would send you looking for a failure that never happened."""
    kind, why = swarm.dep_state(by_name(swarm, "writer"))
    assert kind == "waiting" and "scout" in why


def test_dep_state_reports_non_idle_agents_as_such(swarm):
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.WORKING)
    assert swarm.dep_state(by_name(swarm, "scout"))[0] == "not-idle"


# ── per-agent actions ────────────────────────────────────────────────────────
def test_start_an_agent_with_no_dependencies(swarm):
    out = swarm.control(by_name(swarm, "scout").id, "start")
    assert out["ok"] is True and out["state"] == "working"


def test_start_refuses_and_names_the_blocking_dependency(swarm):
    out = swarm.control(by_name(swarm, "writer").id, "start")
    assert out["ok"] is False
    assert "scout" in out["reason"], "a refusal that does not name the blocker"


def test_start_refuses_when_upstream_failed(swarm):
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.FAILED)
    out = swarm.control(by_name(swarm, "writer").id, "start")
    assert out["ok"] is False and "failed" in out["reason"]


def test_start_only_from_idle(swarm):
    a = by_name(swarm, "scout")
    swarm.set_status(a.id, AgentStatus.WORKING)
    assert swarm.control(a.id, "start")["ok"] is False


def test_cancel_a_working_agent(swarm):
    a = by_name(swarm, "scout")
    swarm.set_status(a.id, AgentStatus.WORKING)
    assert swarm.control(a.id, "cancel")["state"] == "cancelled"


def test_cancel_refuses_on_finished_work(swarm):
    a = by_name(swarm, "scout")
    swarm.set_status(a.id, AgentStatus.DONE)
    out = swarm.control(a.id, "cancel")
    assert out["ok"] is False and "already done" in out["reason"]


def test_retry_puts_a_failed_agent_back_to_idle_not_working(swarm):
    """Its dependencies may have changed since it failed, and `start` is what
    checks them. Jumping straight to working would skip that."""
    a = by_name(swarm, "scout")
    swarm.set_status(a.id, AgentStatus.FAILED)
    assert swarm.control(a.id, "retry")["state"] == "idle"
    assert a.status is AgentStatus.IDLE


def test_retry_clears_the_previous_failure(swarm):
    a = by_name(swarm, "scout")
    a.error, a.progress = "boom", 0.7
    swarm.set_status(a.id, AgentStatus.FAILED)
    swarm.control(a.id, "retry")
    assert a.error == "" and a.progress == 0.0 and a.completed is None


def test_retry_refuses_on_a_healthy_agent(swarm):
    assert swarm.control(by_name(swarm, "scout").id, "retry")["ok"] is False


# ── removal and the DAG ──────────────────────────────────────────────────────
def test_remove_refuses_while_something_depends_on_it(swarm):
    """Dependencies are by NAME. Deleting the node they point at does not
    rewrite them — it silently makes them unsatisfiable forever."""
    out = swarm.control(by_name(swarm, "scout").id, "remove")
    assert out["ok"] is False and "writer" in out["reason"]
    assert by_name(swarm, "scout") is not None


def test_remove_a_leaf_works(swarm):
    out = swarm.control(by_name(swarm, "editor").id, "remove")
    assert out["ok"] is True
    assert by_name(swarm, "editor") is None


def test_removing_a_leaf_frees_its_parent(swarm):
    swarm.control(by_name(swarm, "editor").id, "remove")
    assert swarm.control(by_name(swarm, "writer").id, "remove")["ok"] is True


def test_unknown_agent_and_action(swarm):
    assert swarm.control("nope", "start")["reason"] == "no such agent"
    assert "unknown action" in swarm.control(by_name(swarm, "scout").id, "explode")["reason"]


# ── bulk ─────────────────────────────────────────────────────────────────────
def test_run_ready_starts_only_what_can_run(swarm):
    out = swarm.run_ready()
    assert out["started"] == ["scout"]
    assert by_name(swarm, "writer").status is AgentStatus.IDLE


def test_run_ready_reports_what_it_could_not_start_and_why(swarm):
    """The blocked list is the point of a DAG view — "nothing happened" hides
    exactly the thing you opened it to find out."""
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.FAILED)
    out = swarm.run_ready()
    assert out["started"] == []
    names = {b["name"]: b["reason"] for b in out["blocked"]}
    assert "writer" in names and "scout" in names["writer"]
    assert "nothing is ready" in out["reason"]


def test_run_ready_advances_the_chain_one_layer_at_a_time(swarm):
    swarm.run_ready()
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.DONE)
    assert swarm.run_ready()["started"] == ["writer"]


def test_stop_all_cancels_only_live_work(swarm):
    scout, writer = by_name(swarm, "scout"), by_name(swarm, "writer")
    swarm.set_status(scout.id, AgentStatus.WORKING)
    out = swarm.stop_all()
    assert out["stopped"] == ["scout"]
    assert writer.status is AgentStatus.IDLE, "an idle agent was cancelled"


# ── adding ───────────────────────────────────────────────────────────────────
def test_add_requires_a_name_and_a_goal(swarm):
    assert swarm.add_checked("", "x")["ok"] is False
    assert swarm.add_checked("x", "  ")["ok"] is False


def test_add_refuses_a_duplicate_name(swarm):
    """Dependencies are by name, so two agents called "scout" make every
    dependency on that name a coin flip."""
    out = swarm.add_checked("scout", "another one")
    assert out["ok"] is False and "already exists" in out["reason"]


def test_add_refuses_a_dependency_that_does_not_exist(swarm):
    out = swarm.add_checked("publisher", "ship it", ["ghost"])
    assert out["ok"] is False and "ghost" in out["reason"]
    assert by_name(swarm, "publisher") is None


def test_add_accepts_real_dependencies(swarm):
    out = swarm.add_checked("publisher", "ship it", ["editor"])
    assert out["ok"] is True
    assert by_name(swarm, "publisher").dependencies == ["editor"]


def test_added_agent_is_immediately_part_of_the_dag(swarm):
    swarm.add_checked("publisher", "ship it", ["editor"])
    assert swarm.dep_state(by_name(swarm, "publisher"))[0] == "waiting"


# ── store dispatch ───────────────────────────────────────────────────────────
@pytest.fixture()
def store(swarm):
    """A Store stripped to the swarm surface, with a runner that records
    spawns instead of starting harnesses. These tests are about DISPATCH —
    which verb reaches which engine call — not about execution."""
    from aion.store import Store
    from aion.swarmrun import SwarmRunner
    s = Store.__new__(Store)
    s.swarm = swarm
    s.spawned = []
    s._swarm_runner = SwarmRunner(
        swarm,
        spawn=lambda agent, prompt: (s.spawned.append(agent.name)
                                     or f"task{len(s.spawned)}"),
        harness="demo")
    return s


def test_store_dispatches_every_verb(store, swarm):
    """run_ready now SPAWNS rather than flipping a status flag."""
    assert store.swarm_command({"action": "run_ready"})["started"] == ["scout"]
    assert store.spawned == ["scout"]
    aid = by_name(swarm, "scout").id
    assert store.swarm_command({"action": "cancel", "agent_id": aid})["ok"] is True
    assert store.swarm_command({"action": "retry", "agent_id": aid})["ok"] is True
    assert store.swarm_command({"action": "start", "agent_id": aid})["ok"] is True
    assert store.spawned == ["scout", "scout"]


def test_status_reports_why_a_swarm_is_stuck(store, swarm):
    store.swarm_command({"action": "run_ready"})
    st = store.swarm_command({"action": "status"})
    assert st["in_flight"] == 1 and st["total"] == 3


def test_store_refuses_an_unknown_verb(store):
    assert "unknown swarm action" in store.swarm_command({"action": "nuke"})["reason"]


def test_store_requires_an_agent_for_per_agent_verbs(store):
    assert store.swarm_command({"action": "start"})["reason"] == "no agent"


def test_store_rejects_a_malformed_deps_field(store):
    """The body comes from a browser; deps is the one field that is a list."""
    out = store.swarm_command({"action": "add", "name": "x", "goal": "y",
                               "deps": "scout"})
    assert out["ok"] is False and "list" in out["reason"]


def test_store_survives_a_non_dict_body(store):
    assert store.swarm_command(None)["ok"] is False


def test_swarm_outcomes_say_agent_id_not_task_id(swarm):
    """Outcome carries its subject as `task_id`; these are agents, not tasks.
    Answering with a task id that is not a task reads fine until somebody
    writes a client against it."""
    for out in (swarm.control(by_name(swarm, "scout").id, "start"),
                swarm.add_checked("x", "y"),
                swarm.add_checked("", "")):
        assert "task_id" not in out
        assert "agent_id" in out


def test_remove_refusal_is_grammatical_for_one_dependent(swarm):
    out = swarm.control(by_name(swarm, "writer").id, "remove")
    assert "editor depends on it" in out["reason"]


def test_remove_refusal_lists_several_dependents(swarm):
    swarm.add_checked("archivist", "file it", ["scout"])
    out = swarm.control(by_name(swarm, "scout").id, "remove")
    assert "archivist, writer depend on it" in out["reason"]
