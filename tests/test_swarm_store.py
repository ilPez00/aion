"""Swarm persistence — the dependency DAG has to survive the process."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion import procgraph as pg  # noqa: E402
from aion.swarm import AgentStatus, SwarmAgent, SwarmOrchestrator, SwarmStore  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> SwarmStore:
    return SwarmStore(tmp_path / "swarm.json")


@pytest.fixture()
def orch(store: SwarmStore) -> SwarmOrchestrator:
    return SwarmOrchestrator(store=store)


# ── checkpointing ────────────────────────────────────────────────────────
def test_adding_an_agent_checkpoints_immediately(orch, store):
    orch.add_agent("scout", "find the docs")
    assert store.path.exists()
    assert [a["name"] for a in json.loads(store.path.read_text())] == ["scout"]


def test_status_and_progress_changes_are_checkpointed(orch, store):
    a = orch.add_agent("scout", "find the docs")
    orch.set_progress(a.id, 0.5)
    orch.set_status(a.id, AgentStatus.WORKING)
    rec = json.loads(store.path.read_text())[0]
    assert rec["status"] == "working" and rec["progress"] == 0.5


def test_logs_are_checkpointed(orch, store):
    a = orch.add_agent("scout", "x")
    orch.log(a.id, "found something")
    assert json.loads(store.path.read_text())[0]["logs"] == ["found something"]


def test_persistence_can_be_switched_off(tmp_path, monkeypatch):
    """Unit tests and throwaway swarms must not touch ~/.aion."""
    o = SwarmOrchestrator(persist=False)
    o.add_agent("scout", "x")
    assert o.store is None


# ── round trip ───────────────────────────────────────────────────────────
def test_a_swarm_survives_a_restart(store):
    a = SwarmOrchestrator(store=store)
    s = a.add_agent("scout", "find the docs")
    w = a.add_agent("writer", "draft it", deps=["scout"])
    a.set_progress(s.id, 0.75)

    b = SwarmOrchestrator(store=SwarmStore(store.path))
    assert b.restore() == 2
    names = {x.name: x for x in b.agents.values()}
    assert names["writer"].dependencies == ["scout"]
    assert names["scout"].progress == 0.75
    assert names["scout"].id == s.id and names["writer"].id == w.id


def test_the_full_goal_survives_truncation_in_the_display_shape(store):
    """`as_dict` truncates to 80 chars for the dashboard; the record must not."""
    goal = "g" * 300
    o = SwarmOrchestrator(store=store)
    o.add_agent("scout", goal)
    restored = SwarmOrchestrator(store=SwarmStore(store.path))
    restored.restore()
    assert next(iter(restored.agents.values())).goal == goal


def test_in_flight_agents_come_back_idle_not_working(store):
    """The coroutine died with the process — claiming WORKING would be a lie."""
    o = SwarmOrchestrator(store=store)
    a = o.add_agent("scout", "x")
    o.set_status(a.id, AgentStatus.WORKING)
    b = SwarmOrchestrator(store=SwarmStore(store.path))
    b.restore()
    assert next(iter(b.agents.values())).status is AgentStatus.IDLE


def test_finished_agents_keep_their_state(store):
    o = SwarmOrchestrator(store=store)
    a = o.add_agent("scout", "x")
    o.set_status(a.id, AgentStatus.DONE)
    b = SwarmOrchestrator(store=SwarmStore(store.path))
    b.restore()
    assert next(iter(b.agents.values())).status is AgentStatus.DONE


def test_restored_agents_are_ready_to_run_again(store):
    """A dependency that completed stays satisfied across the restart."""
    o = SwarmOrchestrator(store=store)
    s = o.add_agent("scout", "x")
    o.add_agent("writer", "y", deps=["scout"])
    o.set_status(s.id, AgentStatus.DONE)
    b = SwarmOrchestrator(store=SwarmStore(store.path))
    b.restore()
    assert [a.name for a in b.agents_ready()] == ["writer"]


# ── robustness ───────────────────────────────────────────────────────────
def test_a_corrupt_file_loses_the_swarm_not_the_process(store):
    store.path.write_text("{{{not json")
    assert store.load() == []


def test_one_bad_record_does_not_lose_the_others(store):
    store.path.write_text(json.dumps([
        {"no_id": True},
        {"id": "a1", "name": "good", "goal": "g", "status": "idle", "deps": []},
    ]))
    assert [a.name for a in store.load()] == ["good"]


def test_an_unknown_status_falls_back_to_idle(store):
    store.path.write_text(json.dumps([
        {"id": "a1", "name": "x", "goal": "g", "status": "ascended", "deps": []}]))
    assert store.load()[0].status is AgentStatus.IDLE


def test_missing_file_loads_empty(store):
    assert store.load() == []


def test_clear_is_idempotent(store, orch):
    orch.add_agent("scout", "x")
    store.clear()
    store.clear()
    assert not store.path.exists()


# ── the reader the HUD actually uses ─────────────────────────────────────
def test_procgraph_picks_up_a_persisted_swarm(tmp_path):
    """procgraph.read_swarm already looked for this file — it must fit."""
    inst = tmp_path / "instances" / "solo"
    inst.mkdir(parents=True)
    o = SwarmOrchestrator(store=SwarmStore(inst / "swarm.json"))
    o.add_agent("scout", "find the docs")
    o.add_agent("writer", "draft it", deps=["scout"])

    got = pg.read_swarm(tmp_path / "instances")
    by_name = {a["name"]: a for a in got}
    assert set(by_name) == {"scout", "writer"}
    assert by_name["writer"]["deps"] == ["scout"]
    assert by_name["writer"]["instance"] == "solo"


def test_dependencies_are_names_so_the_graph_must_resolve_them(tmp_path):
    """Pins the contract the HUD adapter relies on.

    `deps` holds NAMES. Drawing edges as if they were ids yields a swarm with
    no edges — silently, which is worse than an error.
    """
    inst = tmp_path / "instances" / "solo"
    inst.mkdir(parents=True)
    o = SwarmOrchestrator(store=SwarmStore(inst / "swarm.json"))
    o.add_agent("scout", "x")
    w = o.add_agent("writer", "y", deps=["scout"])

    got = {a["name"]: a for a in pg.read_swarm(tmp_path / "instances")}
    ids = {a["id"] for a in got.values()}
    assert got["writer"]["deps"][0] not in ids       # it is a name, not an id
    assert got["writer"]["deps"][0] == "scout"
    assert got["writer"]["id"] == w.id


def test_search_finds_a_persisted_swarm_agent(tmp_path, monkeypatch):
    inst = tmp_path / "instances" / "solo"
    inst.mkdir(parents=True)
    (inst / "meta.json").write_text(json.dumps({"id": "solo", "pid": 0}))
    o = SwarmOrchestrator(store=SwarmStore(inst / "swarm.json"))
    o.add_agent("cartographer", "map the coastline")

    monkeypatch.setenv("AION_HOME", str(tmp_path))
    hits = pg.search("cartographer")
    assert hits and hits[0]["type"] == "swarm"
    assert hits[0]["module"] == "agents"


# ── work that outlived the process ───────────────────────────────────────────
# A local task dies with the harness coroutine, so it comes back IDLE. A REMOTE
# one does not: the peer never noticed we went away and is still working.
# Resetting that to IDLE is not conservative, it is a second copy of the same
# job — double spend, and every side effect that step has, twice.
def _runner(orch):
    from aion.swarmrun import SwarmRunner
    return SwarmRunner(orch, spawn=lambda a, p: "t-new", harness="demo")


def test_a_remote_step_comes_back_still_working(store):
    from aion.swarm import AgentStatus, SwarmOrchestrator, SwarmStore

    b = SwarmOrchestrator(store=store)
    a = b.add_agent("heavy", "grind", instance="workstation")
    _runner(b)._own(a.id, "t0007", "workstation")
    b.set_status(a.id, AgentStatus.WORKING)

    back = SwarmOrchestrator(store=SwarmStore(store.path))
    back.restore()
    got = back.agent_by_name("heavy")
    assert got.status is AgentStatus.WORKING
    assert got.task_id == "t0007" and got.instance == "workstation"


def test_a_local_step_still_comes_back_idle(store):
    """The coroutine running it is gone. Claiming otherwise strands the DAG on
    a task that will never report."""
    from aion.swarm import AgentStatus, SwarmOrchestrator, SwarmStore

    b = SwarmOrchestrator(store=store)
    a = b.add_agent("prep", "collect")
    _runner(b)._own(a.id, "t0001")
    b.set_status(a.id, AgentStatus.WORKING)

    back = SwarmOrchestrator(store=SwarmStore(store.path))
    back.restore()
    assert back.agent_by_name("prep").status is AgentStatus.IDLE


def test_a_remote_step_with_no_task_id_is_not_trusted(store):
    """WORKING was set, the spawn request was still in flight when we died.
    Nothing is running over there to re-attach to."""
    from aion.swarm import AgentStatus, SwarmOrchestrator, SwarmStore

    b = SwarmOrchestrator(store=store)
    a = b.add_agent("heavy", "grind", instance="workstation")
    b.set_status(a.id, AgentStatus.WORKING)

    back = SwarmOrchestrator(store=SwarmStore(store.path))
    back.restore()
    assert back.agent_by_name("heavy").status is AgentStatus.IDLE


def test_rehydrate_re_attaches_the_watch(store):
    from aion.swarm import AgentStatus, SwarmOrchestrator, SwarmStore

    b = SwarmOrchestrator(store=store)
    a = b.add_agent("heavy", "grind", instance="workstation")
    _runner(b)._own(a.id, "t0007", "workstation")
    b.set_status(a.id, AgentStatus.WORKING)

    back = SwarmOrchestrator(store=SwarmStore(store.path))
    back.restore()
    runner = _runner(back)
    assert runner.rehydrate()["adopted"] == ["heavy"]
    aid = back.agent_by_name("heavy").id
    assert runner.task_of[aid] == "t0007"
    # Namespaced by instance: task ids are unique per registry, not per fleet,
    # so two machines in one DAG both hand back `t0007`. Asserted through
    # `_key` rather than the raw string so this says what it means.
    assert runner.agent_of[runner._key("workstation", "t0007")] == aid
    assert runner.watches[aid].instance == "workstation"


def test_a_rehydrated_step_completes_instead_of_being_re_run(store):
    """The whole point: the next poll collects the result of the job that was
    already running, and the DAG moves on."""
    from aion.swarm import AgentStatus, SwarmOrchestrator, SwarmStore

    b = SwarmOrchestrator(store=store)
    heavy = b.add_agent("heavy", "grind", instance="workstation")
    b.add_agent("report", "write up", deps=["heavy"])
    _runner(b)._own(heavy.id, "t0007", "workstation")
    b.set_status(heavy.id, AgentStatus.WORKING)

    back = SwarmOrchestrator(store=SwarmStore(store.path))
    back.restore()
    spawned = []
    from aion.swarmrun import SwarmRunner
    runner = SwarmRunner(
        back, spawn=lambda a, p: (spawned.append(a.name) or f"t{len(spawned)}"),
        poll_remote=lambda inst, tid: {"state": "done", "output": "ground it"},
        harness="demo")
    runner.rehydrate()
    runner.poll()

    assert spawned == ["report"], "the remote step was re-run instead of adopted"
    assert back.agent_by_name("heavy").status is AgentStatus.DONE
    assert back.agent_by_name("heavy").output == "ground it"


def test_rehydrate_is_safe_to_call_twice(store):
    from aion.swarm import AgentStatus, SwarmOrchestrator, SwarmStore

    b = SwarmOrchestrator(store=store)
    a = b.add_agent("heavy", "grind", instance="workstation")
    _runner(b)._own(a.id, "t0007", "workstation")
    b.set_status(a.id, AgentStatus.WORKING)
    back = SwarmOrchestrator(store=SwarmStore(store.path))
    back.restore()
    runner = _runner(back)
    runner.rehydrate()
    runner.rehydrate()
    assert len(runner.watches) == 1


def test_a_finished_agent_carries_no_task_id(store):
    """A stale id on a DONE agent would be re-adopted on the next restart and
    polled forever against a task nobody is running."""
    from aion.swarm import SwarmOrchestrator

    b = SwarmOrchestrator(store=store)
    a = b.add_agent("heavy", "grind", instance="workstation")
    runner = _runner(b)
    runner._own(a.id, "t0007", "workstation")
    runner.finish(a.id, "done with it")
    assert b.agents[a.id].task_id == ""


def test_a_cockpit_restart_brings_the_dag_back(tmp_path, monkeypatch):
    """`restore()` existed and was called nowhere in production. The DAG
    survived a restart on disk, and in the web HUD (procgraph reads swarm.json
    directly), while the cockpit that owns it came back believing there was no
    swarm at all."""
    monkeypatch.setenv("AION_HOME", str(tmp_path))
    from aion.core import Bus, TaskRegistry, load_config
    from aion.store import Store

    cfg = load_config()
    first = Store(cfg, Bus(), harnesses={})
    first.swarm.add_checked("scout", "find sources")
    first.swarm.add_checked("writer", "draft it", ["scout"])

    second = Store(cfg, Bus(), harnesses={})
    assert sorted(a.name for a in second.swarm.agents.values()) == ["scout", "writer"]
    assert second.swarm.agent_by_name("writer").dependencies == ["scout"]
