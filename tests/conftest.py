"""Shared test isolation.

The suite used to run against the real ~/.aion: tests spawned tasks, the
SessionStore persisted them, and a later test loaded those tasks back as
INTERRUPTED and failed on them. The failure depended on whatever happened to
be on disk, so it moved around whenever the state layout changed.

Every test now gets its own HOME and its own fleet root.
"""
from __future__ import annotations

import pytest

from aion import fleet, profile

# Keep a handle to the real onboarding gate so tests that exercise it can
# restore it (conftest stubs it off by default below).
from aion.ui import wizard as _wizard
_REAL_SHOULD_SHOW = _wizard.should_show_onboarding


@pytest.fixture(autouse=True)
def isolate_aion_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # resolved at call time, so patching the module attribute is enough
    monkeypatch.setattr(fleet, "AION_HOME", home / ".aion")
    # captured at import time, so it needs patching directly
    monkeypatch.setattr(profile, "PROFILE_PATH", home / ".aion" / "profile.json")
    monkeypatch.delenv("AION_INSTANCE", raising=False)
    # never bind a real port or reach the network from a test
    monkeypatch.delenv("AION_LISTEN", raising=False)
    # fleet settings are module-global; reset so one test cannot configure
    # thresholds for the next
    fleet.configure({})
    # Stub the onboarding gate OFF so the tour never auto-launches and swallows
    # keystrokes in tests that boot the app for unrelated work. No marker file
    # is written (a file would leak into tests that walk tmp_path). Tests that
    # exercise the gate restore `_REAL_SHOULD_SHOW` themselves.
    monkeypatch.setattr(_wizard, "should_show_onboarding", lambda *a, **k: False)
    yield
    fleet.configure({})
