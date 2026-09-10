"""AION-001: FleetView engine + harness readers.

Pure-engine tests over fixtures (all-green, one-lost-task,
node-unreachable, malformed/partial JSON); sorting; lost-state
highlighting. Harness reads real state-dir shape via tmp fixtures.
Gate: python -m pytest tests/test_fleetview.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion.fleet import (  # noqa: E402
    FleetView,
    NodeCap,
    TaskRow,
    default_task_state_dir,
    read_capability_json,
    read_task_queue,
)


def _cap_all_green():
    return {"nodes": {
        "pansa": {"reachable": True, "cores": 12, "mem_total_mb": 15872,
                  "mem_avail_mb": 9000, "has_cargo": "yes", "has_node": "yes"},
        "omo": {"reachable": True, "cores": 16, "mem_total_mb": 15155,
                "mem_avail_mb": 4000, "gpu_name": "RX 6650 XT",
                "gpu_vram_mb": 8192, "has_cargo": "yes"},
    }}


def _tasks_one_lost():
    return [
        {"id": "t2", "title": "e2e3", "engine": "opencode", "status": "lost",
         "host": "feather", "submitted_at": "2026-09-01T06:48:17Z"},
        {"id": "t1", "title": "smoke-ok", "engine": "shell", "status": "done",
         "rc": 0, "host": "feather", "submitted_at": "2026-09-01T06:45:16Z"},
    ]


def test_capability_all_green():
    v = FleetView.from_capability(_cap_all_green())
    assert v.live_nodes == 2
    assert v.nodes[0].reachable and v.nodes[1].reachable
    # most RAM first
    assert v.nodes[0].name == "pansa"


def test_capability_unreachable_degrades():
    cap = {"nodes": {"air": {"reachable": False},
                     "pansa": {"reachable": True, "cores": 12}}}
    v = FleetView.from_capability(cap)
    assert v.live_nodes == 1
    assert v.nodes[0].name == "pansa"  # reachable sorts first
    assert any(n.name == "air" and not n.reachable for n in v.nodes)


def test_capability_malformed_never_crashes():
    for bad in (None, {}, {"nodes": None}, {"nodes": {"x": "nonsense"}},
                {"nodes": {"y": {"cores": "many", "mem_total_mb": None}}}):
        v = FleetView.from_capability(bad)
        assert isinstance(v.nodes, list)


def test_tasks_lost_floats_first_and_loud():
    v = FleetView.from_tasks(_tasks_one_lost())
    assert v.tasks[0].status == "lost"
    assert v.tasks[0].loud
    assert len(v.loud_tasks) == 1
    assert not v.tasks[1].loud


def test_tasks_malformed_rows_skipped():
    v = FleetView.from_tasks([None, "x", {}, {"noid": 1},
                              {"id": "t9", "status": "done", "rc": "bogus"}])
    assert [t.id for t in v.tasks] == ["t9"]
    assert v.tasks[0].rc is None


def test_summary_counts():
    v = FleetView.from_capability(_cap_all_green())
    v.tasks = FleetView.from_tasks(_tasks_one_lost()).tasks
    s = v.summary()
    assert "2 nodes" in s and "2 live" in s and "1 need attention" in s


def test_readers_missing_paths_degrade(tmp_path):
    assert read_capability_json(tmp_path / "nope.json") == {}
    assert read_task_queue(tmp_path / "nodir") == []


def test_readers_malformed_file_degrades(tmp_path):
    p = tmp_path / "cap.json"
    p.write_text("{not json")
    assert read_capability_json(p) == {}
    q = tmp_path / "tasks"
    q.mkdir()
    (q / "t1").mkdir()
    (q / "t1" / "meta.json").write_text("[broken")
    assert read_task_queue(q) == []


def test_readers_task_dirs(tmp_path):
    q = tmp_path / "tasks"
    (q / "t1").mkdir(parents=True)
    (q / "t1" / "meta.json").write_text(json.dumps(
        {"id": "t1", "title": "a", "status": "done", "rc": 0}))
    (q / "t2").mkdir()
    (q / "t2" / "meta.json").write_text(json.dumps(
        {"id": "t2", "title": "b", "status": "lost"}))
    rows = read_task_queue(q)
    assert {r["id"] for r in rows} == {"t1", "t2"}
    v = FleetView.from_tasks(rows)
    assert v.tasks[0].id == "t2"  # lost first


def test_render_section_never_crashes():
    from aion.ui.fleet_panel import render_capability_tasks
    theme = {"dim": "#9aabbb", "accent": "#5ad1ff", "ok": "#7CFFB2",
             "warn": "#FFD479", "err": "#FF8A8A", "faint": "#6b7d8d"}
    assert "no capability" in render_capability_tasks(FleetView(), theme)
    v = FleetView.from_capability(_cap_all_green())
    v.tasks = FleetView.from_tasks(_tasks_one_lost()).tasks
    out = render_capability_tasks(v, theme)
    assert "LOST" in out or "lost" in out
    assert "pansa" in out


def test_default_task_dir_shape():
    assert str(default_task_state_dir()).endswith("randomesh/tasks")
