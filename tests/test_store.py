"""
Unit tests for the aion brain (store.py) — zero UI, fast, deterministic.
These prove the intent->state logic without ever touching Textual.

NOTE: _spawn/_respawn/_run_command are async and only *schedule* the harness
coroutine via create_task — so each test keeps the event loop alive while the
task runs (one asyncio.run per test that both spawns and awaits completion).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.core import Intent, IntentType, TaskState
from aion.store import Store
from aion.harnesses import build_harnesses
from aion.core import Bus, SessionStore, TaskRegistry


def _make_store(tmp_path, clean=True):
    cfg = {
        "app_name": "aion",
        "workspaces": [
            {"id": "models", "title": "Models", "icon": "◈"},
            {"id": "tasks", "title": "Tasks", "icon": "▤"},
            {"id": "agent", "title": "Agent", "icon": "✦"},
        ],
        "harnesses": [
            {"id": "demo", "type": "demo", "name": "Demo", "tier": "standard", "max_steps": 5},
            {"id": "shell", "type": "shell", "name": "Shell", "tier": "cheap", "max_steps": 3,
             "command": "echo step {n} && sleep 0.05"},
        ],
    }
    # start from a clean slate by default — a leftover session.json (from a
    # previous test or run) would ingest stale tasks and shift focus indices
    if clean:
        (tmp_path / "session.json").unlink(missing_ok=True)
    fs = SessionStore(tmp_path / "session.json")
    bus = Bus()
    registry = TaskRegistry(bus)
    harnesses = build_harnesses(cfg["harnesses"], bus, registry, fs)
    return Store(cfg, bus, harnesses, fs)


async def _run_until(tasks_getter, want_state, timeout=5.0):
    steps = int(timeout / 0.1)
    for _ in range(steps):
        await asyncio.sleep(0.1)
        if any(t.state == want_state for t in tasks_getter()):
            return True
    return False


def test_navigate_and_workspace_switch():
    s = _make_store(Path("/tmp/aion_ut"))
    assert s.state.active_ws == 0
    s.handle(Intent.navigate("right"))
    assert s.state.active_ws == 1
    s.handle(Intent.switch_workspace(index=2))
    assert s.state.active_ws == 2


def test_tier_routing_command():
    s = _make_store(Path("/tmp/aion_ut"))
    asyncio.run(s._run_command("tier cheap"))
    assert s.state.active_harness == "shell"


def test_spawn_runs_to_completion():
    s = _make_store(Path("/tmp/aion_ut"))

    async def go():
        await s._spawn("demo", "hello")
        assert await _run_until(lambda: s.registry.tasks.values(), TaskState.DONE)
    asyncio.run(go())
    assert any(t.state == TaskState.DONE and t.progress == 1.0
               for t in s.registry.tasks.values())


def test_pause_resume_cancel():
    s = _make_store(Path("/tmp/aion_ut"))

    async def go():
        await s._spawn("demo", "x")
        assert await _run_until(lambda: s.registry.tasks.values(), TaskState.RUNNING)
        tid = next(t.id for t in s.registry.tasks.values() if t.harness == "demo")
        task = s.registry.tasks[tid]
        s.state.active_ws = 1
        s.state.focus = 0
        s.handle(Intent(IntentType.PAUSE))
        await asyncio.sleep(0.2)
        assert task.paused is True
        s.handle(Intent(IntentType.RESUME))
        await asyncio.sleep(0.2)
        assert task.paused is False
        s.handle(Intent(IntentType.CANCEL))
        await asyncio.sleep(0.1)
        assert task.state == TaskState.CANCELLED
    asyncio.run(go())


def test_crash_restore_as_interrupted():
    tmp = Path("/tmp/aion_ut2")
    s = _make_store(tmp)

    async def go():
        await s._spawn("demo", "crashme")
        assert await _run_until(lambda: s.registry.tasks.values(), TaskState.RUNNING)
        s.store.save(s.registry.tasks)
    asyncio.run(go())
    s2 = _make_store(tmp, clean=False)
    assert any(t.state == TaskState.INTERRUPTED for t in s2.registry.tasks.values())


if __name__ == "__main__":
    test_navigate_and_workspace_switch()
    test_tier_routing_command()
    test_spawn_runs_to_completion()
    test_pause_resume_cancel()
    test_crash_restore_as_interrupted()
    print("OK: all store unit tests pass")
