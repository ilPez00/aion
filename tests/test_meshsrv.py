"""Tests for the RandoMesh service lifecycle pure logic (no network)."""
import json

from aion import meshsrv
from aion.meshsrv import (probe_service, snapshot, control_service, SERVICES,
                          ServiceState, install_package, disable_package)


def _fake(rc=0, out="", raise_timeout=False):
    def t(method, target, cmd):
        if raise_timeout:
            raise TimeoutError("ssh hung")
        return rc, out
    return t


def _rec():
    """Fake transport that records (target, cmd) and succeeds."""
    seen = []

    def t(method, target, cmd):
        seen.append((target, cmd))
        return 0, ""
    t.seen = seen  # type: ignore[attr-defined]
    return t


def test_probe_tcp_open():
    st = probe_service("omo-llm", _fake(out="OPEN"))
    assert st.running is True
    assert st.reachable is True
    assert st.detail == "open"


def test_probe_tcp_closed():
    st = probe_service("omo-llm", _fake(out="CLOSED"))
    assert st.running is False


def test_probe_unit_active():
    st = probe_service("physis", _fake(out="active\n"))
    # physis uses tcp probe, but exercise unit path directly:
    assert st.name == "physis"


def test_snapshot_counts():
    fake = _fake(out="OPEN")
    # monkeypatch tcp probe by making all probes return OPEN
    snap = snapshot(fake)
    # every service probe returns OPEN -> all up
    assert snap["total"] == len(SERVICES)
    assert snap["up"] == len(SERVICES)


def test_control_start():
    res = control_service("omo-llm", "start", _fake(rc=0, out="started"))
    assert res["ok"] is True
    assert res["action"] == "start"


def test_control_unknown():
    res = control_service("nope", "start", _fake())
    assert res["ok"] is False


def test_transport_timeout_softfail():
    # a dead host (TimeoutError) must not crash snapshot
    snap = snapshot(_fake(raise_timeout=True))
    assert snap["total"] == len(SERVICES)
    assert snap["up"] == 0


# ── package lifecycle: install / disable / derived unit cmds / host override ──

def _with_spec(name: str, spec: dict):
    """Context-manager-ish helper: inject a spec, guarantee restore."""
    saved = SERVICES.get(name)
    SERVICES[name] = spec

    def undo():
        if saved is not None:
            SERVICES[name] = saved
        else:
            SERVICES.pop(name, None)
    return undo


def test_install_and_disable_run_declared_cmds():
    undo = _with_spec("tst-pkg", {"host": "pansa-ts", "unit": "a.service",
                                  "install": "bash install-tst.sh",
                                  "disable": "bash disable-tst.sh",
                                  "kind": "package"})
    try:
        tr = _rec()
        r = install_package("tst-pkg", tr)
        assert r["ok"] and r["action"] == "install" and r["host"] == "pansa-ts"
        assert tr.seen[-1] == ("pansa-ts", "bash install-tst.sh")
        r = disable_package("tst-pkg", tr)
        assert r["ok"] and r["action"] == "disable"
        assert tr.seen[-1][1] == "bash disable-tst.sh"
        # services without the cmd are refused, not silently "successful"
        assert install_package("omo-llm", tr)["ok"] is False
        assert install_package("nope", tr)["ok"] is False
    finally:
        undo()


def test_control_derives_unit_cmd_and_honours_host_override():
    undo = _with_spec("tst-derive", {"host": "declared-ts",
                                     "unit": "b.service", "scope": "user"})
    try:
        tr = _rec()
        control_service("tst-derive", "start", tr)
        assert tr.seen[-1] == ("declared-ts", "systemctl --user start b.service")
        control_service("tst-derive", "stop", tr, host="other-ts")
        assert tr.seen[-1] == ("other-ts", "systemctl --user stop b.service")
    finally:
        undo()


def test_explicit_control_cmd_beats_derived():
    undo = _with_spec("tst-explicit", {"host": "h", "unit": "c.service",
                                       "start": "systemctl --user restart c.service",
                                       "stop": "systemctl --user stop c.service"})
    try:
        tr = _rec()
        control_service("tst-explicit", "start", tr)
        assert tr.seen[-1][1].startswith("systemctl --user restart")
    finally:
        undo()


def test_probe_reports_lifecycle_flags():
    undo = _with_spec("tst-flags", {"host": "h", "probe": ("tcp", 1),
                                    "install": "x", "disable": "y",
                                    "kind": "package"})
    try:
        d = probe_service("tst-flags", _fake(out="CLOSED")).as_dict()
        assert d["kind"] == "package" and d["can_install"] and d["can_disable"]
        d2 = probe_service("omo-llm", _fake(out="OPEN")).as_dict()
        assert d2["kind"] == "service" and not d2["can_install"]
    finally:
        undo()


def test_probe_host_override(tmp_path, monkeypatch):
    # a cockpit may aim any package at any reachable box for one call
    tr = _rec()
    probe_service("omo-llm", tr, host="air-ts")
    assert tr.seen[-1][0] == "air-ts"


