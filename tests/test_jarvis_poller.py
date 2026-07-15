"""Tests for the proactive Jarvis poller wired into the app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui.app import AiOSApp
from aion.core import Task, TaskState


def test_jarvis_poller_updates_state_and_feed():
    app = AiOSApp()
    # simulate a failed task in the live state
    app.store.state.tasks = [
        Task(id="t1", label="boom", harness="demo", state=TaskState.FAILED)
    ]
    # force a poll
    app._poll_jarvis()
    assert app.store.state.suggestions, "expected at least one suggestion"
    assert any("failed" in s.lower() for s in app.store.state.suggestions)
    # top suggestion lands in the activity feed (logs)
    assert app.store.state.logs and "failed" in app.store.state.logs[-1].lower()


def test_jarvis_poller_no_spam_on_repeat():
    app = AiOSApp()
    app.store.state.tasks = [
        Task(id="t1", label="boom", harness="demo", state=TaskState.FAILED)
    ]
    app._poll_jarvis()
    first_len = len(app.store.state.logs)
    # run again — same suggestion should NOT be appended again
    app._poll_jarvis()
    assert len(app.store.state.logs) == first_len, "poller should not repeat identical suggestion"
