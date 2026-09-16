"""Tests for sentinelx.py — SentinelX agent monitor + lifecycle (no network).

Gate: .venv/bin/python -m pytest tests/test_sentinelx.py -q
"""
import pytest

from aion import sentinelx
from aion.sentinelx import (SentinelHost, control, enroll_url, parse_probe,
                            probe_host, snapshot)


def _fake(rc=0, out="", raises=None):
    def t(method, target, cmd):
        if raises is not None:
            raise raises
        return rc, out
    return t


def _rec(rc=0, out=""):
    """Recording transport: keeps (target, cmd) so the built command is testable."""
    seen = []

    def t(method, target, cmd):
        seen.append((method, target, cmd))
        return rc, out
    t.seen = seen  # type: ignore[attr-defined]
    return t


# A healthy, enrolled, sudo-granted agent — the exact shape the probe prints.
LIVE_BLOCK = "\n".join([
    "unit=active",
    "enabled=enabled",
    "since=Tue 2026-09-16 05:06:23 CEST",
    "restarts=0",
    "installed=1",
    "host_id=host_e4f4b084b37844b3",
    "identity=1",
    "sudoers=1",
    "cmds=86",
    "hub=401",
    "up=1140",
    "conn=connected; session=sess_aed3618b2322",
    "sudo=0",
])


def test_parse_probe_live_host():
    h = parse_probe(LIVE_BLOCK, "pansa", "pansa-ts")
    assert h.name == "pansa" and h.host == "pansa-ts"
    assert h.reachable is True
    assert h.installed is True and h.enrolled is True and h.sudoers is True
    assert h.unit == "active"
    assert h.host_id == "host_e4f4b084b37844b3"
    assert h.cmd_count == 86
    assert h.conn_session == "sess_aed3618b2322"
    assert h.up_s == 1140
    assert h.hub_ok is True          # 401 = hub reachable, just unauthenticated
    assert h.state == "live"


def test_parse_probe_unenrolled_host_needs_enrollment():
    block = "\n".join([
        "unit=inactive", "enabled=disabled", "installed=1",
        "host_id=host_756c6e2cd2a843d2", "identity=0", "sudoers=0",
        "cmds=0", "hub=401", "conn=", "sudo=0",
    ])
    h = parse_probe(block, "air", "air-ts")
    assert h.state == "unenrolled"
    assert h.cmd_count == 0
    assert h.conn_session == ""
    assert h.enroll_url.endswith("host_id=host_756c6e2cd2a843d2")


def test_parse_probe_absent_and_stopped():
    absent = parse_probe("installed=0\nunit=inactive\nhost_id=", "pi", "pi-ts")
    assert absent.state == "absent"
    stopped = parse_probe("installed=1\nidentity=1\nunit=inactive\n"
                          "host_id=host_x", "pi", "pi-ts")
    assert stopped.state == "stopped"


def test_parse_probe_tolerates_garbage():
    """A half-printed probe must never raise — the HUD has to keep painting."""
    h = parse_probe("<<<ssh banner noise>>>\nunit=active\ncmds=not-a-number",
                    "omo", "omo-ts")
    assert h.unit == "active"
    assert h.cmd_count == 0          # unparsable count degrades, does not crash
    assert h.state in ("absent", "unenrolled", "stopped", "live")


def test_probe_host_unreachable_soft_fails():
    h = probe_host("air", "air-ts", _fake(rc=255, out="ssh: timed out"))
    assert h.reachable is False and h.state == "down"


def test_probe_host_transport_exception_soft_fails():
    h = probe_host("pi", "pi-ts", _fake(raises=TimeoutError("ssh hung")))
    assert h.reachable is False
    assert h.state == "down"
    assert "TimeoutError" in h.note


def test_probe_host_sends_one_ssh_per_host():
    t = _rec(out=LIVE_BLOCK)
    probe_host("pansa", "pansa-ts", t)
    assert len(t.seen) == 1                      # one round trip, not five
    method, target, cmd = t.seen[0]
    assert (method, target) == ("ssh", "pansa-ts")
    assert "U=sentinelx-cloud-core" in cmd
    assert "systemctl is-active $U" in cmd
    assert "/etc/sentinelx/identity.json" in cmd
    # the HUD must never read the enrollment token, only its existence
    assert "cat /etc/sentinelx/identity.json" not in cmd


