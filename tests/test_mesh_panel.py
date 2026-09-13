"""Tests for the Mesh workspace wiring: sessions/models render + palette."""
import asyncio as _aio

from aion.ui.mesh_panel import render_mesh

THEME = {k: f"#{k}" for k in
         ("ok", "warn", "err", "faint", "fg", "dim", "accent")}


def _data():
    return {
        "mesh": {"nodes": [{"name": "pansa", "role": "storage-node",
                            "reachable": True, "load1": 1.0,
                            "ram_pct": 40, "disk_pct": 20}],
                 "total": 1, "reachable": 1},
        "services": {"up": 1, "services": [
            {"name": "omo-llm", "host": "omo-ts", "running": True,
             "probe_value": "8081"}]},
        "sessions": {"total": 2, "live": 1, "rows": [
            {"id": "t-run-1", "title": "Probe omo disk", "status": "running",
             "engine": "shell", "rc": "", "terminal": False},
            {"id": "t-done-2", "title": "Write docs", "status": "done",
             "engine": "opencode", "rc": "0", "terminal": True}]},
        "models": {"total": 2, "by_role": {"coding": 1, "agentic": 1},
                   "rows": [
                       {"id": "qwen3-coder", "node": "omo",
                        "roles": ["coding", "agentic"], "enabled": True},
                       {"id": "old-model", "node": "pansa",
                        "roles": ["chat"], "enabled": False}]},
    }


def test_render_mesh_sessions_and_models():
    out = render_mesh(_data(), THEME)
    assert "1 live/2" in out
    assert "t-run-1" in out and "running" in out and "Probe omo disk" in out
    assert "t-done-2" in out
    assert "coding:1" in out and "agentic:1" in out
    assert "qwen3-coder" in out and "omo" in out
    assert "old-model" not in out  # disabled models stay hidden


def test_render_mesh_tolerates_missing_sections():
    out = render_mesh({"mesh": {"nodes": [], "total": 0, "reachable": 0}},
                      THEME)
    assert "no mesh data" in out
    assert "sessions" not in out and "models 0" not in out


def test_render_mesh_bridge_section():
    data = {"mesh": {"nodes": [], "total": 0, "reachable": 0},
            "bridge": {"pending": 1, "learnings": 5, "rows": [
                {"id": "abc123", "from": "omo",
                 "content": "check the pool"}]}}
    out = render_mesh(data, THEME)
    assert "1 pending" in out and "5 learnings" in out
    assert "abc123" in out and "check the pool" in out
    assert "bridge inbox" in out


class _FakeApp:
    def __init__(self):
        self._mesh_cache = {"ts": 0.0, "data": {}}
        self.ran = []

    def _mesh_rows(self):
        return []

    async def _mesh_do(self, name, action, host=None):
        self.ran.append((name, action, host))
        return "RAN"


def test_mesh_sessions_palette(monkeypatch):
    from aion import fleettask
    from aion.fleettask import FleetTask
    from aion.ui.app import AiOSApp
    monkeypatch.setattr(fleettask, "read_local_tasks", lambda: [
        FleetTask(id="t1", title="T", status="queued", engine="shell")])
    out = _aio.run(AiOSApp._handle_mesh_command(_FakeApp(), "mesh sessions"))
    assert "1 live/1" in out and "t1" in out and "queued" in out


def test_mesh_place_palette_dry_run(monkeypatch, tmp_path):
    from aion import fleetplace
    from aion.ui.app import AiOSApp
    # hermetic stand-in for delegate.sh: canned dry-run output, no ssh.
    fake = tmp_path / "delegate.sh"
    fake.write_text("#!/usr/bin/env bash\n"
                    "echo '=== chosen: picked-ts (score=0.9000) ==='\n")
    fake.chmod(0o755)
    monkeypatch.setattr(fleetplace, "DELEGATE_SCRIPT", str(fake))
    out = _aio.run(AiOSApp._handle_mesh_command(
        _FakeApp(), "mesh place echo hi"))
    assert "would run on picked-ts" in out and "dry-run" in out
    out = _aio.run(AiOSApp._handle_mesh_command(_FakeApp(), "mesh place"))
    assert out.startswith("usage:")


def test_mesh_place_passes_needs(monkeypatch, tmp_path):
    from aion import fleetplace
    from aion.ui.app import AiOSApp
    # the stand-in records argv: --needs must reach the real script's flags.
    log = tmp_path / "argv.log"
    fake = tmp_path / "delegate.sh"
    fake.write_text("#!/usr/bin/env bash\n"
                    f"printf '%s\\n' \"$@\" > {log}\n"
                    "echo '=== chosen: picked-ts (score=0.9000) ==='\n")
    fake.chmod(0o755)
    monkeypatch.setattr(fleetplace, "DELEGATE_SCRIPT", str(fake))
    out = _aio.run(AiOSApp._handle_mesh_command(
        _FakeApp(), "mesh place echo hi --needs tool:cargo --needs gpu"))
    assert "would run on picked-ts" in out
    assert "[tool:cargo, gpu]" in out
    argv = log.read_text().split()
    assert argv.count("--needs") == 2
    assert "tool:cargo" in argv and "gpu" in argv


def test_fleet_panel_combines_peers_and_mesh():
    """The merged workspace stacks both halves — no half goes missing."""
    from aion.ui.app import AiOSApp

    class _Stub:
        def _net_panel(self, theme):
            return "PEERS-HALF"

        def _mesh_panel(self, theme):
            return "MESH-HALF"

    out = AiOSApp._fleet_panel(_Stub(), {})
    assert out == "PEERS-HALF\nMESH-HALF"


def test_shipped_config_has_one_fleet_workspace():
    """net+mesh merged: exactly one fleet workspace, old ids gone."""
    import json
    from pathlib import Path
    cfg = json.loads((Path(__file__).parent.parent
                      / "config" / "layout.json").read_text())
    ids = [w["id"] for w in cfg["workspaces"]]
    assert ids.count("fleet") == 1
    assert "net" not in ids and "mesh" not in ids


def test_fleet_workspace_navigates():
    """Switching to Fleet yields the panel placeholder, never a blank."""
    from aion.core import Bus, Intent, IntentType, TaskRegistry, load_config
    from aion.harnesses import build_harnesses
    from aion.store import Store
    cfg = load_config()
    bus = Bus()
    registry = TaskRegistry(bus)
    store = Store(cfg, bus,
                  harnesses=build_harnesses(cfg["harnesses"], bus, registry))
    idx = [w["id"] for w in cfg["workspaces"]].index("fleet")
    store.handle(Intent(IntentType.SWITCH_WORKSPACE, {"index": idx}))
    items = store._current_items()
    assert items == [{"type": "fleet_panel"}]
    store.handle(Intent(IntentType.NAVIGATE, {"dir": "down"}))
    store.handle(Intent(IntentType.ACTIVATE))
