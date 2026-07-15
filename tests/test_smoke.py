"""
Headless smoke test for aion.

Boots the real TUI (Textual run_test, no TTY) and verifies the multi-harness
cockpit still wires up end-to-end after the reactivity/store refactor.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui.app import AiOSApp
from aion.core import TOPIC_INTENT, Intent, IntentType, TaskState, Task


@pytest.mark.asyncio
async def test_boot_and_run():
    # isolate from any leftover session state
    Path.home().joinpath(".aion", "session.json").unlink(missing_ok=True)
    app = AiOSApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # cockpit renders with the test config (tiers set)
        assert app.cfg["app_name"] == "aion"
        assert len(app.harnesses) >= 1
        assert any(h.tier == "cheap" for h in app.harnesses.values())

        # switching workspace via Intent
        await app.bus.publish(TOPIC_INTENT, Intent.switch_workspace(index=3))
        await pilot.pause()
        assert app.store.state.active_ws == 3

        # spawn a demo task through the store
        await app.bus.publish(TOPIC_INTENT, Intent.command("demo hello world"))
        await pilot.pause(0.6)
        tasks = list(app.store.registry.tasks.values())
        assert len(tasks) >= 1
        tid = next(t.id for t in tasks if t.harness == "demo")
        task = app.store.registry.tasks[tid]
        assert task.state == TaskState.RUNNING

        # let it finish (demo max_steps=20 * 0.15s ≈ 3s)
        for _ in range(40):
            await pilot.pause(0.2)
            if any(t.state == TaskState.DONE and t.progress == 1.0 for t in app.store.registry.tasks.values()):
                break
        assert any(t.state == TaskState.DONE and t.progress == 1.0 for t in app.store.registry.tasks.values()), \
            "demo task did not complete"
        print("OK: cockpit booted, intent->store->harness flow verified")


if __name__ == "__main__":
    asyncio.run(test_boot_and_run())
