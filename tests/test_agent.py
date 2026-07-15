"""Tests for the agent tool loop (Cycle 10)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.agent import ToolEnv, parse_tools, strip_tools, execute


def test_parse_single_tool():
    calls = parse_tools("Let me run it. <tool run>demo hello</tool> done")
    assert calls == [("run", "demo hello")]


def test_parse_multiple_tools():
    text = "<tool rerun></tool> then <tool mem>status</tool>"
    assert parse_tools(text) == [("rerun", ""), ("mem", "status")]


def test_strip_tools():
    assert strip_tools("hi <tool run>demo</tool> bye") == "hi bye"


def test_execute_runs_callable_and_returns_observation():
    env = ToolEnv(run=lambda h, p: f"ran {h}/{p}", state=lambda: "2 tasks")
    reply, obs = execute("ok <tool run>demo hello</tool>", env)
    assert reply == "ok"
    assert "[run] ran demo/hello" in obs


def test_execute_unknown_tool_tagged():
    env = ToolEnv()
    _, obs = execute("<tool bogus>x</tool>", env)
    assert "unknown" in obs


def test_execute_no_tools_returns_text_untouched():
    env = ToolEnv(run=lambda h, p: "x")
    reply, obs = execute("just talking", env)
    assert reply == "just talking"
    assert obs == ""
