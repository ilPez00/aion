"""test_agg.py — cross-machine aggregation (agg.py) pure-logic tests.

The network/SSH is behind an injectable transport; these tests fake it and
point AION_AGG_DB at a tmp file, so they never touch the fleet or real stores.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import aion.agg as agg

SAMPLE_PAYLOAD = {
    "host": "omo",
    "generated_at": 1700000000.0,
    "memory": [
        {"kind": "hermes", "section": 0,
         "body": "User prefers terse caveman output. no snap anywhere.",
         "preview": "User prefers terse caveman output. no snap anywhere."},
        {"kind": "aion", "section": 0, "body": "focus_mode: deep",
         "preview": "focus_mode: deep"},
    ],
    "sessions": [
        {"harness": "hermes", "id": "s1", "title": "Fix ollama mesh",
         "state": "done", "created": 1700000000.0,
         "preview": "repointed COLIBRI_NODES"},
        {"harness": "opencode", "id": "s2", "title": "refactor agg.py",
         "state": "session", "created": 0.0, "preview": ""},
        {"harness": "kanban", "id": "kanban-1", "title": "add CI workflow",
         "state": "todo", "created": 0.0, "preview": "add CI workflow"},
    ],
    "docs": [
        {"kind": "vault", "path": "vault/notes/ollama.md", "title": "ollama",
         "preview": "ollama needs localhost:11434"},
        {"kind": "skill", "path": "skills/mlops/x.md", "title": "x",
         "preview": "rocm mesh skill"},
    ],
    "models": [
        {"kind": "gguf", "path": "/models/gguf/starling-7b-Q4_K_M.gguf",
         "size_bytes": int(4.2 * (1024 ** 3)), "vram_hint_gb": 4.2, "hint": "gpu",
         "mtime": 1.0, "ref": "/models/gguf/starling-7b-Q4_K_M.gguf"},
        {"kind": "gguf", "path": "/models/gguf/gemma3-27b-Q4_K_M.gguf",
         "size_bytes": int(18.1 * (1024 ** 3)), "vram_hint_gb": 18.1, "hint": "gpu",
         "mtime": 2.0, "ref": "/models/gguf/gemma3-27b-Q4_K_M.gguf"},
    ],
    "counts": {"memory": 2, "sessions": 3, "docs": 2, "models": 2},
}


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "agg.db"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("AION_AGG_DB", str(p))
    yield p
    monkeypatch.undo()


def test_ingest_node_payload_populates_db(db_path):
    r = agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    assert r["ingested"]["total"] == 9
    st = agg.status(db_path)
    assert st["exists"] is True
    assert st["items"] == 9
    assert st["by_kind"] == {"memory": 2, "session": 3, "doc": 2, "model": 2}
    assert st["by_node"] == {"omo": 9}


def test_ingest_is_idempotent_upsert(db_path):
    agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    st = agg.status(db_path)
    assert st["items"] == 9


def test_search_matches_body_and_returns_highlight(db_path):
    agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    hits = agg.search("ollama", db_path)
    assert hits, "expected a match for 'ollama'"
    assert any(h["source"] == "hermes" for h in hits)
    assert all("ollama" in (h.get("body") or "") or "ollama" in (h.get("title") or "") for h in hits)


def test_search_kind_filter(db_path):
    agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    docs = agg.search("ollama", db_path, kind="doc")
    assert all(h["kind"] == "doc" for h in docs)
    assert any(h["source"] == "vault" for h in docs)


def test_search_model_kind(db_path):
    agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    hits = agg.search("gguf", db_path, kind="model")
    assert all(h["kind"] == "model" for h in hits)
    assert len(hits) == 2


def test_recent_orders_by_ts(db_path):
    agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    rec = agg.recent(db_path, limit=10)
    assert rec, "expected rows"
    assert rec[0]["kind"] == "session"  # ts=1700000000 is max
    assert len(rec) == 9


def test_status_reports_model_inventory(db_path):
    agg.ingest_node_payload(db_path, SAMPLE_PAYLOAD)
    st = agg.status(db_path)
    assert st["model_total"] == 2
    assert st["model_gb"] == pytest.approx(4.2 + 18.1, abs=0.1)
    by_host = st["model_by_host"]
    assert len(by_host) == 2
    assert by_host[0]["node"] == "omo"
    assert by_host[0]["gpu"] is True
    assert by_host[0]["name"].endswith(".gguf")


def test_status_missing_db(tmp_path):
    missing = tmp_path / "nope" / "agg.db"
    st = agg.status(missing)
    assert st["exists"] is False


def test_collect_node_fake_transport(db_path):
    """Fake transport returns the sample JSON; collect_node ingests it."""
    def fake_transport(alias, source):
        return True, json.dumps(SAMPLE_PAYLOAD), ""
    r = agg.collect_node(db_path, "omo", "omo-ts", fake_transport)
    assert r["ok"] is True
    assert r["node"] == "omo"
    assert r["ingested"]["total"] == 9


def test_collect_node_non_json_soft_fail(db_path):
    def fake_transport(alias, source):
        return True, "not json at all", ""
    r = agg.collect_node(db_path, "omo", "omo-ts", fake_transport)
    assert r["ok"] is False
    assert "non-JSON" in r.get("error", "")


def test_collect_node_transport_error_soft_fail(db_path):
    def fake_transport(alias, source):
        return False, "", "Connection refused"
    r = agg.collect_node(db_path, "omo", "omo-ts", fake_transport)
    assert r["ok"] is False
    assert "Connection refused" in r.get("error", "")


def test_collect_all_fake_transport_aggregates_two_nodes(db_path):
    payload_b = dict(SAMPLE_PAYLOAD, host="pansa")

    def fake_transport(alias, source):
        if alias.startswith("omo"):
            return True, json.dumps(SAMPLE_PAYLOAD), ""
        return True, json.dumps(payload_b), ""

    nodes = {"omo": "omo-ts", "pansa": "pansa-ts"}
    r = agg.collect_all(db_path, transport=fake_transport, nodes=nodes)
    assert all(v["ok"] for v in r["results"].values())
    st = r["summary"]
    assert st["by_node"] == {"omo": 9, "pansa": 9}
    assert st["items"] == 18


def test_collector_source_is_loadable():
    src = agg._collector_source()
    assert "mesh_agg_collect" in src
    assert "print(json.dumps" in src


def test_render_mesh_shows_aggregate():
    from aion.ui.mesh_panel import render_mesh

    theme = {k: f"#{k}" for k in
             ("ok", "warn", "err", "faint", "fg", "dim", "accent")}
    data = {
        "nodes": [], "total": 0, "reachable": 0,
        "agg": {
            "exists": True, "items": 12, "model_total": 2, "model_gb": 22.3,
            "by_node": {"omo": 7, "pansa": 5},
            "by_kind": {"session": 6, "memory": 4},
            "recent": True,
            "model_by_host": [
                {"node": "omo", "name": "starling-7b.gguf", "gb": 4.2,
                 "gpu": True, "hint": "gpu", "path": "/m/g"},
            ],
        },
    }
    out = render_mesh(data, theme)
    assert "agent aggregate" in out
    assert "12 items" in out
    assert "omo:7" in out and "pansa:5" in out
    assert "session:6" in out
    assert "model serving" in out.lower() or "model serving" in out
    assert "4.2GB" in out


def test_render_mesh_shows_uncollected():
    from aion.ui.mesh_panel import render_mesh

    theme = {k: k for k in
             ("ok", "warn", "err", "faint", "fg", "dim", "accent")}
    data = {"nodes": [], "total": 0, "reachable": 0,
            "agg": {"exists": False, "items": 0}}
    out = render_mesh(data, theme)
    assert "not collected" in out
