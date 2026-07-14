"""
Pipeline test: drive the FULL aion app headless via Textual run_test and
assert the end-to-end flow (intent -> store -> harness -> live state) works,
plus a lightweight performance assertion (re-renders must be cheap, not a
full teardown+remount of the widget tree).
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui.app import AiOSApp
from aion.core import TOPIC_INTENT, Intent, IntentType, TaskState


@pytest.mark.asyncio
async def test_pipeline():
    Path.home().joinpath(".aion", "session.json").unlink(missing_ok=True)
    app = AiOSApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # --- tier routing flows through the full app ---
        await app.bus.publish(TOPIC_INTENT, Intent.command("tier cheap"))
        await pilot.pause()
        assert app.store.state.active_harness == "shell"

        # --- spawn a task via the store/app and watch it run ---
        await app.bus.publish(TOPIC_INTENT, Intent.command("demo hello"))
        await pilot.pause(0.6)
        tid = next(t.id for t in app.store.registry.tasks.values() if t.harness == "demo")
        task = app.store.registry.tasks[tid]
        assert task.state.value == "running"
        assert len(app._center) >= 1   # stable widget tree exists

        # --- performance: 10 progress updates should NOT remount widgets ---
        before = len(app._center)
        for _ in range(10):
            app.store.registry.set_progress(task, min(1.0, task.progress + 0.01))
            app._render_all()
        after = len(app._center)
        assert before == after, f"widget tree remounted: {before} -> {after}"

        # --- pause/resume via bound keys (intuitive: just press p) ---
        app.store.state.active_ws = 1
        app.store.state.focus = 0
        app.action_pause()
        await pilot.pause(0.3)
        assert task.paused is True
        app.action_resume()
        await pilot.pause(0.3)
        assert task.paused is False

        # --- help overlay renders and toggles ---
        app.action_help()
        assert app.query_one("#help").display is True
        app.action_help()
        assert app.query_one("#help").display is False

    print("OK: pipeline (intent->store->harness->UI) + perf + intuition checks pass")


if __name__ == "__main__":
    asyncio.run(test_pipeline())
