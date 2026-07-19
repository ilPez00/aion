"""test_health_context.py — peripheral health context wiring for agents."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aion.core import Bus, TaskRegistry
from aion.harnesses import AgentEntityHarness, HarnessConfig
from aion.agents import AgentStore


def _health_json(tmp_path: Path) -> Path:
    p = tmp_path / "health.json"
    p.write_text(json.dumps({"records": [
        {"date": "2026-07-19", "steps": 8432, "heart_rate": 72,
         "sleep_hours": 7.5, "active_calories": 420, "screen_time": 2.1},
        {"date": "2026-07-18", "steps": 9201, "heart_rate": 68,
         "sleep_hours": 6.8, "active_calories": 510, "screen_time": 1.8},
    ]}))
    return p


@pytest.mark.asyncio
async def test_health_context_in_poll(tmp_path: Path):
    hp = _health_json(tmp_path)
    bus = Bus()
    got = {}
    async def cap(msg):
        got["metrics"] = msg.get("metrics")
    bus.subscribe("stats", cap)
    cfg = HarnessConfig.from_dict({
        "id": "agent_entity", "type": "agent_entity",
        "health_path": str(hp), "interval": 1.0,
    })
    h = AgentEntityHarness(cfg, bus, TaskRegistry(bus))
    await h.poll_once()
    await asyncio.sleep(0.02)
    assert got["metrics"]["ok"] is True
    hc = got["metrics"].get("health_context", {})
    assert hc.get("steps") == 8432
    assert hc.get("heart_rate") == 72
    assert hc.get("sleep_hours") == 7.5


@pytest.mark.asyncio
async def test_health_context_no_data(tmp_path: Path):
    bus = Bus()
    got = {}
    async def cap(msg):
        got["metrics"] = msg.get("metrics")
    bus.subscribe("stats", cap)
    cfg = HarnessConfig.from_dict({
        "id": "agent_entity", "type": "agent_entity",
        "health_path": str(tmp_path / "nonexistent.json"), "interval": 1.0,
    })
    h = AgentEntityHarness(cfg, bus, TaskRegistry(bus))
    await h.poll_once()
    await asyncio.sleep(0.02)
    assert got["metrics"]["ok"] is True
    assert got["metrics"].get("health_context", {}) == {}


@pytest.mark.asyncio
async def test_health_memory_injected(tmp_path: Path):
    hp = _health_json(tmp_path)
    bus = Bus()
    got = {}
    async def cap(msg):
        got["metrics"] = msg.get("metrics")
    bus.subscribe("stats", cap)
    agent_path = tmp_path / "agents.json"
    store = AgentStore(path=agent_path)
    a = store.create("Alice")
    cfg = HarnessConfig.from_dict({
        "id": "agent_entity", "type": "agent_entity",
        "health_path": str(hp), "interval": 1.0,
    })
    h = AgentEntityHarness(cfg, bus, TaskRegistry(bus))
    h._agent_store = store
    await h.poll_once()
    await asyncio.sleep(0.02)
    agent = store.get(a.id)
    assert agent is not None
    health_mems = [m for m in agent.memory_entries if m.kind == "health"]
    assert len(health_mems) == 1
    assert "8432 steps" in health_mems[0].text
    assert "72.0 bpm" in health_mems[0].text
