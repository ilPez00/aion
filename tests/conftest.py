"""Shared test isolation.

The suite used to run against the real ~/.aion: tests spawned tasks, the
SessionStore persisted them, and a later test loaded those tasks back as
INTERRUPTED and failed on them. The failure depended on whatever happened to
be on disk, so it moved around whenever the state layout changed.

Every test now gets its own HOME and its own fleet root.
"""
from __future__ import annotations

import pytest

from aion import agg, fleet, meshmon, meshsrv, profile

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
    # meshsrv.probe_service/snapshot default transport=None -> _ssh_transport,
    # which shells out to real `ssh` with a 30s timeout. Any test reaching the
    # mesh code without injecting a fake (ui.app does exactly that at
    # app.py:1344 and :1373) sat there probing the author's actual fleet, one
    # host at a time. 255 is what an unreachable host returns, and the mesh
    # code is documented to treat that as running=False rather than raise.
    #
    # Three modules each carry their own default SSH transport, and every one
    # of them is reachable from the UI without a fake being injected:
    #   meshsrv._ssh_transport     via ui/app.py:1344 and :1373
    #   meshmon._default_transport via ui/app.py:_render_center -> store ->
    #                              dashboard.collect_dashboard -> _mesh_snapshot
    #   agg._ssh_transport         via the collector
    # meshmon's docstring claims "imported lazily so tests never shell out",
    # but a lazy import does not stop the subprocess — only this does.
    monkeypatch.setattr(
        meshsrv, "_ssh_transport",
        lambda method, target, cmd: (255, "ssh disabled in tests"))
    _dead_ssh = lambda method, target, cmd: (255, "ssh disabled in tests")
    monkeypatch.setattr(meshmon, "_default_transport", _dead_ssh)
    # meshsrv and agg look their transport up at call time, so patching the
    # module attribute is enough. meshmon does NOT: probe_node and snapshot
    # bind `transport=_default_transport` as a default argument, evaluated at
    # import. The module attribute can be replaced all day and the already-bound
    # default still points at the real one — patch __defaults__ as well.
    monkeypatch.setattr(meshmon.probe_node, "__defaults__", (_dead_ssh,))
    monkeypatch.setattr(meshmon.snapshot, "__defaults__", (_dead_ssh,))
    monkeypatch.setattr(
        agg, "_ssh_transport",
        lambda target, source: (False, "", "ssh disabled in tests"))
    # fleet settings are module-global; reset so one test cannot configure
    # thresholds for the next
    fleet.configure({})
    # Probe budget: the collectors talk real ssh to real hosts, and a box that
    # is powered off costs its full connect timeout on every probe. Boot tests
    # then die in Textual's pilot waiting for a screen stuck in on_mount — a
    # red suite that reports the state of the house, not the state of the code.
    monkeypatch.setenv("AION_PROBE_CONNECT_TIMEOUT", "1")
    monkeypatch.setenv("AION_PROBE_TIMEOUT", "2")
    # Stub the onboarding gate OFF so the tour never auto-launches and swallows
    # keystrokes in tests that boot the app for unrelated work. No marker file
    # is written (a file would leak into tests that walk tmp_path). Tests that
    # exercise the gate restore `_REAL_SHOULD_SHOW` themselves.
    monkeypatch.setattr(_wizard, "should_show_onboarding", lambda *a, **k: False)
    yield
    fleet.configure({})
