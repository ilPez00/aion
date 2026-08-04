"""FactoryHarness bridges the loop to a Task. Uses a stub subprocess so no
real agent runs."""
from __future__ import annotations

import subprocess
import types

import pytest

from aion.core import Bus, TaskRegistry, TaskState
from aion.harnesses import HARNESS_TYPES, FactoryHarness, HarnessConfig


def _harness(extra, max_steps=5, store=None):
    bus = Bus()
    reg = TaskRegistry(bus)
    cfg = HarnessConfig(id="factory", type="factory", name="Factory",
                        max_steps=max_steps, extra=extra)
    return FactoryHarness(cfg, bus, reg, store), reg


def _fake_subprocess(monkeypatch, script):
    """Patch subprocess.run so the loop's run_cmd returns scripted output.
    `script` is a list of (returncode, output); consumed in order."""
    calls = {"i": 0}

    def fake_run(cmd, **kw):
        i = calls["i"]
        calls["i"] += 1
        rc, out = script[min(i, len(script) - 1)]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_factory_registered():
    assert HARNESS_TYPES["factory"] is FactoryHarness


def test_nested_extra_block_is_loaded():
    """Regression: from_dict must merge a nested "extra" dict, not bury it
    under extra["extra"] (which silently returned None for every setting)."""
    cfg = HarnessConfig.from_dict({
        "id": "f", "type": "factory",
        "extra": {"command": "run {p}", "done_marker": "DONE"},
    })
    assert cfg.extra["command"] == "run {p}"
    assert cfg.extra["done_marker"] == "DONE"


@pytest.mark.asyncio
async def test_completes_on_marker(monkeypatch):
    _fake_subprocess(monkeypatch, [(0, "working"), (0, "TASK_COMPLETE")])
    h, reg = _harness({"command": "agent {p}", "done_marker": "TASK_COMPLETE"})
    task = reg.create("factory", "factory")

    await h.run(task, "build the thing")

    assert task.state == TaskState.DONE
    assert task.progress == 1.0
    assert any("stopped: done" in line for line in task.log)


@pytest.mark.asyncio
async def test_budget_exhaustion_is_not_success(monkeypatch):
    _fake_subprocess(monkeypatch, [(0, "still going")])
    h, reg = _harness({"command": "c", "done_marker": "NEVER"}, max_steps=3)
    task = reg.create("factory", "factory")

    await h.run(task, "p")

    # ran out of iterations without finishing -> INTERRUPTED, not DONE
    assert task.state == TaskState.INTERRUPTED


@pytest.mark.asyncio
async def test_agent_error_fails_task(monkeypatch):
    _fake_subprocess(monkeypatch, [(2, "boom")])
    h, reg = _harness({"command": "c"})
    task = reg.create("factory", "factory")

    await h.run(task, "p")

    assert task.state == TaskState.FAILED


@pytest.mark.asyncio
async def test_missing_command_fails_fast():
    h, reg = _harness({})       # no command anywhere
    task = reg.create("factory", "factory")
    await h.run(task, "p")
    assert task.state == TaskState.FAILED
    assert any("no command" in line for line in task.log)


@pytest.mark.asyncio
async def test_kill_before_run_cancels(monkeypatch):
    _fake_subprocess(monkeypatch, [(0, "x")])
    h, reg = _harness({"command": "c"})
    task = reg.create("factory", "factory")
    h.cancel(task)
    await h.run(task, "p")
    assert task.state == TaskState.CANCELLED


@pytest.mark.asyncio
async def test_a_coherence_window_without_a_scorer_is_refused_out_loud(monkeypatch):
    """A window with nothing scoring rounds is a guard that can never fire and
    a config that reads as though it will. Silently keeping it would let
    someone conclude the brain approved of a run it never saw."""
    _fake_subprocess(monkeypatch, [(0, "still going")])
    h, reg = _harness({"command": "c", "coherence_window": 3}, max_steps=2)
    task = reg.create("factory", "factory")

    await h.run(task, "p")

    assert any("coherence_window ignored" in line for line in task.log)
    assert task.state == TaskState.INTERRUPTED   # ran the budget, was not killed


@pytest.mark.asyncio
async def test_sustained_drift_ends_the_run_and_says_why(monkeypatch):
    _fake_subprocess(monkeypatch, [(0, f"fresh nonsense {i}") for i in range(9)])
    monkeypatch.setattr("aion.physis.score_text", lambda text: -0.9)
    h, reg = _harness({"command": "c", "coherence": True,
                       "coherence_window": 2}, max_steps=9)
    task = reg.create("factory", "factory")

    await h.run(task, "p")

    assert any("stopped: incoherent" in line for line in task.log)
    assert any("brain stopped recognising" in line for line in task.log)
    assert task.state == TaskState.INTERRUPTED


@pytest.mark.asyncio
async def test_an_unreachable_brain_lets_the_run_finish(monkeypatch):
    """score_text answers 0.0 for a dead brain. A guard that reads that as
    "bad" turns physis being down into every loop being killed."""
    _fake_subprocess(monkeypatch, [(0, f"fresh {i}") for i in range(4)])
    monkeypatch.setattr("aion.physis.score_text", lambda text: 0.0)
    h, reg = _harness({"command": "c", "coherence": True,
                       "coherence_window": 2, "coherence_floor": 0.0},
                      max_steps=4)
    task = reg.create("factory", "factory")

    await h.run(task, "p")

    assert not any("incoherent" in line for line in task.log)
