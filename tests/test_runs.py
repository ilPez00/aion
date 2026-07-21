"""Tests for the Runs workspace collector (pure)."""
from __future__ import annotations

import types

from aion import runs
from aion.core import Task, TaskState


def _task(tid, harness, state, created=0.0, log=None):
    t = Task(id=tid, label=f"{harness}: work {tid}", harness=harness)
    t.state = TaskState(state)
    t.created = created
    t.log = log or []
    return t


def _harnesses(spec):
    """spec: {id: tags-tuple} -> {id: obj with .cfg.context_tags}."""
    out = {}
    for hid, tags in spec.items():
        out[hid] = types.SimpleNamespace(
            cfg=types.SimpleNamespace(context_tags=tags))
    return out


HARNESSES = _harnesses({
    "research": ("dev", "agent"),
    "factory": ("dev", "agent"),
    "demo": ("system",),        # not agent work
    "system": ("system",),
})


def test_agent_ids_are_the_agent_tagged_harnesses():
    ids = runs.agent_harness_ids(HARNESSES)
    assert ids == {"research", "factory"}
    assert "demo" not in ids


def test_processes_tab_shows_only_live_agent_tasks():
    tasks = [
        _task("t1", "research", "running"),
        _task("t2", "factory", "pending"),
        _task("t3", "research", "done"),      # a result, not a process
        _task("t4", "demo", "running"),       # not agent work
    ]
    ids = runs.agent_harness_ids(HARNESSES)
    rows = runs.collect_runs(tasks, ids, runs.TAB_PROCESSES)
    got = {r.id for r in rows}
    assert got == {"t1", "t2"}


def test_results_tab_shows_finished_agent_tasks():
    tasks = [
        _task("t1", "research", "running"),
        _task("t3", "research", "done"),
        _task("t5", "factory", "failed"),
        _task("t6", "factory", "interrupted"),
    ]
    ids = runs.agent_harness_ids(HARNESSES)
    rows = runs.collect_runs(tasks, ids, runs.TAB_RESULTS)
    assert {r.id for r in rows} == {"t3", "t5", "t6"}


def test_results_are_newest_first_processes_oldest_first():
    tasks = [
        _task("old", "research", "running", created=1.0),
        _task("new", "research", "running", created=9.0),
    ]
    ids = runs.agent_harness_ids(HARNESSES)
    proc = runs.collect_runs(tasks, ids, runs.TAB_PROCESSES)
    assert [r.id for r in proc] == ["old", "new"]     # oldest first

    done = [_task("r-old", "research", "done", created=1.0),
            _task("r-new", "research", "done", created=9.0)]
    res = runs.collect_runs(done, ids, runs.TAB_RESULTS)
    assert [r.id for r in res] == ["r-new", "r-old"]  # newest first


def test_output_drops_noisy_step_pings():
    log = ["[research] plan: x", "[research] search: y",
           "Berlin is the capital [1]", "[research] reflect: z"]
    t = _task("t1", "research", "done", log=log)
    ids = runs.agent_harness_ids(HARNESSES)
    row = runs.collect_runs([t], ids, runs.TAB_RESULTS)[0]
    assert any("Berlin" in ln for ln in row.output)
    assert not any("plan:" in ln for ln in row.output)


def test_tab_counts():
    tasks = [
        _task("t1", "research", "running"),
        _task("t2", "factory", "pending"),
        _task("t3", "research", "done"),
        _task("t4", "demo", "running"),      # excluded
    ]
    ids = runs.agent_harness_ids(HARNESSES)
    counts = runs.tab_counts(tasks, ids)
    assert counts == {"processes": 2, "results": 1}


def test_other_tab_toggles():
    assert runs.other_tab("processes") == "results"
    assert runs.other_tab("results") == "processes"
