"""
Responsiveness regression: keep render-path work off the event loop.

These prove the two HUD stall fixes without building a full Store:
  * `_detect_external_agents` never blocks the caller on `pgrep`
  * `_collect_workflows` throttles so keypress-driven renders don't rebuild
"""
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.store import Store


def _bare():
    """Store without __init__ — tests only the render-path helpers."""
    s = Store.__new__(Store)
    s._external_agents_cache = (0.0, [])
    s._external_agents_probing = False
    s._workflows_last = -10.0
    return s


def _stub_state():
    s = _bare()
    st = mock.Mock()
    st.stats = {}
    st.tasks = []
    st.workflows = []
    s.state = st
    s.swarm = mock.Mock()
    s.swarm.agents = {}
    s.registry = mock.Mock()
    s.registry.tasks = {}
    s.board_store = mock.Mock()
    s.board_store.list_all.return_value = []
    return s


def test_detect_external_agents_does_not_block():
    s = _bare()
    # cache empty -> spawns a thread. Must return immediately, never wait on
    # `pgrep` (up to 2s per binary × 4 binaries in the worst case).
    t0 = time.monotonic()
    with mock.patch("threading.Thread"):
        out = s._detect_external_agents()
    dt = time.monotonic() - t0
    assert dt < 0.2, f"blocked {dt:.3f}s on pgrep path"
    assert out == []


def test_detect_external_agents_uses_cache():
    s = _bare()
    cached = [{"name": "OpenCode", "pid": 1, "age_s": 3}]
    s._external_agents_cache = (time.time(), cached)
    s._external_agents_probing = False
    with mock.patch("threading.Thread") as T:
        out = s._detect_external_agents()
    assert out == cached
    assert not T.called  # fresh cache -> no thread spawned


def test_detect_external_agents_one_in_flight():
    s = _bare()
    s._external_agents_probing = True
    with mock.patch("threading.Thread") as T:
        out = s._detect_external_agents()
    assert out == []
    assert not T.called  # do not stack threads while a refresh is running


def test_collect_workflows_throttles():
    import aion.workflows as _wf
    s = _stub_state()
    calls = {"n": 0}

    def fake_collect(*a, **k):
        calls["n"] += 1
        return []

    orig = _wf.collect_workflows
    _wf.collect_workflows = fake_collect
    try:
        s._collect_workflows()  # builds, stamps _workflows_last
        s._collect_workflows()  # immediate second call must no-op (throttled)
    finally:
        _wf.collect_workflows = orig
    assert calls["n"] == 1


def test_collect_workflows_rebuilds_after_window():
    from aion.store import _WORKFLOWS_THROTTLE_S
    import aion.workflows as _wf
    s = _stub_state()
    calls = {"n": 0}

    def fake_collect(*a, **k):
        calls["n"] += 1
        return []

    orig = _wf.collect_workflows
    _wf.collect_workflows = fake_collect
    try:
        s._collect_workflows()
        # advance past the throttle window
        s._workflows_last -= (_WORKFLOWS_THROTTLE_S + 0.01)
        s._collect_workflows()
    finally:
        _wf.collect_workflows = orig
    assert calls["n"] == 2
