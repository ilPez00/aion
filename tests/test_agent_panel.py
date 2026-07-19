"""Tests for agent workspace side-by-side compare rendering."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.text import Text
from aion.ui.app import AiOSApp


def _make_app():
    app = AiOSApp()
    app.cfg["theme"] = app.cfg["themes"]["jarvis"]
    # point at Agent workspace
    ws_ids = [w["id"] for w in app.cfg["workspaces"]]
    app.store.state.active_ws = ws_ids.index("agent")
    return app


def test_agent_panel_compare_render():
    app = _make_app()
    # simulate a finished compare result on the store
    app.store.state.compare_result = {
        "prompt": "explain recursion briefly",
        "answers": {"fcm": "Recursion is when a function calls itself. " * 5,
                     "groq": "A function that invokes itself until a base case. " * 5},
        "done": True,
    }
    theme = app.cfg["theme"]
    out = app._agent_panel(theme)
    # both providers shown
    assert "fcm" in out and "groq" in out
    # no rich markup errors
    for ln in out.split("\n"):
        Text.from_markup(ln)


def test_agent_panel_chat_still_works():
    app = _make_app()
    from aion.llm import ChatSession, ChatMessage
    app.store.chat = ChatSession()
    app.store.chat.add("user", "hello there friend")
    app.store.chat.add("assistant", "hi! how can I help?")
    theme = app.cfg["theme"]
    out = app._agent_panel(theme)
    assert "hello there" in out
    for ln in out.split("\n"):
        Text.from_markup(ln)
