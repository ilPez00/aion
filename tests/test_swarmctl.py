"""Swarm control: per-agent verbs over a dependency DAG.

`swarm run` / `swarm stop` were all-or-nothing. These add start / cancel /
retry / remove for one agent, which means the interesting question stops being
"what state is this in" and becomes "what upstream of it is in the way" — so
most of what is tested here is that a refusal names the dependency.
"""
from __future__ import annotations

import asyncio
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


# ── pause/resume: delegated to the task the agent owns ───────────────────────
# An agent has no execution of its own. These verbs exist at the task layer
# already, so the swarm does not reimplement them -- it finds the task and
# applies exactly the rules `control_task` enforces for anyone else.
@pytest.fixture()
def live(swarm, tmp_path, monkeypatch):
    """A Store with a real registry, one fake harness, and the swarm above."""
    from aion.core import Bus, TaskRegistry, TaskState
    from aion.store import Store
    from aion.swarmrun import SwarmRunner

    monkeypatch.setenv("AION_HOME", str(tmp_path))
    bus = Bus()
    registry = TaskRegistry(bus)

    class FakeHarness:
        id = name = "demo"
        vram_mb = 0
        def __init__(self):
            self.calls = []
        def pause(self, task):
            self.calls.append(("pause", task.id)); task.paused = True
        def resume(self, task):
            self.calls.append(("resume", task.id)); task.paused = False
        def cancel(self, task):
            self.calls.append(("cancel", task.id))
            registry.set_state(task, TaskState.CANCELLED)

    s = Store.__new__(Store)
    s.bus, s.registry, s.swarm = bus, registry, swarm
    s.harnesses = {"demo": FakeHarness()}
    s._task_prompts = {}
    class _State:
        def __init__(self):
            self.active_harness = "demo"
            self.history = []
            self.swarm_dashboard = ""
            self.swarm_plan = {}
    s.state = _State()

    def spawn(agent, prompt):
        task = registry.create(agent.name, "demo")
        registry.set_state(task, TaskState.RUNNING)
        return task.id

    s._swarm_runner = SwarmRunner(swarm, spawn=spawn, harness="demo")
    return s


@pytest.mark.asyncio
async def test_pausing_an_agent_pauses_its_real_task(live, swarm):
    live.swarm_command({"action": "run_ready"})
    aid = by_name(swarm, "scout").id
    out = live.swarm_command({"action": "pause", "agent_id": aid})
    assert out["ok"] is True and out["agent_id"] == aid
    assert live.harnesses["demo"].calls[-1][0] == "pause"


@pytest.mark.asyncio
async def test_resume_uses_the_task_rules_not_a_second_copy(live, swarm):
    live.swarm_command({"action": "run_ready"})
    aid = by_name(swarm, "scout").id
    assert live.swarm_command({"action": "resume", "agent_id": aid})["reason"] == (
        "not paused")
    live.swarm_command({"action": "pause", "agent_id": aid})
    assert live.swarm_command({"action": "resume", "agent_id": aid})["ok"] is True
    assert live.harnesses["demo"].calls[-1][0] == "resume"


@pytest.mark.asyncio
async def test_an_idle_agent_cannot_be_paused(live, swarm):
    out = live.swarm_command({"action": "pause",
                              "agent_id": by_name(swarm, "scout").id})
    assert out["ok"] is False and "idle" in out["reason"]
    assert live.harnesses["demo"].calls == []


@pytest.mark.asyncio
async def test_pause_says_so_when_the_agent_owns_no_task_yet(live, swarm):
    """WORKING with nothing attached is the remote-spawn-in-flight window.
    Refusing with a reason beats a button that appears to do nothing."""
    from aion.swarm import AgentStatus
    aid = by_name(swarm, "scout").id
    swarm.set_status(aid, AgentStatus.WORKING)
    out = live.swarm_command({"action": "pause", "agent_id": aid})
    assert out["ok"] is False and "not attached to a task" in out["reason"]


