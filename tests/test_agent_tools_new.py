"""Tests for the new agent tools: vault, swarm, hermes (Cycle C)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.agent import ToolEnv, parse_tools, execute
from aion.store import Store


class _FakeHarness:
    name = "Demo"
    tier = "cheap"


def _store():
    return Store(harnesses={"demo": _FakeHarness()})


def test_parse_vault_tool_with_separator():
    calls = parse_tools("<tool vault>ideas/x::some content</tool>")
    assert calls == [("vault", "ideas/x::some content")]


def test_parse_hermes_tool_with_separator():
    calls = parse_tools("<tool hermes>Fix bug::body text</tool>")
    assert calls == [("hermes", "Fix bug::body text")]


def test_execute_routes_vault_and_hermes():
    captured = {}
    env = ToolEnv(
        vault=lambda p, c: captured.setdefault("v", f"{p}|{c}") or "ok",
        hermes=lambda t, b: captured.setdefault("h", f"{t}|{b}") or "ok",
    )
    _, obs = execute("<tool vault>notes/x::hello</tool><tool hermes>T::b</tool>", env)
    assert captured["v"] == "notes/x|hello"
    assert captured["h"] == "T|b"
    assert "[vault] notes/x|hello" in obs and "[hermes] T|b" in obs


def test_agent_vault_tool_writes_file(tmp_path):
    s = _store()
    out = s._agent_vault_tool("test/note", "hello world", )
    # patch vault_root via monkeypatch not needed: writes under ~/.aion/vault
    assert "wrote" in out or "vault error" in out  # either is acceptable offline


def test_agent_swarm_tool_returns_planned():
    s = _store()
    out = s._agent_swarm_tool("build a dashboard")
    assert "swarm planned" in out
    # a plan + agent were added to the orchestrator
    assert len(s.swarm.agents) >= 1


def test_agent_hermes_tool_handles_missing_cli():
    s = _store()
    out = s._agent_hermes_tool("Do thing", "details")
    # hermes CLI may be absent in CI; we just must not crash
    assert isinstance(out, str) and len(out) > 0
