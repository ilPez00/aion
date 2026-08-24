"""tests for life wiring: harness poller, voice vocab, jarvis suggestions."""
from __future__ import annotations

import asyncio
import json

import pytest

from aion.core import Bus, TaskRegistry, TOPIC_STATS
from aion.harnesses import HarnessConfig, LifeHarness


def _cfg(**extra) -> HarnessConfig:
    return HarnessConfig.from_dict({
        "id": "life", "type": "life", "name": "Life HUD",
        "enabled": True, "extra": {"interval": 0.05, **extra},
    })


async def _noop(m) -> None:
    return None


def test_life_harness_start_is_async_and_publishes_snapshot(tmp_path):
    money = tmp_path / "money.md"
    money.write_text("- d | payment | pilot | 700 | paid\n")
    health = tmp_path / "health.json"
    health.write_text(json.dumps({"steps": 4000}))

    bus = Bus()
    got: list[dict] = []
    h = LifeHarness(_cfg(), bus, TaskRegistry(bus), store=None)
    # env override BEFORE from_env runs inside _poll
    import os
    os.environ["AION_LIFE_MONEY_FILE"] = str(money)
    os.environ["AION_LIFE_HEALTH_FILE"] = str(health)
    try:
        async def run():
            async def rec(m):
                got.append(m)
            bus.subscribe(TOPIC_STATS, rec)
            await h.start()
            await asyncio.sleep(0.3)
            if h._task is not None:
                h._task.cancel()

        asyncio.run(run())
    finally:
        os.environ.pop("AION_LIFE_MONEY_FILE", None)
        os.environ.pop("AION_LIFE_HEALTH_FILE", None)

    assert got, "harness published nothing"
    metrics = [m["metrics"] for m in got if m.get("harness") == "life"]
    assert metrics and metrics[0]["ok"] is True
    doms = metrics[0]["snapshot"]["domains"]
    assert doms["money"]["paid_total"] == 700.0
    assert doms["fitness"]["steps"] == 4000


def test_voice_knows_life_words():
    from aion.voicecmd import MODULE_WORDS
    words = {w for ws in MODULE_WORDS.values() for w in ws.split()}
    for w in ("life", "money", "fitness", "social"):
        assert w in words


def test_voice_goto_life_resolves():
    from aion.voicecmd import parse
    a = parse("show life flow", confidence=0.9)
    assert getattr(a, "action", "") == "goto"
    assert (getattr(a, "args", {}) or {}).get("module") == "life"


class _FakeState:
    """Bare ViewState stand-in with only what suggest() touches."""

    def __init__(self, stats):
        self.stats = stats
        self.tasks = []
        self.swarm_dashboard = {}


def test_jarvis_flags_dark_domains_and_open_invoices():
    from aion.jarvis import suggest
    snap = {"domains": {
        "computer": {"ok": True, "cpu_pct": 5},
        "fitness": {"ok": False, "reason": "no file"},
        "social": {"ok": False, "reason": "unconfigured"},
        "money": {"ok": True, "paid_total": 0, "open_total": 1800,
                  "target_mrr": 2500, "entries": []},
    }}
    sugs = suggest(_FakeState({"life": {"snapshot": snap}}))
    text = " | ".join(s.text for s in sugs)
    assert "life domains dark" in text
    assert "1.800" in text  # € formatted, italian separator
