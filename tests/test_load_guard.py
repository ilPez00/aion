"""Workspace data loads must not close a render->load->event->render loop.

_current_items() runs on every render and each loader publishes on a bus topic
the app re-renders from. Unguarded, visiting the hermes/skills/settings
workspace spun that cycle ~340x/s and starved the event loop (UI froze).
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.store import Store  # noqa: E402


def _store() -> Store:
    return Store(cfg={"workspaces": [{"id": "hermes", "title": "H", "icon": "x"}],
                      "harnesses": []})


def test_spawn_load_is_not_reentrant():
    """A second spawn while the first is in flight is dropped."""
    async def main():
        s = _store()
        calls = []

        async def slow():
            calls.append(1)
            await asyncio.sleep(0.2)

        for _ in range(50):          # simulate 50 render passes
            s._spawn_load("hermes", slow)
        await asyncio.sleep(0.05)
        assert len(calls) == 1, f"expected 1 load, got {len(calls)}"
    asyncio.run(main())


def test_spawn_load_respects_interval():
    """After a load completes, re-spawning inside the interval is dropped."""
    async def main():
        s = _store()
        calls = []

        async def quick():
            calls.append(1)

        s._spawn_load("hermes", quick)
        await asyncio.sleep(0.05)
        assert len(calls) == 1
        for _ in range(20):
            s._spawn_load("hermes", quick)
        await asyncio.sleep(0.05)
        assert len(calls) == 1, "interval did not suppress re-load"
    asyncio.run(main())


def test_spawn_load_allows_reload_after_interval():
    async def main():
        s = _store()
        calls = []

        async def quick():
            calls.append(1)

        s._spawn_load("hermes", quick)
        await asyncio.sleep(0.05)
        # pretend the interval elapsed
        s._load_last["hermes"] = time.time() - (Store.LOAD_MIN_INTERVAL_S + 1)
        s._spawn_load("hermes", quick)
        await asyncio.sleep(0.05)
        assert len(calls) == 2, f"expected reload after interval, got {len(calls)}"
    asyncio.run(main())


def test_spawn_load_keys_are_independent():
    async def main():
        s = _store()
        calls = []

        async def quick():
            calls.append(1)

        s._spawn_load("hermes", quick)
        s._spawn_load("skills", quick)
        s._spawn_load("settings", quick)
        await asyncio.sleep(0.05)
        assert len(calls) == 3
    asyncio.run(main())


def test_inflight_clears_after_failure():
    """A raising loader must still release its in-flight slot."""
    async def main():
        s = _store()

        async def boom():
            raise RuntimeError("nope")

        s._spawn_load("hermes", boom)
        await asyncio.sleep(0.05)
        assert "hermes" not in s._load_inflight
    asyncio.run(main())
