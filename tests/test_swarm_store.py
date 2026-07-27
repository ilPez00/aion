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
