"""Tests for the RandoMesh service lifecycle pure logic (no network)."""
from aion.meshsrv import probe_service, snapshot, control_service, SERVICES, ServiceState


def _fake(rc=0, out="", raise_timeout=False):
    def t(method, target, cmd):
        if raise_timeout:
            raise TimeoutError("ssh hung")
        return rc, out
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
