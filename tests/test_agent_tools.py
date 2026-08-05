"""Tests for the store's agent tool implementations (Cycle 10).

The tests below the fold are about something the ones above cannot see. Each
of these checks a tool's RETURN STRING, and every one of them passed while
`_agent_run_tool` emitted a command the dispatcher could not parse — the
answer was right, the effect was wrong, and nothing joined the two.

The tool surface is a contract between four places:

    llm.py          the prompt telling the model which tools exist
    agent.ToolEnv   the fields a tool environment may carry
    agent.execute   the dispatch turning a parsed call into a callable
    store._chat     the environment the live cockpit builds

Nothing checked they agree, and they did not: `_chat` passed `think=`, which
`ToolEnv` has no field for, so building the environment raised TypeError and
EVERY agent chat message crashed before reaching the model. `think` was also
missing from `execute`'s dispatch and from the prompt — a feature wired at one
of its four ends.
"""
import asyncio
import inspect
import re
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.agent import ToolEnv, execute
from aion.core import Bus, Task, TaskState, load_config
from aion.store import Store

ROOT = Path(__file__).resolve().parents[1]
LLM_SRC = (ROOT / "src" / "aion" / "llm.py").read_text(encoding="utf-8")
STORE_SRC = (ROOT / "src" / "aion" / "store.py").read_text(encoding="utf-8")


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


# ── the four-way contract ───────────────────────────────────────────────────

def env_field_names() -> set[str]:
    return {f.name for f in fields(ToolEnv)}


def prompt_tool_names() -> set[str]:
    return set(re.findall(r"<tool ([a-z_]+)>", LLM_SRC))


def dispatch_names() -> set[str]:
    return set(re.findall(r'name == "([a-z_]+)"', inspect.getsource(execute)))


def store_env_kwargs() -> set[str]:
    """The keywords `_chat` actually passes to ToolEnv."""
    body = STORE_SRC.split("env = ToolEnv(")[1].split(")\n")[0]
    return set(re.findall(r"^\s*([a-z_]+)=", body, re.M))


def test_the_cockpit_builds_an_environment_toolenv_accepts():
    """The one that was broken. `think=` went to a dataclass with no such
    field, so every agent chat raised TypeError on construction — before the
    model, before any tool, before anything that could catch it."""
    extra = store_env_kwargs() - env_field_names()
    assert not extra, f"store passes fields ToolEnv lacks: {extra}"


def test_every_documented_tool_can_be_dispatched():
    """A tool in the prompt but not in `execute` is one the model was told
    works and gets "unsupported" back from."""
    missing = prompt_tool_names() - dispatch_names()
    assert not missing, f"documented but undispatchable: {missing}"


def test_every_dispatchable_tool_is_a_field():
    missing = dispatch_names() - env_field_names()
    assert not missing, f"dispatched but not a ToolEnv field: {missing}"


def test_every_documented_tool_is_wired_by_the_cockpit():
    """The other direction: a documented tool the cockpit never fills in is
    `env.has()` False — the model is told it works and gets "unknown"."""
    missing = prompt_tool_names() - store_env_kwargs()
    assert not missing, f"documented but not wired: {missing}"


def test_think_specifically_is_wired_everywhere():
    """Named because it was wired at one end out of four."""
    for where, names in (("ToolEnv", env_field_names()),
                         ("execute", dispatch_names()),
                         ("prompt", prompt_tool_names()),
                         ("store", store_env_kwargs())):
        assert "think" in names, f"think missing from {where}"


def test_building_the_live_tool_environment_does_not_raise():
    """`_chat` crashed here on every message."""
    s = Store(cfg=load_config(), bus=Bus())
    asyncio.run(s._chat("hello"))


def test_an_unknown_tool_is_reported_not_raised():
    _, obs = execute("<tool nosuch>x</tool>", ToolEnv())
    assert "unknown" in obs


def test_a_tool_that_raises_is_reported_not_raised():
    """A tool surface that can kill the chat loop is worse than one that
    returns a bad answer: the reply is lost either way, and so is the turn."""
    def boom(_):
        raise RuntimeError("nope")
    _, obs = execute("<tool mem>q</tool>", ToolEnv(mem=boom))
    assert "error" in obs and "nope" in obs


def test_think_round_trips_through_execute():
    seen = []
    _, obs = execute("<tool think>why is it slow</tool>",
                     ToolEnv(think=lambda q: seen.append(q) or "because"))
    assert seen == ["why is it slow"]
    assert "[think] because" in obs
