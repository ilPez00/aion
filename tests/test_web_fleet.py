"""Web fleet surfaces: /api/fleet/sessions + /api/fleet/models snapshots."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import aion_web


def test_fleet_sessions_snapshot_empty_queue(monkeypatch, tmp_path):
    """Isolated HOME => empty queue dir => honest empty, never an error."""
    snap = aion_web.fleet_sessions_snapshot()
    assert snap == {"sessions": [], "total": 0, "live": 0}


def test_fleet_sessions_snapshot_rows(monkeypatch):
    from aion import fleettask
    from aion.fleettask import FleetTask
    monkeypatch.setattr(fleettask, "read_local_tasks", lambda: [
        FleetTask(id="t1", title="T", status="running", engine="shell"),
        FleetTask(id="t2", title="U", status="done", engine="shell", rc="0")])
    snap = aion_web.fleet_sessions_snapshot()
    assert (snap["total"], snap["live"]) == (2, 1)
    assert snap["sessions"][0]["id"] == "t1"


def test_fleet_models_snapshot_roles(monkeypatch, tmp_path):
    from aion import fleetview
    p = tmp_path / "fleet-models.json"
    p.write_text(json.dumps({"models": [
        {"id": "m1", "node": "omo", "roles": ["coding", "agentic"]},
        {"id": "m2", "node": "pansa", "roles": ["retrieval"],
         "enabled": False}]}))
    monkeypatch.setattr(fleetview, "DEFAULT_MODELS", p)
    snap = aion_web.fleet_models_snapshot()
    assert snap["total"] == 2
    assert snap["by_role"] == {"coding": 1, "agentic": 1}
    assert snap["models"][1]["enabled"] is False
