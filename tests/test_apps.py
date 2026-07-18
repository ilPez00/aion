"""
Unit tests for the TUI app registry (apps.py) and the `app`/`apps` palette
commands in store.py — zero UI, deterministic via monkeypatched shutil.which.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import apps
from aion.store import Store
from aion.core import Bus, SessionStore


def _make_store(tmp_path):
    cfg = {
        "app_name": "aion",
        "workspaces": [
            {"id": "models", "title": "Models", "icon": "◈"},
            {"id": "term", "title": "Term", "icon": "▣"},
        ],
        "harnesses": [],
    }
    (tmp_path / "session.json").unlink(missing_ok=True)
    return Store(cfg=cfg, bus=Bus(), harnesses={},
                 store=SessionStore(tmp_path / "session.json"))


def test_resolve_first_installed_wins(monkeypatch):
    monkeypatch.setattr(apps.shutil, "which",
                        lambda b: "/usr/bin/" + b if b == "neomutt" else None)
    cmd, note = apps.resolve("mail")
    assert cmd == "neomutt"
    assert note == "neomutt"


def test_resolve_appends_args(monkeypatch):
    monkeypatch.setattr(apps.shutil, "which",
                        lambda b: "/usr/bin/" + b if b == "micro" else None)
    cmd, _ = apps.resolve("edit", "notes/plan.md")
    assert cmd == "micro notes/plan.md"


def test_resolve_missing_gives_install_hint(monkeypatch):
    monkeypatch.setattr(apps.shutil, "which", lambda b: None)
    cmd, note = apps.resolve("sheet")
    assert cmd is None
    assert "visidata" in note


def test_resolve_unknown_app():
    cmd, note = apps.resolve("doom")
    assert cmd is None
    assert "unknown app" in note


def test_list_apps_covers_registry(monkeypatch):
    monkeypatch.setattr(apps.shutil, "which", lambda b: None)
    lines = apps.list_apps()
    assert len(lines) == len(apps.APPS)
    assert all(line.startswith("app ") for line in lines)


def test_app_command_switches_to_term(tmp_path, monkeypatch):
    monkeypatch.setattr(apps.shutil, "which",
                        lambda b: "/usr/bin/" + b if b == "vd" else None)
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("app sheet")
    asyncio.run(run())
    assert st.state.term_command == "vd"
    assert st.cfg["workspaces"][st.state.active_ws]["id"] == "term"


def test_app_command_missing_logs_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(apps.shutil, "which", lambda b: None)
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("app mail")
    asyncio.run(run())
    assert st.state.term_command == ""
    assert st.cfg["workspaces"][st.state.active_ws]["id"] == "models"
    assert any("aerc" in line for line in st.state.logs)


def test_apps_command_lists_into_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(apps.shutil, "which", lambda b: None)
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("apps")
    asyncio.run(run())
    assert len(st.state.logs) >= len(apps.APPS)
