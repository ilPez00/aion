"""ResearchHarness bridges the pure loop to a Task. Tests use a fake web
module so no network or API key is touched."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from aion.core import Bus, TaskRegistry, TaskState
from aion.harnesses import HARNESS_TYPES, HarnessConfig, ResearchHarness


def _install_fake_web(monkeypatch, *, answer="the answer", rounds_sources=2,
                      hang=False):
    """Replace aion.web with a stub the harness imports inside run()."""
    fake = types.ModuleType("aion.web")

    def web_search(query, n):
        return [{"title": f"S{i}", "url": f"http://u/{i}", "snippet": "x"}
                for i in range(rounds_sources)]

    def chat(messages, **kw):
        sysmsg = messages[0]["content"].lower()
        if "search queries" in sysmsg:
            return None                      # -> single-query plan
        if "concise" in sysmsg or "cite" in sysmsg:
            return answer
        return None

    fake.web_search = web_search
    fake.chat = chat
    monkeypatch.setitem(sys.modules, "aion.web", fake)


def _harness(store=None):
    bus = Bus()
    reg = TaskRegistry(bus)
    cfg = HarnessConfig(id="research", type="research", name="Research",
                        max_steps=3)
    return ResearchHarness(cfg, bus, reg, store), reg


def test_research_registered_in_type_map():
    assert HARNESS_TYPES["research"] is ResearchHarness


@pytest.mark.asyncio
async def test_run_completes_and_logs_answer(monkeypatch):
    _install_fake_web(monkeypatch, answer="Berlin is the capital.")
    h, reg = _harness()
    task = reg.create("research: capital", "research")

    await h.run(task, "what is the capital")

    assert task.state == TaskState.DONE
    assert task.progress == 1.0
    assert any("Berlin is the capital." in line for line in task.log)
    assert any(line.startswith("  [1]") for line in task.log)  # cited sources


@pytest.mark.asyncio
async def test_run_marks_cancelled_when_killed(monkeypatch):
    _install_fake_web(monkeypatch)
    h, reg = _harness()
    task = reg.create("research", "research")
    h.cancel(task)      # kill before the first reporter callback

    await h.run(task, "q")

    assert task.state == TaskState.CANCELLED


@pytest.mark.asyncio
async def test_run_checkpoints_through_store(monkeypatch):
    _install_fake_web(monkeypatch)
    saves = []

    class Store:
        def save(self, tasks):
            saves.append(len(tasks))

    h, reg = _harness(store=Store())
    task = reg.create("research", "research")
    await h.run(task, "q")
    assert saves, "harness should checkpoint via the store"


@pytest.mark.asyncio
async def test_run_fails_soft_on_engine_error(monkeypatch):
    fake = types.ModuleType("aion.web")
    fake.web_search = lambda q, n: (_ for _ in ()).throw(RuntimeError("boom"))
    fake.chat = lambda m, **k: None
    monkeypatch.setitem(sys.modules, "aion.web", fake)

    h, reg = _harness()
    task = reg.create("research", "research")
    await h.run(task, "q")
    assert task.state == TaskState.FAILED
    assert any("error" in line for line in task.log)