@pytest.mark.asyncio
async def test_pause_records_itself_in_the_agent_log(live, swarm):
    live.swarm_command({"action": "run_ready"})
    aid = by_name(swarm, "scout").id
    live.swarm_command({"action": "pause", "agent_id": aid})
    assert any("paused" in line for line in swarm.agents[aid].logs)


@pytest.mark.asyncio
async def test_pause_of_an_unknown_agent_is_refused_not_crashed(live):
    assert live.swarm_command({"action": "pause", "agent_id": "ghost"})["ok"] is False


# ── the same verb, one machine away ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_pausing_a_remote_step_travels_to_the_peer(live, swarm, monkeypatch):
    """The task lives on the other instance, so the local registry is the wrong
    place to look and the wrong place to act."""
    from aion.swarmrun import Watch
    import aion.remotes as remotes

    aid = by_name(swarm, "scout").id
    from aion.swarm import AgentStatus
    swarm.set_status(aid, AgentStatus.WORKING)
    live._swarm_runner.task_of[aid] = "t-remote"
    live._swarm_runner.agent_of["t-remote"] = aid
    live._swarm_runner.watches[aid] = Watch(aid, "workstation", "t-remote",
                                            state="running")
    monkeypatch.setattr(live, "_peer_node", lambda inst: object(), raising=False)
    sent = []

    async def fake_control(self, node, task_id, action):
        sent.append((task_id, action))
        return {"ok": True}

    monkeypatch.setattr(remotes.RemoteClient, "control_task", fake_control)
    out = live.swarm_command({"action": "pause", "agent_id": aid})
    assert out["ok"] is True and out["pending"] is True
    await asyncio.sleep(0)          # let the dispatched request run
    assert sent == [("t-remote", "pause")]
    assert live.harnesses["demo"].calls == []
    assert any("workstation" in line for line in swarm.agents[aid].logs)


@pytest.mark.asyncio
async def test_a_peer_that_left_the_fleet_refuses_rather_than_pretending(live, swarm):
    from aion.swarmrun import Watch
    from aion.swarm import AgentStatus

    aid = by_name(swarm, "scout").id
    swarm.set_status(aid, AgentStatus.WORKING)
    live._swarm_runner.task_of[aid] = "t-remote"
    live._swarm_runner.watches[aid] = Watch(aid, "gone-box", "t-remote",
                                            state="running")
    live._peer_node = lambda inst: None
    out = live.swarm_command({"action": "pause", "agent_id": aid})
    assert out["ok"] is False and "gone-box" in out["reason"]


@pytest.mark.asyncio
async def test_a_refusal_from_the_peer_reaches_the_agent_log(live, swarm, monkeypatch):
    import aion.remotes as remotes
    from aion.swarmrun import Watch
    from aion.swarm import AgentStatus

    aid = by_name(swarm, "scout").id
    swarm.set_status(aid, AgentStatus.WORKING)
    live._swarm_runner.task_of[aid] = "t-remote"
    live._swarm_runner.watches[aid] = Watch(aid, "workstation", "t-remote",
                                            state="running")
    monkeypatch.setattr(live, "_peer_node", lambda inst: object(), raising=False)

    async def refuse(self, node, task_id, action):
        return {"ok": False, "reason": "already paused"}

    monkeypatch.setattr(remotes.RemoteClient, "control_task", refuse)
    live.swarm_command({"action": "pause", "agent_id": aid})
    await asyncio.sleep(0)
    assert any("refused pause" in line for line in swarm.agents[aid].logs)


def test_a_remote_poll_records_the_state_it_saw(swarm):
    """Pausing a remote step is judged against the last poll, so the poll has
    to keep it. Before, only the miss counter survived."""
    from aion.swarmrun import Watch, read_poll
    w = Watch("a1", "workstation", "t1")
    read_poll({"state": "running", "paused": True}, w)
    assert (w.state, w.paused) == ("running", True)


