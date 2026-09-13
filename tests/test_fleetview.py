"""Tests for fleetview.py — unified Fleet view-model (no network)."""
import json

from aion.fleetview import collect, load_models, summary


def _nodes():
    return {"total": 2, "reachable": 1, "nodes": [
        {"name": "pansa", "reachable": True},
        {"name": "air", "reachable": False}]}


def _services():
    return {"total": 2, "up": 1, "services": [
        {"name": "omo-llm", "running": True},
        {"name": "colibri", "running": False}]}


def _sessions():
    return [{"id": "t1", "status": "running", "terminal": False},
            {"id": "t2", "status": "done", "terminal": True}]


def _models_file(tmp_path):
    p = tmp_path / "fleet-models.json"
    p.write_text(json.dumps({"models": [
        {"id": "qwen3-coder", "node": "omo", "roles": ["coding", "agentic"]},
        {"id": "embed", "node": "pansa", "roles": ["retrieval"],
         "enabled": False}]}))
    return p


def test_collect_assembles_all_sections(tmp_path):
    view = collect(_nodes, _services, _sessions,
                   models_path=_models_file(tmp_path))
    assert (view["nodes"]["reachable"], view["nodes"]["total"]) == (1, 2)
    assert (view["services"]["up"], view["services"]["total"]) == (1, 2)
    assert (view["sessions"]["live"], view["sessions"]["total"]) == (1, 2)
    assert view["models"]["total"] == 2
    assert view["models"]["by_role"] == {"coding": 1, "agentic": 1}
    assert summary(view) == \
        "nodes 1/2 · services 1/2 · sessions 1 live/2 · models 2"


def test_collect_soft_fails_per_source(tmp_path):
    def boom():
        raise TimeoutError("ssh hung")
    view = collect(boom, boom, boom, models_path=tmp_path / "missing.json")
    assert view["nodes"] == {"total": 0, "reachable": 0, "rows": []}
    assert view["services"] == {"total": 0, "up": 0, "rows": []}
    assert view["sessions"] == {"total": 0, "live": 0, "rows": []}
    assert view["models"]["total"] == 0
    assert "0/0" in summary(view)


def test_load_models_malformed(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{oops")
    assert load_models(p) == []
    p.write_text(json.dumps({"models": [{"noid": 1}, {"id": "ok"}]}))
    assert [m["id"] for m in load_models(p)] == ["ok"]


def test_collect_live_against_real_files():
    """Real fleet-models.json parses; real queue reads without raising."""
    view = collect(lambda: {"total": 0, "reachable": 0, "nodes": []},
                   lambda: {"total": 0, "up": 0, "services": []},
                   lambda: [],
                   models_path="/home/gio/dev/randomesh/fleet-models.json")
    assert view["models"]["total"] >= 1
    assert "coding" in view["models"]["by_role"]