def test_snapshot_counts_states_and_soft_fails_per_host():
    def t(method, target, cmd):
        return (0, LIVE_BLOCK) if target == "pansa-ts" else (255, "unreachable")
    snap = snapshot(t, hosts={"pansa": "pansa-ts", "air": "air-ts"})
    assert snap["total"] == 2
    assert snap["live"] == 1
    assert snap["connector"].endswith("/mcp/mcp")
    states = {h["name"]: h["state"] for h in snap["hosts"]}
    assert states == {"pansa": "live", "air": "down"}
    assert snap["hosts"][0]["name"] == "pansa"    # live sorts above down


def test_default_host_is_pansa_and_env_overrides_it(monkeypatch):
    """The hub's free plan covers one machine: pansa is the one a bare verb means."""
    monkeypatch.delenv("AION_SENTINELX_HOST", raising=False)
    assert sentinelx.DEFAULT_HOST == "pansa"
    assert sentinelx.default_host() == "pansa"
    monkeypatch.setenv("AION_SENTINELX_HOST", "omo")
    assert sentinelx.default_host() == "omo"
    monkeypatch.setenv("AION_SENTINELX_HOST", "  ")   # blank is not an override
    assert sentinelx.default_host() == "pansa"


def test_control_without_a_host_targets_the_default(monkeypatch):
    monkeypatch.delenv("AION_SENTINELX_HOST", raising=False)
    t = _rec()
    res = control("", "restart", t, hosts={"pansa": "pansa-ts", "omo": "omo-ts"})
    assert res["ok"] is True and res["name"] == "pansa"
    assert t.seen[0][1] == "pansa-ts"


def test_snapshot_marks_and_sorts_the_default_host(monkeypatch):
    monkeypatch.delenv("AION_SENTINELX_HOST", raising=False)
    snap = snapshot(_fake(out=LIVE_BLOCK),
                    hosts={"omo": "omo-ts", "pansa": "pansa-ts"})
    assert snap["default"] == "pansa"
    assert snap["hosts"][0]["name"] == "pansa"        # default sorts first
    assert snap["hosts"][0]["is_default"] is True
    assert snap["hosts"][1]["is_default"] is False


def test_snapshot_probes_hosts_concurrently():
    """Wall-clock must be one node's latency, not the sum of them.

    With air and pi down on this fleet, sequential probing costs 2 x ssh
    timeout on every refresh cycle — the difference between a live panel and
    a panel that updates once a minute.
    """
    import time

    def slow_transport(method, target, cmd):
        time.sleep(0.2)
        return 0, LIVE_BLOCK

    hosts = {n: f"{n}-ts" for n in ("pansa", "omo", "feather", "air", "pi")}
    t0 = time.monotonic()
    snap = snapshot(slow_transport, hosts=hosts)
    elapsed = time.monotonic() - t0
    assert snap["total"] == 5 and len(snap["hosts"]) == 5
    assert snap["live"] == 5                      # order preserved, data intact
    assert elapsed < 0.6, f"probes look sequential ({elapsed:.2f}s)"


def test_snapshot_never_raises_without_hosts(monkeypatch):
    monkeypatch.setattr(sentinelx, "_load_hosts", lambda: {})
    snap = snapshot(_fake(rc=255), hosts={})
    assert snap == {"hosts": [], "total": 0, "live": 0, "unenrolled": 0,
                    "hub": sentinelx.HUB, "connector": sentinelx.CONNECTOR,
                    "unit": sentinelx.UNIT, "default": sentinelx.default_host()}


def test_control_builds_scoped_systemctl_command():
    t = _rec(out="")
    res = control("pansa", "restart", t, hosts={"pansa": "pansa-ts"})
    assert res["ok"] is True
    method, target, cmd = t.seen[0]
    assert target == "pansa-ts"
    assert cmd == "sudo -n systemctl restart sentinelx-cloud-core"
    assert res["action"] == "restart"