# ── one set of rules, not two ────────────────────────────────────────────────
def test_agent_cancel_uses_the_task_terminal_states(swarm):
    from aion import agentctl
    for state in (AgentStatus.DONE, AgentStatus.FAILED, AgentStatus.CANCELLED):
        aid = swarm.add_agent(f"x{state.value}", "g").id
        swarm.set_status(aid, state)
        assert swarm.can(aid, "cancel") == agentctl.legal(
            "cancel", agentctl.AGENT_AS_TASK[state.value])


def test_agent_retry_matches_the_task_rerun_set(swarm):
    from aion import agentctl
    for state in AgentStatus:
        aid = swarm.add_agent(f"r{state.value}", "g").id
        swarm.set_status(aid, state)
        assert swarm.can(aid, "retry")[0] is (state.value in agentctl.RERUNNABLE)


def test_delegated_verbs_answer_a_missing_agent_like_the_others(live):
    assert live.swarm_command({"action": "pause"})["reason"] == "no agent"


# ── the typed command and the HUD button are the same verb ───────────────────
# `swarm run` in the cockpit used to set every ready agent to WORKING and spawn
# nothing, so a DAG typed here sat at layer one forever while the identical DAG
# driven from the HUD ran. Both now land in `swarm_command`.
@pytest.mark.asyncio
async def test_typed_swarm_run_spawns_real_tasks(live, swarm):
    await live._swarm_command("swarm run")
    aid = by_name(swarm, "scout").id
    assert live._swarm_runner.task_of.get(aid), "run started no task"
    assert any("scout" in line for line in live.state.history)


@pytest.mark.asyncio
async def test_typed_swarm_run_says_why_nothing_started(live, swarm):
    swarm.set_status(by_name(swarm, "scout").id, AgentStatus.FAILED)
    await live._swarm_command("swarm run")
    assert any("scout" in line for line in live.state.history)


@pytest.mark.asyncio
async def test_typed_swarm_add_refuses_a_duplicate_name(live, swarm):
    """Skipping add_checked here let two agents share a name, which makes every
    dependency on that name a coin flip."""
    await live._swarm_command("swarm add scout something else")
    assert len([a for a in swarm.agents.values() if a.name == "scout"]) == 1
    assert any("already exists" in line for line in live.state.history)


@pytest.mark.asyncio
async def test_typed_swarm_add_still_parses_dependencies(live, swarm):
    await live._swarm_command("swarm add publisher ship it << editor")
    assert by_name(swarm, "publisher").dependencies == ["editor"]


@pytest.mark.asyncio
async def test_typed_swarm_create_twice_does_not_collide(live, swarm):
    await live._swarm_command("swarm create research a thing")
    await live._swarm_command("swarm create research another thing")
    names = sorted(a.name for a in swarm.agents.values() if a.name.startswith("Agent"))
    assert names == ["Agent", "Agent-2"]


@pytest.mark.asyncio
async def test_typed_swarm_create_leaves_the_agent_idle(live, swarm):
    """It used to be set WORKING with no task behind it — a lie the dashboard
    then displayed as a running agent."""
    await live._swarm_command("swarm create research a thing")
    assert by_name(swarm, "Agent").status is AgentStatus.IDLE


@pytest.mark.asyncio
async def test_typed_swarm_stop_takes_the_tasks_with_it(live, swarm):
    await live._swarm_command("swarm run")
    await live._swarm_command("swarm stop")
    assert live.harnesses["demo"].calls[-1][0] == "cancel"
    assert live._swarm_runner.task_of == {}


@pytest.mark.asyncio
async def test_typed_swarm_status_stores_the_shape_every_view_reads(live, swarm):
    """`swarm status` used to store the legacy TEXT dashboard while the bus
    stored a dict. Every consumer — panel, HUD, jarvis — reads the dict, so
    the command whose entire job is "show me the swarm" made the view worse
    than not running it, and the panel raised AttributeError on `.get`."""
    await live._swarm_command("swarm status")
    d = live.state.swarm_dashboard
    assert isinstance(d, dict)
    assert d["total"] == len(swarm.agents)
    assert {a["name"] for a in d["agents"]} == {a.name for a in swarm.agents.values()}


