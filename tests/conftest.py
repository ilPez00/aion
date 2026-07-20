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
    yield
