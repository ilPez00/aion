"""bridge.py — the cockpit's shared state, read from the web process.

The central property is fail-soft: the HUD paints one surface from seven
stores, and one broken store must degrade to an error string in its own
section rather than blanking the screen.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion import bridge  # noqa: E402


# ── json coercion ────────────────────────────────────────────────────────
def test_paths_are_flattened_for_json():
    """SkillInfo legitimately holds a Path; discovering that at json.dumps
    time is a 500 on a route that had already succeeded."""
    got = bridge._jsonable({"p": Path("/tmp/x"), "n": [Path("/a"), 1]})
    assert got == {"p": "/tmp/x", "n": ["/a", 1]}
    json.dumps(got)


def test_jsonable_handles_objects_and_sets():
    class Thing:
        def __init__(self):
            self.p = Path("/x")
            self.tags = {"a"}
    json.dumps(bridge._jsonable(Thing()))


def test_jsonable_passes_scalars_through():
    assert bridge._jsonable(None) is None
    assert bridge._jsonable(3) == 3
    assert bridge._jsonable(True) is True


# ── fail-soft ────────────────────────────────────────────────────────────
def test_soft_reports_the_error_instead_of_raising():
    val, err = bridge._soft(lambda: 1 / 0, "fallback")
    assert val == "fallback" and "ZeroDivisionError" in err


def test_a_broken_store_does_not_blank_the_desktop(monkeypatch):
    monkeypatch.setattr(bridge, "todos",
                        lambda: {"items": [], "error": "boom", "open": 0})
    d = bridge.desktop()
    assert d["todos"]["error"] == "boom"
    assert "apps" in d and "modes" in d       # the rest still rendered


def test_corrupt_stores_yield_empty_sections(tmp_path, monkeypatch):
    from aion import fleet
    monkeypatch.setattr(fleet, "AION_HOME", tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir(parents=True)
    (shared / "memory.json").write_text("{{{ not json")
    (shared / "boards.json").write_text("[[[")
    assert bridge.memory()["facts"] == []
    assert bridge.boards()["boards"] == []


# ── todos round trip ─────────────────────────────────────────────────────
@pytest.fixture()
def store_home(tmp_path, monkeypatch):
    from aion import fleet
    monkeypatch.setattr(fleet, "AION_HOME", tmp_path)
    (tmp_path / "shared").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_todo_add_and_complete(store_home):
    bridge.todo_add("buy milk")
    d = bridge.todos()
    assert [t["text"] for t in d["items"]] == ["buy milk"]
    assert d["open"] == 1

    bridge.todo_done(0)
    d = bridge.todos()
    assert d["items"][0]["done"] is True and d["open"] == 0


def test_todo_remove(store_home):
    bridge.todo_add("a")
    bridge.todo_add("b")
    bridge.todo_remove(0)
    assert [t["text"] for t in bridge.todos()["items"]] == ["b"]


def test_open_count_ignores_completed(store_home):
    bridge.todo_add("a")
    bridge.todo_add("b")
    bridge.todo_done(0)
    assert bridge.todos()["open"] == 1


# ── memory round trip ────────────────────────────────────────────────────
def test_memory_add_and_forget(store_home):
    bridge.memory_add("the parser is in src/lex")
    assert bridge.memory()["count"] == 1
    bridge.memory_forget(0)
    assert bridge.memory()["count"] == 0


def test_forgetting_the_top_fact_removes_that_fact(store_home):
    """The list the UI shows and the list the store deletes from must be one
    list. `facts` is insertion order, `search()` is newest-first — mixing them
    silently forgets the wrong thing."""
    bridge.memory_add("oldest")
    bridge.memory_add("newest")
    shown = [f["text"] for f in bridge.memory()["facts"]]
    bridge.memory_forget(0)
    left = [f["text"] for f in bridge.memory()["facts"]]
    assert shown[0] not in left, f"forgot the wrong fact: showed {shown}, left {left}"


def test_an_out_of_range_index_is_reported_not_silent(store_home):
    bridge.todo_add("only one")
    out = bridge.todo_done(99)
    assert out["error"] and out["items"][0]["done"] is False


def test_memory_query_filters(store_home):
    bridge.memory_add("rocket thrust notes")
    bridge.memory_add("tomato planting notes")
    got = bridge.memory("rocket")
    assert got["query"] == "rocket"
    assert all("rocket" in str(f["text"]).lower() for f in got["facts"])


# ── derived surfaces ─────────────────────────────────────────────────────
def test_apps_reports_availability():
    d = bridge.apps()
    assert d["error"] is None and d["apps"]
    assert all("id" in a and "available" in a for a in d["apps"])
    assert d["installed"] == sum(1 for a in d["apps"] if a["available"])


def test_modes_are_listed():
    d = bridge.modes()
    assert d["error"] is None and len(d["modes"]) >= 3
    assert all(m["id"] for m in d["modes"])


def test_providers_report_presence_never_the_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "sk-super-secret-value")
    got = bridge.providers()
    groq = next(p for p in got if p["env"] == "GROQ_API_KEY")
    assert groq["present"] is True
    assert "sk-super-secret-value" not in json.dumps(got)


def test_absent_provider_is_reported_as_absent(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    got = next(p for p in bridge.providers() if p["env"] == "OPENAI_API_KEY")
    assert got["present"] is False


def test_settings_serialises(monkeypatch):
    """This is what broke first: SkillInfo carries Path objects."""
    json.dumps(bridge.settings())


def test_paths_surface_tells_you_where_things_live():
    p = bridge.paths()
    assert {"home", "shared", "instance", "fs_root"} <= set(p)


# ── search ───────────────────────────────────────────────────────────────
def test_search_finds_a_todo(store_home):
    bridge.todo_add("refactor the lexer")
    hits = bridge.search("lexer")
    assert hits and hits[0]["type"] == "todo"
    assert hits[0]["module"] == "desk"


def test_search_finds_a_memory_fact(store_home):
    bridge.memory_add("the deploy key lives in the vault")
    assert any(h["type"] == "fact" for h in bridge.search("deploy"))


def test_search_is_empty_for_a_blank_query():
    assert bridge.search("   ") == []


def test_search_hits_carry_jump_coordinates(store_home):
    bridge.todo_add("something")
    for h in bridge.search("something"):
        assert h["module"] and h["label"]


def test_search_is_capped(store_home):
    for i in range(40):
        bridge.todo_add(f"item {i}")
    assert len(bridge.search("item", limit=5)) <= 5
