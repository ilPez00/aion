"""Tests for the store's agent tool implementations (Cycle 10)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.core import Task, TaskState
from aion.store import Store


class _FakeHarness:
    name = "Demo"
    tier = "cheap"


def _store():
    return Store(harnesses={"demo": _FakeHarness()})


def test_agent_state_tool_reports_counts():
    s = _store()
    s.registry.tasks["t1"] = Task(id="t1", label="x", harness="demo", state=TaskState.RUNNING)
    out = s._agent_state_tool()
    assert "running=1" in out
    assert "tasks=" in out


def test_agent_run_tool_unknown_harness():
    s = _store()
    out = s._agent_run_tool("nope", "hello")
    assert "unknown harness" in out


def test_agent_run_tool_valid_schedules():
    s = _store()
    out = s._agent_run_tool("demo", "say hi")
    assert "scheduled demo" in out


def test_agent_rerun_tool_no_failed():
    s = _store()
    out = s._agent_rerun_tool()
    assert "no failed tasks" in out


def test_agent_note_tool_logs():
    s = _store()
    before = len(s.memory.facts)
    out = s._agent_note_tool("groq key set")
    assert "noted" in out
    assert len(s.memory.facts) == before + 1