@pytest.mark.asyncio
async def test_typed_swarm_status_also_says_it_in_words(live):
    await live._swarm_command("swarm status")
    assert any("swarm:" in h for h in live.state.history)


# ── `swarm plan` / `swarm apply` in the terminal ─────────────────────────────
# The planner existed and only the browser could reach it, so the cockpit —
# the surface people actually use — had the worst way to build a DAG: one
# `swarm add ... << deps` line per step, in an order `add_checked` accepts.

def _fake_plan(monkeypatch, steps, problems=()):
    from aion import swarmplan

    def fake_propose(goal, **kw):
        return swarmplan.Plan(goal=goal, source="llm",
                              steps=[swarmplan.Step(**s) for s in steps],
                              problems=list(problems))
    monkeypatch.setattr(swarmplan, "propose", fake_propose)


@pytest.mark.asyncio
async def test_planning_creates_nothing_until_apply(live, swarm, monkeypatch):
    before = set(swarm.agents)
    _fake_plan(monkeypatch, [{"name": "read", "goal": "read docs", "deps": []},
                             {"name": "write", "goal": "draft", "deps": ["read"]}])
    await live._swarm_command("swarm plan write a post")
    assert set(swarm.agents) == before
    assert len(live.state.swarm_plan["steps"]) == 2


@pytest.mark.asyncio
async def test_apply_creates_the_steps_that_were_shown(live, swarm, monkeypatch):
    _fake_plan(monkeypatch, [{"name": "read", "goal": "read docs", "deps": []},
                             {"name": "write", "goal": "draft", "deps": ["read"]}])
    await live._swarm_command("swarm plan write a post")
    await live._swarm_command("swarm apply")
    assert by_name(swarm, "write").dependencies == ["read"]
    assert live.state.swarm_plan == {}, "an applied plan must stop being pending"


@pytest.mark.asyncio
async def test_apply_does_not_re_plan(live, swarm, monkeypatch):
    """The model is not deterministic. Re-proposing on apply would create a
    different DAG from the one the human just read, which makes the review
    step decorative."""
    _fake_plan(monkeypatch, [{"name": "first", "goal": "g", "deps": []}])
    await live._swarm_command("swarm plan a goal")
    _fake_plan(monkeypatch, [{"name": "second", "goal": "g", "deps": []}])
    await live._swarm_command("swarm apply")
    assert by_name(swarm, "first") is not None
    assert by_name(swarm, "second") is None


@pytest.mark.asyncio
async def test_apply_with_nothing_planned_says_so(live):
    await live._swarm_command("swarm apply")
    assert any("nothing planned" in h for h in live.state.history)


@pytest.mark.asyncio
async def test_a_refused_plan_is_not_held(live, monkeypatch):
    _fake_plan(monkeypatch, [], problems=["the planner did not return usable JSON"])
    await live._swarm_command("swarm plan something")
    assert live.state.swarm_plan == {}
    assert any("refused" in h for h in live.state.history)


@pytest.mark.asyncio
async def test_a_planner_that_raises_does_not_take_the_cockpit_with_it(live, monkeypatch):
    from aion import swarmplan

    def boom(goal, **kw):
        raise RuntimeError("no provider")
    monkeypatch.setattr(swarmplan, "propose", boom)
    await live._swarm_command("swarm plan something")
    assert live.state.swarm_plan == {}
    assert any("no provider" in h for h in live.state.history)


@pytest.mark.asyncio
async def test_the_planner_does_not_run_on_the_event_loop(live, monkeypatch):
    """A 30s model call on the loop is a frozen cockpit: no keystrokes, no task
    updates, no heartbeat, for half a minute."""
    import asyncio

    from aion import swarmplan
    seen = {}

    def record(goal, **kw):
        seen["thread"] = __import__("threading").current_thread().name
        return swarmplan.Plan(goal=goal, steps=[swarmplan.Step("a", "g", [])])
    monkeypatch.setattr(swarmplan, "propose", record)
    main = __import__("threading").current_thread().name
    await live._swarm_command("swarm plan x")
    assert seen["thread"] != main
    assert asyncio.get_running_loop().is_running()