def test_control_rejects_unknown_action_before_touching_ssh():
    t = _rec()
    res = control("pansa", "install", t, hosts={"pansa": "pansa-ts"})
    assert res["ok"] is False
    assert "unknown action" in res["error"]
    assert t.seen == []          # nothing ran


def test_control_reports_missing_grant_with_the_fix():
    t = _fake(rc=1, out="sudo: a password is required")
    res = control("pansa", "stop", t, hosts={"pansa": "pansa-ts"})
    assert res["ok"] is False
    assert "needs root" in res["error"]
    assert "NOPASSWD" in res["hint"] and "sentinelx-cloud-core" in res["hint"]


def test_grant_hint_is_valid_sudoers_syntax():
    """Specs must be comma-separated — space-joined parses but matches nothing."""
    hint = sentinelx.grant_hint(alias="pansa-ts")
    rule = hint.split("echo '", 1)[1].split("' |", 1)[0]
    assert rule == (
        "gio ALL=(root) NOPASSWD: "
        "/usr/bin/systemctl start sentinelx-cloud-core, "
        "/usr/bin/systemctl stop sentinelx-cloud-core, "
        "/usr/bin/systemctl restart sentinelx-cloud-core, "
        "/usr/bin/systemctl is-active sentinelx-cloud-core")
    assert "visudo -c" in hint


def test_control_unknown_host_is_a_refusal_not_a_crash():
    res = control("nope", "start", _rec(), hosts={"pansa": "pansa-ts"})
    assert res["ok"] is False and "unknown host" in res["error"]


def test_span_formats_hud_durations():
    assert sentinelx.span(0) == "0s"
    assert sentinelx.span(45) == "45s"
    assert sentinelx.span(1140) == "19m"
    assert sentinelx.span(7200) == "2h"
    assert sentinelx.span(90 * 3600) == "3d"
    assert sentinelx.span(None) == "0s"          # 0/None must not raise


def test_enroll_url_points_at_the_dashboard_with_the_host_id():
    url = enroll_url("host_abc123")
    assert url == (f"{sentinelx.HUB}/auth/dashboard/enroll?host_id=host_abc123")
    assert enroll_url("") == ""      # nothing to enroll yet


def test_hosts_default_to_the_real_fleet_config():
    hosts = sentinelx._load_hosts()
    assert hosts, "fleet.json (or the built-in fallback) must yield nodes"
    assert all(isinstance(v, str) and v for v in hosts.values())


def _plain(markup: str) -> str:
    """Strip Rich markup — assertions belong to what the operator reads."""
    import re
    return re.sub(r"\[/?[^\]]*\]", "", markup)


def test_panel_section_renders_states_and_conn_session():
    from aion.ui.mesh_panel import render_mesh

    theme = {k: f"#{k}" for k in ("ok", "warn", "err", "faint", "fg", "dim",
                                  "accent")}
    data = {"mesh": {"nodes": [], "total": 0, "reachable": 0}, "sentinelx": {
        "total": 2, "live": 1, "connector": sentinelx.CONNECTOR,
        "unit": sentinelx.UNIT, "default": "pansa",
        "hosts": [
            {**SentinelHost(name="pansa", host="pansa-ts", reachable=True,
                            installed=True, enrolled=True, unit="active",
                            host_id="host_e4f4b084b37844b3", cmd_count=86,
                            conn_session="sess_aed3618b2322",
                            sudoers=True, is_default=True).as_dict()},
            {**SentinelHost(name="air", host="air-ts", reachable=True,
                            installed=True, enrolled=False,
                            host_id="host_756c6e2cd2a843d2").as_dict()},
        ]}}
    out = render_mesh(data, theme)
    text = _plain(out)
    assert "SentinelX" in text and "1/2 live" in text
    assert "pansa" in text and "sess_aed361" in text
    assert "86 cmds" in text and "sudo" in text
    assert "air" in text and "unenrolled" in text
    assert "pansa    ★" in text                      # default host is marked
    assert "sentinelx start|stop|restart <host> · default pansa" in text


