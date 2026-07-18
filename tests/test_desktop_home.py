"""
Unit tests for the desktop homescreen additions: todos.py, profile.py,
observer.py and their dashboard/store wiring. Zero UI, deterministic.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.todos import TodoStore
from aion import profile as prof
from aion.observer import Observer
from aion.dashboard import collect_dashboard
from aion.store import Store
from aion.core import Bus, SessionStore


# ---- todos ---------------------------------------------------------------
def test_todo_roundtrip(tmp_path):
    st = TodoStore(tmp_path / "todos.md")
    st.add("write thesis chapter")
    st.add("fix cyclops build")
    assert st.open_count() == 2
    assert st.done(1)
    items = st.items()
    assert items[0]["text"] == "fix cyclops build"      # open first
    assert items[-1]["done"] is True
    assert st.rm(1)
    assert len(st.items()) == 1
    assert not st.done(99)


def test_todo_markdown_format(tmp_path):
    p = tmp_path / "todos.md"
    st = TodoStore(p)
    st.add("alpha")
    st.done(1)
    assert p.read_text() == "- [x] alpha\n"


# ---- profile / trackers --------------------------------------------------
def test_profile_scan_generates_trackers(tmp_path):
    home = tmp_path / "home"
    (home / "repo1" / ".git").mkdir(parents=True)
    (home / "Documents").mkdir()
    (home / "Documents" / "a.md").write_text("x")
    (home / "Downloads").mkdir()
    (home / "Downloads" / "big.bin").write_bytes(b"0" * 2048)
    p = prof.scan(["dev", "writing", "data"], home=home)
    ids = {t["id"] for t in p["trackers"]}
    assert {"repos", "docs_size", "dl_size", "disk_free"} <= ids
    repo_t = next(t for t in p["trackers"] if t["id"] == "repos")
    assert repo_t["value"] == 1
    assert p["disk"]      # top-level dirs sized


def test_profile_delta_arrows(tmp_path):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    p1 = prof.scan(["data"], home=home)
    (home / "Downloads" / "new.bin").write_bytes(b"0" * (3 * 1024 * 1024))
    p2 = prof.scan(["data"], home=home, prev=p1)
    t = next(t for t in p2["trackers"] if t["id"] == "dl_size")
    assert "↑" in prof.tracker_line(t)


def test_profile_save_load(tmp_path):
    path = tmp_path / "profile.json"
    prof.save({"scopes": ["dev"], "scanned_at": 1, "trackers": [], "disk": []}, path)
    assert prof.load(path)["scopes"] == ["dev"]
    assert prof.load(tmp_path / "missing.json") is None


# ---- observer ------------------------------------------------------------
def test_observer_heuristics():
    o = Observer()
    o.attach("micro")
    o.tick("editing file\nall good")
    assert "micro" in o.status_line and "err" not in o.status_line
    o.tick("Traceback (most recent call last)\nValueError: boom\nerror")
    assert "err" in o.status_line
    o.detach()
    assert not o.active and o.status_line == ""


def test_observer_ai_gating():
    o = Observer()
    o.attach("vd")
    o.tick("some screen")
    assert not o.want_ai_pass()          # ai disabled
    o.ai_enabled = True
    o._last_ai = 0
    assert o.want_ai_pass()
    prompt = o.begin_ai_pass()
    assert "some screen" in prompt
    assert not o.want_ai_pass()          # busy
    o.set_ai_result("User edits a CSV in visidata.")
    assert o.ai_line.startswith("◉")
    o.set_ai_result("⚠️ LLM unavailable (tried FCM).")
    assert "unavailable" not in o.ai_line   # error replies filtered


# ---- store/dashboard wiring ----------------------------------------------
def _make_store(tmp_path):
    cfg = {"app_name": "aion",
           "workspaces": [{"id": "desktop", "title": "Desktop", "icon": "⌂"}],
           "harnesses": []}
    (tmp_path / "session.json").unlink(missing_ok=True)
    st = Store(cfg=cfg, bus=Bus(), harnesses={},
               store=SessionStore(tmp_path / "session.json"))
    st.todos = TodoStore(tmp_path / "todos.md")
    return st


def test_todo_command_and_dashboard(tmp_path):
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("todo ship the HUD")
        await st._run_command("todo done 1")
    asyncio.run(run())
    d = collect_dashboard(st.state, st.cfg, todos=st.todos)
    assert d.todos and d.todos[0]["done"] is True
    assert d.todos_open == 0
    assert d.launcher   # registry availability present


def test_setup_command_scans_in_background(tmp_path, monkeypatch):
    st = _make_store(tmp_path)
    monkeypatch.setattr(prof, "PROFILE_PATH", tmp_path / "profile.json")
    monkeypatch.setattr(prof.Path, "home", classmethod(lambda cls: tmp_path))

    async def run():
        await st._run_command("setup dev bogus")
    asyncio.run(run())
    for _ in range(100):                 # scan runs in a worker thread
        if "profile" in st.state.stats:
            break
        time.sleep(0.05)
    p = st.state.stats.get("profile")
    assert p and p["scopes"] == ["dev"]  # bogus scope filtered
    assert any(t["id"] == "disk_free" for t in p["trackers"])


def test_desktop_panel_renders_new_sections():
    """Boot the real app headless, skip the intro, render the desktop panel."""
    import pytest
    pytest.importorskip("textual")
    from aion.ui.app import AiOSApp

    async def run():
        app = AiOSApp()
        async with app.run_test(size=(140, 50)):
            app.action_skip_boot()
            theme = app.cfg["theme"]
            panel = app._desktop_panel(theme)
            for section in ("01 STATUS", "02 LAUNCHER", "03 TODO",
                            "04 SESSIONS", "05 DATA", "06 SYSTEM",
                            "07 AGENTS", "08 ACTIVITY", "09 QUICK"):
                assert section in panel
            app._render_right()          # observer branch must not raise
    asyncio.run(run())


def test_observe_command_toggles(tmp_path):
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("observe ai")
        assert st.state.observer_ai is True
        await st._run_command("observe off")
        assert st.state.observer_ai is False
    asyncio.run(run())
