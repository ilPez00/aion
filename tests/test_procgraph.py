"""Process-graph engine: reading fleet state off disk, and searching it."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion import procgraph as pg  # noqa: E402


@pytest.fixture()
def fleet(tmp_path: Path) -> Path:
    """Two instances: one 'live' (our own pid) and one dead."""
    root = tmp_path / "instances"
    live = root / "alpha"
    dead = root / "beta"
    live.mkdir(parents=True)
    dead.mkdir(parents=True)
    (live / "meta.json").write_text(json.dumps({
        "id": "alpha", "pid": os.getpid(), "port": 8765, "hostname": "omo",
        "active_harness": "demo", "running_count": 1,
        "started_at": 1000.0, "updated_at": 2000.0}))
    (dead / "meta.json").write_text(json.dumps({
        "id": "beta", "pid": 999999, "port": 8766, "hostname": "omo",
        "active_harness": "shell", "running_count": 0}))
    (live / "session.json").write_text(json.dumps([
        {"id": "t0001", "label": "Demo Harness: pump optimisation",
         "harness": "demo", "state": "running", "progress": 0.4,
         "eta": 12, "domain": "CONSTRUCT/WORK", "log": ["step 1", "step 2"]},
        {"id": "t0002", "label": "Shell Agent: build", "harness": "shell",
         "state": "done", "progress": 1.0, "log": []},
    ]))
    (dead / "session.json").write_text(json.dumps([
        {"id": "t0009", "label": "Ghost task", "harness": "vanished",
         "state": "interrupted", "progress": 0.2, "log": []},
    ]))
    return root


@pytest.fixture()
def config(tmp_path: Path) -> Path:
    p = tmp_path / "layout.json"
    p.write_text(json.dumps({"harnesses": [
        {"id": "demo", "name": "Demo Harness", "type": "demo", "tier": "standard",
         "vram_mb": 420, "enabled": True, "max_steps": 20},
        {"id": "shell", "name": "Shell Agent", "type": "shell", "tier": "cheap",
         "vram_mb": 0, "enabled": True},
    ]}))
    return p


def snap(fleet, config, **kw):
    return pg.snapshot(root=fleet, config_path=config, **kw)


# ── instances ────────────────────────────────────────────────────────────
def test_reads_every_instance_live_or_not(fleet):
    got = {i.id: i for i in pg.read_instances(fleet)}
    assert set(got) == {"alpha", "beta"}
    assert got["alpha"].alive is True
    assert got["beta"].alive is False


def test_dead_instances_are_kept_not_hidden(fleet, config):
    """Their INTERRUPTED tasks are exactly what you open this view to find."""
    s = snap(fleet, config)
    assert any(t["instance"] == "beta" for t in s["tasks"])


def test_missing_root_is_empty_not_an_error(tmp_path):
    assert pg.read_instances(tmp_path / "nope") == []


def test_corrupt_meta_does_not_kill_the_scan(fleet, config):
    (fleet / "alpha" / "meta.json").write_text("{not json")
    s = snap(fleet, config)
    assert s["summary"]["instances"] == 2       # falls back to the dir name


def test_a_stray_file_in_the_instances_dir_is_ignored(fleet, config):
    (fleet / "README").write_text("not an instance")
    assert snap(fleet, config)["summary"]["instances"] == 2


# ── tasks ────────────────────────────────────────────────────────────────
def test_tasks_carry_their_instance_and_harness(fleet, config):
    s = snap(fleet, config)
    t = next(t for t in s["tasks"] if t["id"] == "t0001")
    assert t["instance"] == "alpha" and t["harness"] == "demo"
    assert t["progress"] == 0.4 and t["state"] == "running"


def test_corrupt_session_json_yields_no_tasks_not_a_crash(fleet, config):
    (fleet / "alpha" / "session.json").write_text("[[[")
    s = snap(fleet, config)
    assert all(t["instance"] != "alpha" for t in s["tasks"])


def test_task_entries_missing_an_id_are_skipped(fleet, config):
    (fleet / "alpha" / "session.json").write_text(
        json.dumps([{"label": "no id"}, {"id": "t1", "label": "ok"}]))
    ids = [t["id"] for t in snap(fleet, config)["tasks"] if t["instance"] == "alpha"]
    assert ids == ["t1"]


def test_finished_tasks_can_be_filtered_out(fleet, config):
    states = {t["state"] for t in snap(fleet, config, include_finished=False)["tasks"]}
    assert states <= {"running", "pending"}


def test_log_tail_is_bounded(fleet, config):
    (fleet / "alpha" / "session.json").write_text(json.dumps([
        {"id": "t1", "label": "x", "harness": "demo", "state": "done",
         "log": [f"line {i}" for i in range(500)]}]))
    t = next(t for t in snap(fleet, config)["tasks"] if t["id"] == "t1")
    assert len(t["log"]) <= 12 and t["log"][-1] == "line 499"


# ── harnesses ────────────────────────────────────────────────────────────
def test_harnesses_come_from_config(fleet, config):
    ids = {h["id"] for h in snap(fleet, config)["harnesses"]}
    assert {"demo", "shell"} <= ids


def test_a_task_whose_harness_vanished_gets_an_orphan_hub(fleet, config):
    """Orphaned work must stay visible — that is the interesting failure."""
    s = snap(fleet, config)
    orphan = next(h for h in s["harnesses"] if h["id"] == "vanished")
    assert orphan["orphan"] is True and orphan["enabled"] is False
    assert any(t["harness"] == "vanished" for t in s["tasks"])


def test_missing_config_still_produces_a_graph(fleet, tmp_path):
    s = pg.snapshot(root=fleet, config_path=tmp_path / "gone.json")
    # every harness present is an orphan synthesised from the tasks
    assert s["tasks"] and all(h.get("orphan") for h in s["harnesses"])


# ── summary ──────────────────────────────────────────────────────────────
def test_summary_counts_match_the_payload(fleet, config):
    s = snap(fleet, config)
    assert s["summary"]["tasks"] == len(s["tasks"])
    assert s["summary"]["live_instances"] == 1
    assert s["summary"]["active"] == 1
    assert sum(s["summary"]["by_state"].values()) == len(s["tasks"])


# ── swarm ────────────────────────────────────────────────────────────────
def test_swarm_is_empty_when_nothing_persisted(fleet, config):
    assert snap(fleet, config)["swarm"] == []


def test_swarm_is_picked_up_when_present(fleet, config):
    (fleet / "alpha" / "swarm.json").write_text(json.dumps([
        {"id": "a1", "name": "scout", "goal": "find docs", "status": "working",
         "progress": 0.3, "deps": []},
        {"id": "a2", "name": "writer", "goal": "draft", "status": "waiting",
         "progress": 0.0, "deps": ["a1"]}]))
    sw = snap(fleet, config)["swarm"]
    assert {a["name"] for a in sw} == {"scout", "writer"}
    assert next(a for a in sw if a["name"] == "writer")["deps"] == ["a1"]


# ── live watch: fingerprint ──────────────────────────────────────────────
def test_fingerprint_is_stable_when_nothing_moves(fleet):
    assert pg.fingerprint(fleet) == pg.fingerprint(fleet)


def test_fingerprint_changes_when_a_checkpoint_is_written(fleet):
    before = pg.fingerprint(fleet)
    data = json.loads((fleet / "alpha" / "session.json").read_text())
    data[0]["progress"] = 0.9
    (fleet / "alpha" / "session.json").write_text(json.dumps(data))
    assert pg.fingerprint(fleet) != before


def test_fingerprint_catches_a_same_length_edit(fleet):
    """mtime granularity can hide a fast edit; size alone hides same-size ones.

    A task flipping `running` -> `pending` is a same-ish-length rewrite, which
    is exactly the change a naive watcher misses.
    """
    p = fleet / "alpha" / "session.json"
    before = pg.fingerprint(fleet)
    raw = p.read_text().replace('"running"', '"pending"')
    p.write_text(raw)
    assert pg.fingerprint(fleet) != before


def test_fingerprint_of_a_missing_root_is_empty(tmp_path):
    assert pg.fingerprint(tmp_path / "nope") == ""


def test_fingerprint_survives_a_vanishing_file(fleet):
    (fleet / "alpha" / "meta.json").unlink()
    assert isinstance(pg.fingerprint(fleet), str)


# ── live watch: diff ─────────────────────────────────────────────────────
def test_first_diff_is_a_full_snapshot(fleet, config):
    d = pg.diff(None, snap(fleet, config))
    assert d["full"] is True and d["changed"]


def test_diff_of_identical_snapshots_is_empty(fleet, config):
    s = snap(fleet, config)
    d = pg.diff(s, s)
    assert d["full"] is False and d["changed"] == [] and d["removed"] == []


def test_diff_reports_a_state_transition_with_its_previous_value(fleet, config):
    """`_was` is what lets the UI animate the transition rather than the value."""
    before = snap(fleet, config)
    raw = json.loads((fleet / "alpha" / "session.json").read_text())
    raw[0]["state"] = "done"
    raw[0]["progress"] = 1.0
    (fleet / "alpha" / "session.json").write_text(json.dumps(raw))
    d = pg.diff(before, snap(fleet, config))
    changed = {c["id"]: c for c in d["changed"]}
    assert changed["t0001"]["state"] == "done"
    assert changed["t0001"]["_was"] == "running"


def test_diff_flags_a_brand_new_task(fleet, config):
    before = snap(fleet, config)
    raw = json.loads((fleet / "alpha" / "session.json").read_text())
    raw.append({"id": "t0050", "label": "fresh", "harness": "demo",
                "state": "pending", "progress": 0.0, "log": []})
    (fleet / "alpha" / "session.json").write_text(json.dumps(raw))
    d = pg.diff(before, snap(fleet, config))
    new = [c for c in d["changed"] if c["id"] == "t0050"]
    assert new and new[0]["_new"] is True


def test_diff_reports_removals(fleet, config):
    before = snap(fleet, config)
    (fleet / "alpha" / "session.json").write_text("[]")
    d = pg.diff(before, snap(fleet, config))
    assert "alpha:t0001" in d["removed"]


def test_diff_notices_a_new_log_line_alone(fleet, config):
    """Log output with no state change is still progress worth pushing."""
    before = snap(fleet, config)
    raw = json.loads((fleet / "alpha" / "session.json").read_text())
    raw[0]["log"] = raw[0]["log"] + ["step 3"]
    (fleet / "alpha" / "session.json").write_text(json.dumps(raw))
    d = pg.diff(before, snap(fleet, config))
    assert any(c["id"] == "t0001" for c in d["changed"])


def test_diff_keys_tasks_by_instance_so_ids_can_collide(fleet, config):
    """Every instance numbers its tasks from t0001 — a bare id is ambiguous."""
    (fleet / "beta" / "session.json").write_text(json.dumps([
        {"id": "t0001", "label": "different task on beta", "harness": "demo",
         "state": "done", "progress": 1.0, "log": []}]))
    s = snap(fleet, config)
    ids = [f"{t['instance']}:{t['id']}" for t in s["tasks"]]
    assert len(ids) == len(set(ids))
    assert "alpha:t0001" in ids and "beta:t0001" in ids


# ── search ───────────────────────────────────────────────────────────────
def test_search_finds_a_task_by_its_label(fleet, config):
    s = snap(fleet, config)
    hits = pg.search("pump", s)
    assert hits and hits[0]["type"] == "task"
    assert hits[0]["module"] == "agents" and hits[0]["node"].startswith("t")


def test_search_reaches_into_task_logs(fleet, config):
    """The log is often the only place the thing you remember was written."""
    hits = pg.search("step 2", snap(fleet, config))
    assert any(h["id"] == "t0001" for h in hits)


def test_search_finds_harnesses_and_instances(fleet, config):
    s = snap(fleet, config)
    assert any(h["type"] == "harness" for h in pg.search("shell", s))
    assert any(h["type"] == "instance" for h in pg.search("alpha", s))


def test_search_is_case_insensitive(fleet, config):
    s = snap(fleet, config)
    assert pg.search("PUMP", s) and pg.search("pump", s)


def test_empty_query_returns_nothing(fleet, config):
    assert pg.search("   ", snap(fleet, config)) == []


def test_search_results_are_capped(fleet, config):
    assert len(pg.search("a", snap(fleet, config), limit=3)) <= 3


def test_search_results_carry_jump_coordinates(fleet, config):
    """Every hit must be enough to navigate to — module + node id."""
    for h in pg.search("demo", snap(fleet, config)):
        assert h["module"] and h["node"] and h["label"]
