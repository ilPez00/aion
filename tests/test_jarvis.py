"""Tests for the proactive Jarvis suggestion engine."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.store import ViewState, Store
from aion.core import Task, TaskState
from aion.jarvis import suggest


def _state(**kw):
    s = ViewState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_jarvis_flags_failed_tasks():
    s = _state(tasks=[Task(id="t1", label="x", harness="demo", state=TaskState.FAILED)])
    out = suggest(s)
    assert any("failed" in o.text.lower() for o in out)
    # failed-task suggestion is actionable
    assert out[0].action == "rerun"


def test_jarvis_flags_high_cpu():
    s = _state(stats={"system": {"cpu_pct": 92}})
    out = suggest(s)
    assert any("CPU" in o.text for o in out)


def test_jarvis_flags_disk_full():
    s = _state(stats={"system": {"disk_pct": 95}})
    out = suggest(s)
    assert any("Disk" in o.text for o in out)


def test_jarvis_flags_blocked_swarm():
    s = _state(swarm_dashboard={"blocked": True})
    out = suggest(s)
    assert any("blocked" in o.text.lower() for o in out)


def test_jarvis_idle_suggests_demo():
    s = _state(tasks=[])
    out = suggest(s)
    assert any("idle" in o.text.lower() for o in out)
    # idle suggestion is actionable
    assert out[0].action == "run demo hello"


def test_jarvis_clean_state_quiet():
    s = _state(tasks=[Task(id="t1", label="x", harness="demo", state=TaskState.RUNNING)],
               stats={"system": {"cpu_pct": 20, "ram_pct": 30, "disk_pct": 40}})
    out = suggest(s)
    # running task + healthy stats => no alert noise
    assert not any("failed" in o.text.lower() or "CPU" in o.text for o in out)
