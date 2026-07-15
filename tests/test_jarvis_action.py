"""Tests for actionable Jarvis (Cycle 6: 'a' acts on top suggestion)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aion.ui.app import AiOSApp
from aion.core import Task, TaskState


@pytest.mark.asyncio
async def test_act_runs_top_suggestion_action():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        # simulate a failed task -> Jarvis suggests 'rerun'
        app.store.state.tasks = [
            Task(id="t1", label="boom", harness="demo", state=TaskState.FAILED)]
        app._poll_jarvis()
        assert app.store.state.suggestions
        assert app.store.state.suggestions[0].action == "rerun"
        before = len(app.store.state.logs)
        app.action_act()
        # running 'rerun' logs a Jarvis action + clears the suggestion
        assert any("Jarvis:" in l for l in app.store.state.logs[before:])
        assert app.store.state.suggestions == [] or \
            app.store.state.suggestions[0].action != "rerun"


@pytest.mark.asyncio
async def test_act_noop_when_no_suggestions():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.store.state.suggestions = []
        # should not raise
        app.action_act()
        assert True