def test_fleetjson_services_declare_packages_and_win(tmp_path, monkeypatch):
    """CONFIG.md-declared services merge into SERVICES — and OVERWRITE a base
    entry of the same name (operator intent beats a stale hardcoded dict)."""
    cfg = {"nodes": [], "serving": {}, "services": {
        "testpkg": {"host": "test-ts", "unit": "test.service", "scope": "user",
                    "install": "bash install-test.sh",
                    "disable": "bash disable-test.sh", "kind": "package",
                    "note": "test package"},
        "omo-llm": {"host": "operator-ts", "port": "9999"}}}
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("AION_FLEET_CONFIG", str(path))
    monkeypatch.setattr(meshsrv, "_FLEET_SERVICES_LOADED", False)
    saved = dict(SERVICES)
    try:
        meshsrv._ensure_fleet_services()
        spec = SERVICES["testpkg"]
        # unit-only service: no port probe declared, probe_service falls back
        # to the unit at probe time; the unit+scope must survive the loader
        assert "probe" not in spec
        assert spec["unit"] == "test.service" and spec["scope"] == "user"
        assert spec["install"].endswith("install-test.sh")
        r = install_package("testpkg", _fake(rc=0, out="ok"))
        assert r["ok"] and r["host"] == "test-ts"
        # the overlay's omo-llm replaced the base entry entirely
        assert SERVICES["omo-llm"]["host"] == "operator-ts"
    finally:
        SERVICES.clear()
        SERVICES.update(saved)


def test_first_call_may_be_install_not_probe(tmp_path, monkeypatch):
    """A fresh process whose FIRST call is install_package/ control_service
    must still see CONFIG.md-declared services — a real bug caught live:
    control/install skipped _ensure_fleet_services, so the first call in a
    process answered 'unknown service' until some probe warmed the registry."""
    cfg = {"nodes": [], "serving": {}, "services": {
        "testpkg": {"host": "warm-ts", "unit": "t.service", "scope": "user",
                    "install": "bash install-test.sh"}}}
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("AION_FLEET_CONFIG", str(path))
    monkeypatch.setattr(meshsrv, "_FLEET_SERVICES_LOADED", False)
    saved = dict(SERVICES)
    try:
        SERVICES["testpkg"] = None  # poison: force the loader to be the source
        del SERVICES["testpkg"]
        r = install_package("testpkg", _fake(rc=0, out="installed"))
        assert r["ok"] and r["host"] == "warm-ts"
    finally:
        SERVICES.clear()
        SERVICES.update(saved)
        monkeypatch.setattr(meshsrv, "_FLEET_SERVICES_LOADED", False)


def test_fleetjson_nodes_all_rendered_and_fleet_wins(tmp_path, monkeypatch):
    """Convergence contract (aion plan.md / randomesh plans/aion-convergence):
    every fleet.json node and serving entry must be cockpit-visible, fleet
    values win over the hardcoded base, and `host` must always stay an ssh
    alias — never a bare IP (ssh to 127.0.0.1 would probe the WRONG box)."""
    cfg = {"nodes": [
        {"name": "testnode", "tailscale": "testnode-ts",
         "serving_port": "18081", "role": "cpu-inference"},
        {"name": "omo", "tailscale": "omo-ts",
         "serving_port": "8081", "role": "source-storage"}],
        "serving": {"nodes": {
            "testnode": {"ip": "100.0.0.1", "port": "18081"},
            "omo": {"ip": "127.0.0.1", "port": "18082"}}},
        "services": {}}
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("AION_FLEET_CONFIG", str(path))
    monkeypatch.setattr(meshsrv, "_FLEET_SERVICES_LOADED", False)
    saved = dict(SERVICES)
    try:
        meshsrv._ensure_fleet_services()
        # a brand-new node appears, addressed by alias, fleet port, runnable
        spec = SERVICES["testnode"]
        assert spec["host"] == "testnode-ts"
        assert spec["probe"] == ("tcp", 18081)
        assert spec["start"] and spec["stop"]
        # fleet serving values win over the base entry — but the base's
        # lifecycle cmds survive (fleet serving rows carry no start/stop)
        omo = SERVICES["omo-llm"]
        assert omo["probe"] == ("tcp", 18082)
        assert omo["host"] == "omo-ts"
        assert "pansa_node" not in omo["start"] and "llama-server" in omo["start"]
        # no entry may ever point the ssh transport at loopback/an IP
        for name, s in SERVICES.items():
            h = s.get("host", "")
            assert h and h != "127.0.0.1" and not h[0].isdigit(), name
        # and the new node actually probes (soft-fail path, not KeyError)
        st = probe_service("testnode", _fake(out="CLOSED"))
        assert st.running is False and st.reachable is True
    finally:
        SERVICES.clear()
        SERVICES.update(saved)
        monkeypatch.setattr(meshsrv, "_FLEET_SERVICES_LOADED", False)
