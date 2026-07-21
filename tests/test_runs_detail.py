"""The Runs right-rail follows focus and shows a run's full output."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aion.core import TaskState
from aion.ui.app import AiOSApp


def _plain(lines):
    return "\n".join(re.sub(r"\[/?[^\]]*\]", "", ln) for ln in lines)


def _ws_index(app, wid):
    return [w["id"] for w in app.cfg["workspaces"]].index(wid)


@pytest.mark.asyncio
async def test_detail_empty_outside_runs_workspace():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.store.state.active_ws = _ws_index(app, "tasks")
        assert app._run_detail_lines(app.cfg["theme"]) == []


@pytest.mark.asyncio
async def test_detail_shows_full_log_of_focused_run():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)) as pilot:
        t = app.store.registry.create("DeepResearch: q", "research")
        t.state = TaskState.DONE
        t.log = ["Tokio is the dominant runtime [1]", "  [1] tokio.rs"]
        app.store._task_prompts[t.id] = "best rust async runtime"
        app.store.state.active_ws = _ws_index(app, "runs")
        app.store.state.runs_tab = "results"
        app.store.state.focus = 1          # 0 is the tab bar
        await pilot.pause()

        text = _plain(app._run_detail_lines(app.cfg["theme"]))
        assert t.id in text
        assert "research" in text
        assert "Tokio is the dominant runtime" in text
        assert "best rust async runtime" in text


@pytest.mark.asyncio
async def test_detail_empty_when_tab_bar_is_focused():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)) as pilot:
        t = app.store.registry.create("Factory Loop: x", "factory")
        t.state = TaskState.RUNNING
        app.store.state.active_ws = _ws_index(app, "runs")
        app.store.state.focus = 0          # the tab bar, not a run
        await pilot.pause()
        assert app._run_detail_lines(app.cfg["theme"]) == []
