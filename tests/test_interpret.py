"""
Unit tests for the plain-language palette layer (interpret.py) and its
store wiring. Rule layer is pure; LLM fallback is mocked.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import interpret as itp
from aion import apps
from aion.store import Store
from aion.core import Bus, SessionStore
from aion.todos import TodoStore


# ---- rule layer ----------------------------------------------------------
def test_launch_phrases():
    assert itp.interpret("open my mail") == "app mail"
    assert itp.interpret("check email") == "app mail"
    assert itp.interpret("launch the spreadsheet") == "app sheet"
    assert itp.interpret("open files") == "app files"
    assert itp.interpret("spreadsheet") == "app sheet"
    assert itp.interpret("edit plan.md") == "app edit plan.md"
    assert itp.interpret("open editor plan.md") == "app edit plan.md"


def test_todo_phrases():
    assert itp.interpret("remind me to buy milk") == "todo buy milk"
    assert itp.interpret("remember to call mom") == "todo call mom"
    assert itp.interpret("add task ship the HUD") == "todo ship the HUD"
    assert itp.interpret("done 3") == "todo done 3"
    assert itp.interpret("finished #2") == "todo done 2"


def test_setup_scan_observe_goto():
    assert itp.interpret("i use this for coding and writing") == "setup dev writing"
    assert itp.interpret("i use my computer for photos and music") == "setup media"
    assert itp.interpret("scan my disk") == "scan"
    assert itp.interpret("watch this") == "observe ai"
    assert itp.interpret("stop watching") == "observe off"
    assert itp.interpret("go to vault") == "goto vault"
    assert itp.interpret("list apps") == "apps"
    assert itp.interpret("help") == "help"


def test_no_false_positives():
    assert itp.interpret("explain quantum entanglement") is None
    assert itp.interpret("hello") is None
    assert itp.interpret("") is None


def test_llm_translate_parses_reply(monkeypatch):
    monkeypatch.setattr("aion.llm.chat_send",
                        lambda s, m, timeout=10: "app mail")
    assert itp.llm_translate("i want to read my messages") == "app mail"
    monkeypatch.setattr("aion.llm.chat_send",
                        lambda s, m, timeout=10: "NONE")
    assert itp.llm_translate("tell me a joke") is None
    monkeypatch.setattr("aion.llm.chat_send",
                        lambda s, m, timeout=10: "⚠️ LLM unavailable (tried FCM).")
    assert itp.llm_translate("whatever") is None
    monkeypatch.setattr("aion.llm.chat_send",
                        lambda s, m, timeout=10: "rm -rf /")
    assert itp.llm_translate("nasty") is None      # non-whitelisted command


# ---- store wiring --------------------------------------------------------
def _make_store(tmp_path):
    cfg = {"app_name": "aion",
           "workspaces": [{"id": "desktop", "title": "D", "icon": "⌂"},
                          {"id": "term", "title": "T", "icon": "▣"},
                          {"id": "vault", "title": "V", "icon": "▦"}],
           "harnesses": []}
    (tmp_path / "session.json").unlink(missing_ok=True)
    st = Store(cfg=cfg, bus=Bus(), harnesses={},
               store=SessionStore(tmp_path / "session.json"))
    st.todos = TodoStore(tmp_path / "todos.md")
    return st


def test_plain_language_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(apps.shutil, "which",
                        lambda b: "/usr/bin/" + b if b == "aerc" else None)
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("open my mail")
    asyncio.run(run())
    assert st.state.term_command == "aerc"
    assert st.cfg["workspaces"][st.state.active_ws]["id"] == "term"
    assert any("→ app mail" in line for line in st.state.logs)


def test_plain_language_todo_and_goto(tmp_path):
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("remind me to buy milk")
        await st._run_command("go to vault")
    asyncio.run(run())
    assert st.todos.open_count() == 1
    assert st.cfg["workspaces"][st.state.active_ws]["id"] == "vault"


def test_llm_fallback_used_when_rules_miss(tmp_path, monkeypatch):
    st = _make_store(tmp_path)
    monkeypatch.setattr(itp, "llm_translate", lambda t, timeout=10: "todo call mom")

    async def run():
        await st._run_command("could you note down that i must call mom")
    asyncio.run(run())
    assert st.todos.open_count() == 1


def test_help_command_logs_examples(tmp_path):
    st = _make_store(tmp_path)

    async def run():
        await st._run_command("help")
    asyncio.run(run())
    assert any("open mail" in line for line in st.state.logs)