@pytest.mark.asyncio
async def test_the_status_payload_carries_the_sentences_not_just_the_numbers(live, swarm):
    """The browser is a second renderer of the same swarm. If it composes its
    own wording from the numbers, the two views drift — so the cockpit decides
    the words once and ships them."""
    st = live.swarm_command({"action": "status"})
    # scout only: writer and editor are waiting on it, which is a queue
    # position and not readiness.
    assert st["why"] == "1 ready — `swarm run` to start"
    assert "spend_text" in st and "capacity_text" in st
    assert st["capacity_text"].startswith("0/")


@pytest.mark.asyncio
async def test_an_unmetered_swarm_ships_an_empty_spend_line(live):
    """No prices configured means no figure — not a "$0.00" that reads as
    "nothing spent" when it means "nothing known"."""
    assert live.swarm_command({"action": "status"})["spend_text"] == ""


# ── replanning: the DAG grows from its own results ──────────────────────────
@pytest.mark.asyncio
async def test_the_replan_tick_asks_off_the_event_loop(live, swarm, monkeypatch):
    """`propose` is a model call. On the loop it is a frozen cockpit, exactly
    as the planner was."""
    import threading

    from aion import swarmreplan

    live._swarm_runner.replan = swarmreplan.ReplanPolicy(max_new_steps=2)
    seen = {}

    def record(goal, output, **kw):
        seen["thread"] = threading.current_thread().name
        return [{"name": "audit", "goal": "audit it"}]
    monkeypatch.setattr(swarmreplan, "propose", record)

    scout = by_name(swarm, "scout")
    live._swarm_runner.finish(scout.id, "found three subsystems")
    await live.swarm_replan_tick()

    assert seen["thread"] != threading.current_thread().name
    assert by_name(swarm, "audit") is not None


@pytest.mark.asyncio
async def test_a_refused_proposal_is_reported_not_silently_dropped(live, swarm, monkeypatch):
    from aion import swarmreplan

    live._swarm_runner.replan = swarmreplan.ReplanPolicy(max_new_steps=1,
                                                         max_total_steps=3)
    monkeypatch.setattr(swarmreplan, "propose",
                        lambda goal, output, **kw: [{"name": "audit", "goal": "g"}])
    live._swarm_runner.finish(by_name(swarm, "scout").id, "out")
    await live.swarm_replan_tick()
    assert any("refused" in h for h in live.state.history)


@pytest.mark.asyncio
async def test_replanning_off_asks_nothing(live, swarm, monkeypatch):
    from aion import swarmreplan

    called = []
    monkeypatch.setattr(swarmreplan, "propose",
                        lambda *a, **k: called.append(1) or [])
    live._swarm_runner.finish(by_name(swarm, "scout").id, "out")
    assert await live.swarm_replan_tick() == []
    assert called == []


def test_a_typed_add_can_declare_what_the_step_writes(live, swarm):
    """`>> path` is what makes "these two race on docs/api.md" answerable —
    nothing else in a swarm knows what a harness touches."""
    import asyncio
    asyncio.run(live._swarm_command("swarm add drafter draft the page >> docs/api.md"))
    assert by_name(swarm, "drafter").writes == ["docs/api.md"]


def test_writes_and_deps_can_be_declared_in_either_order(live, swarm):
    import asyncio
    asyncio.run(live._swarm_command(
        "swarm add one write it >> a.md << scout"))
    asyncio.run(live._swarm_command(
        "swarm add two write it << scout >> b.md"))
    assert by_name(swarm, "one").writes == ["a.md"]
    assert by_name(swarm, "one").dependencies == ["scout"]
    assert by_name(swarm, "two").writes == ["b.md"]
    assert by_name(swarm, "two").dependencies == ["scout"]