def test_panel_tolerates_missing_sentinelx_section():
    from aion.ui.mesh_panel import render_mesh
    theme = {k: f"#{k}" for k in ("ok", "warn", "err", "faint", "fg", "dim",
                                  "accent")}
    out = render_mesh({"mesh": {"nodes": [], "total": 0, "reachable": 0}}, theme)
    assert "SentinelX" not in out


# ── wiring: store dispatcher -> app callback -> module ──────────────────────

@pytest.fixture()
def store():
    from aion.core import Bus, load_config
    from aion.store import Store
    s = Store(cfg=load_config(), bus=Bus())
    s.harnesses = {"demo": object()}
    s.state.active_harness = "demo"
    s.spawned = []

    async def fake_spawn(hid, prompt):
        s.spawned.append((hid, prompt))
    s._spawn = fake_spawn
    return s


def test_sentinelx_reaches_the_app_callback(store):
    import asyncio
    calls = []

    async def fake(text):
        calls.append(text)
        return "sentinelx 3/5 live"
    store.sentinelx_callback = fake
    asyncio.run(store._run_command("sentinelx list", _interpreted=True))
    assert calls == ["sentinelx list"]
    assert any("sentinelx 3/5 live" in ln for ln in store.state.logs)


def test_sentinelx_without_callback_says_so(store):
    import asyncio
    store.sentinelx_callback = None
    asyncio.run(store._run_command("sentinelx restart pansa", _interpreted=True))
    assert any("not available" in ln for ln in store.state.logs)


def test_sentinelx_never_leaks_into_a_harness_run(store):
    """A missed verb must not fall through to the active harness."""
    import asyncio
    store.sentinelx_callback = None
    asyncio.run(store._run_command("sentinelx status omo", _interpreted=True))
    assert store.spawned == []


def test_app_sentinelx_commands_over_the_cached_snapshot(monkeypatch):
    """End-to-end through the real app handler: command -> snapshot -> log text.

    The transport is faked and the UI never boots (a boot would fan out real
    SSH probes), so this proves the wiring — cache, formatting, refusal path —
    without touching the network or the terminal.
    """
    import asyncio

    from aion.ui.app import AiOSApp

    monkeypatch.setattr(sentinelx, "_load_hosts",
                        lambda: {"pansa": "pansa-ts", "air": "air-ts"})

    def fake_transport(method, target, cmd):
        if target == "pansa-ts":
            return 0, LIVE_BLOCK
        return 255, "ssh: connect timed out"
    monkeypatch.setattr(sentinelx, "_ssh_transport", fake_transport)

    app = AiOSApp()
    app._mesh_cache = {}
    refreshed = []
    monkeypatch.setattr(app, "_refresh_mesh", lambda: refreshed.append(1))

    async def go():
        return (
            await app._handle_sentinelx_command("sentinelx list"),
            await app._handle_sentinelx_command("sentinelx status pansa"),
            await app._handle_sentinelx_command("sentinelx restart air"),
            await app._handle_sentinelx_command("sentinelx frobnicate"),
            await app._handle_sentinelx_command("sentinelx connector"),
            await app._handle_sentinelx_command("sentinelx status"),
        )

    listing, detail, denied, bogus, connector, bare = asyncio.run(go())
    assert "1/2 live" in listing and "pansa" in listing and "air" in listing
    assert "host_e4f4b084b37844b3" in detail
    assert "identity present" in detail and "86 commands" in detail
    assert "unknown verb" in bogus
    # air is unreachable, so control must refuse rather than pretend it worked
    assert "FAILED" in denied
    # connector instructions are the only thing that needs no probe at all
    assert "mcp.sentinelx.app/mcp/mcp" in connector
    # a bare `sentinelx status` reports the default machine without naming it
    assert "sentinelx status pansa:" in bare and "pansa-ts" in bare
    # acting triggers exactly one re-probe (verify, don't trust)
    assert refreshed == [1]
