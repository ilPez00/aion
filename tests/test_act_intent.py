"""Tests for ACT intent -> runs top Jarvis suggestion (Cycle 9)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from aion.core import Intent, IntentType, Task, TaskState
from aion.ui.app import AiOSApp
from aion.jarvis import Suggestion


@pytest.mark.asyncio
async def test_act_intent_runs_top_suggestion():
    app = AiOSApp()
    async with app.run_test(size=(120, 40)):
        app.store.state.suggestions = [Suggestion("⚠ x failed", "rerun")]
        app.store.state.tasks = [
            Task(id="t1", label="boom", harness="demo", state=TaskState.FAILED)]
        before = len(app.store.state.logs)
        # ACT intent flows through store.handle (same path the router uses)
        app.store.handle(Intent(IntentType.ACT))
        assert any("Jarvis:" in l for l in app.store.state.logs[before:])
        assert app.store.state.suggestions == []
