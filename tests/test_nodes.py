"""test_nodes.py — unit tests for the machine seam (src/aion/nodes.py).

No network, no ssh: remote behaviour is asserted on the *argv aion builds*
plus fakes for the transport. The local node is exercised for real, since it
is a plain subprocess.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.nodes import (  # noqa: E402
    LOCAL, Node, NodeRegistry, NodeResult, load_nodes,
    TRANSPORT_LOCAL, TRANSPORT_SSH,
)


def _remote(**kw) -> Node:
    base = dict(name="forge", host="forge.ts.net", transport=TRANSPORT_SSH,
                user="gio", home="/home/gio")
    base.update(kw)
    return Node(**base)


# --------------------------------------------------------------------------
# local node: the degenerate case must stay a plain subprocess
# --------------------------------------------------------------------------
def test_local_runs_for_real():
    res = LOCAL.run(["echo", "hello"])
    assert res.ok
    assert res.stdout.strip() == "hello"
    assert res.node == "local"


def test_local_nonzero_exit_is_not_unreachable():
    res = LOCAL.run(["sh", "-c", "exit 3"])
    assert not res.ok
    assert res.returncode == 3
    assert not res.unreachable  # the command ran; it just failed


def test_local_missing_binary_does_not_raise():
    res = LOCAL.run(["aion-definitely-not-a-real-binary"])
    assert not res.ok
    assert not res.unreachable


def test_local_timeout_reports_124():
    res = LOCAL.run(["sleep", "5"], timeout=1)
    assert res.returncode == 124
    assert not res.unreachable  # local box is obviously up


def test_local_fetch_is_identity(tmp_path):
    f = tmp_path / "state.db"
    f.write_text("x")
    assert LOCAL.fetch(f) == f
    assert LOCAL.fetch(tmp_path / "missing.db") is None


def test_local_exists(tmp_path):
    f = tmp_path / "a"
    f.write_text("")
    assert LOCAL.exists(f)
    assert not LOCAL.exists(tmp_path / "b")


def test_local_is_always_reachable():
    assert LOCAL.reachable()


# --------------------------------------------------------------------------
# path resolution: ~ must expand against the *remote* home
# --------------------------------------------------------------------------
def test_remote_tilde_uses_configured_home():
    assert _remote().path("~/.hermes/state.db") == "/home/gio/.hermes/state.db"


def test_remote_tilde_falls_back_to_user():
    n = _remote(home=None)
    assert n.path("~/.hermes/state.db") == "/home/gio/.hermes/state.db"


def test_remote_absolute_path_untouched():
    assert _remote().path("/var/log/x") == "/var/log/x"


def test_remote_home_can_be_non_standard():
    # termux on the rooted phone
    n = _remote(home="/data/data/com.termux/files/home")
    assert n.path("~/.hermes") == "/data/data/com.termux/files/home/.hermes"


def test_local_tilde_uses_expanduser():
    assert LOCAL.path("~/x") == str(Path.home() / "x")


# --------------------------------------------------------------------------
# ssh argv construction
# --------------------------------------------------------------------------
def test_ssh_argv_multiplexes_and_quotes():
    argv = _remote()._ssh_argv(["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_tty}"])
    assert argv[0] == "ssh"
    assert "ControlMaster=auto" in argv
    assert "ControlPersist=10m" in argv
    assert "BatchMode=yes" in argv          # never hang the HUD on a prompt
    assert argv[-2] == "gio@forge.ts.net"
    # the format string has a tab and braces: it must arrive as ONE argument
    assert argv[-1].startswith("tmux list-panes -a -F ")
    assert argv[-1].count("tmux") == 1


def test_ssh_argv_quotes_paths_with_spaces():
    argv = _remote()._ssh_argv(["cat", "/home/gio/my project/f.txt"])
    assert "'/home/gio/my project/f.txt'" in argv[-1]


def test_ssh_argv_honours_identity_and_port():
    argv = _remote(identity="~/.ssh/id_ed25519", port=8022)._ssh_argv(["true"])
    assert "-p" in argv and "8022" in argv
    assert "-i" in argv
    assert "~" not in argv[argv.index("-i") + 1]  # expanded locally


def test_ssh_dest_without_user():
    argv = _remote(user=None)._ssh_argv(["true"])
    assert argv[-2] == "forge.ts.net"


def test_interactive_argv_forces_tty():
    argv = _remote().ssh_interactive_argv(["tmux", "attach", "-t", "%3"])
    assert argv[:2] == ["ssh", "-tt"]      # full-screen TUIs need a pty
    assert argv[-1] == "tmux attach -t %3"


def test_interactive_argv_local_is_bare():
    assert LOCAL.ssh_interactive_argv(["btop"]) == ["btop"]


# --------------------------------------------------------------------------
# transport failure is distinct from command failure
# --------------------------------------------------------------------------
def test_ssh_255_marks_unreachable(monkeypatch):
    class FakeProc:
        returncode, stdout, stderr = 255, "", "ssh: connect: No route to host"

    monkeypatch.setattr("aion.nodes.subprocess.run", lambda *a, **k: FakeProc())
    res = _remote().run(["tmux", "list-panes"])
    assert res.unreachable
    assert not res.ok


def test_ssh_remote_nonzero_is_reachable(monkeypatch):
    class FakeProc:
        returncode, stdout, stderr = 1, "", "no server running"

    monkeypatch.setattr("aion.nodes.subprocess.run", lambda *a, **k: FakeProc())
    res = _remote().run(["tmux", "list-panes"])
    assert not res.ok
    assert not res.unreachable  # host is up, tmux just isn't


def test_ssh_timeout_marks_unreachable(monkeypatch):
    import subprocess as sp

    def boom(*a, **k):
        raise sp.TimeoutExpired(cmd="ssh", timeout=5)

    monkeypatch.setattr("aion.nodes.subprocess.run", boom)
    assert _remote().run(["true"]).unreachable


# --------------------------------------------------------------------------
# fetch: copy-then-read, cache, and stale-on-failure
# --------------------------------------------------------------------------
def test_fetch_copies_then_caches(monkeypatch, tmp_path):
    monkeypatch.setattr("aion.nodes.cache_dir", lambda: tmp_path)
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        Path(argv[-1]).write_text("sqlite bytes")   # scp writes the .part file

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()

    monkeypatch.setattr("aion.nodes.subprocess.run", fake_run)

    got = _remote().fetch("~/.hermes/state.db")
    assert got is not None and got.read_text() == "sqlite bytes"
    assert calls[0][0] == "scp"
    assert calls[0][-2].endswith(":/home/gio/.hermes/state.db")

    # within TTL: served from cache, no second scp
    _remote().fetch("~/.hermes/state.db")
    assert len(calls) == 1

    # ttl_s=0 forces a re-fetch
    _remote().fetch("~/.hermes/state.db", ttl_s=0)
    assert len(calls) == 2


def test_fetch_serves_stale_copy_when_node_drops(monkeypatch, tmp_path):
    monkeypatch.setattr("aion.nodes.cache_dir", lambda: tmp_path)

    def ok_run(argv, **kw):
        Path(argv[-1]).write_text("good")

        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()

    monkeypatch.setattr("aion.nodes.subprocess.run", ok_run)
    first = _remote().fetch("~/.hermes/state.db")
    assert first is not None

    def fail_run(argv, **kw):
        class P:
            returncode, stdout, stderr = 1, "", "connection refused"
        return P()

    monkeypatch.setattr("aion.nodes.subprocess.run", fail_run)
    # a node that just went down shows its last known state, not nothing
    stale = _remote().fetch("~/.hermes/state.db", ttl_s=0)
    assert stale is not None and stale.read_text() == "good"


def test_fetch_returns_none_when_never_seen(monkeypatch, tmp_path):
    monkeypatch.setattr("aion.nodes.cache_dir", lambda: tmp_path)

    def fail_run(argv, **kw):
        class P:
            returncode, stdout, stderr = 1, "", "No such file"
        return P()

    monkeypatch.setattr("aion.nodes.subprocess.run", fail_run)
    assert _remote().fetch("~/.hermes/state.db") is None


def test_fetch_leaves_no_partial_file(monkeypatch, tmp_path):
    monkeypatch.setattr("aion.nodes.cache_dir", lambda: tmp_path)

    def half_run(argv, **kw):
        Path(argv[-1]).write_text("truncated")   # scp died mid-copy

        class P:
            returncode, stdout, stderr = 1, "", "lost connection"
        return P()

    monkeypatch.setattr("aion.nodes.subprocess.run", half_run)
    assert _remote().fetch("~/.hermes/state.db") is None
    assert not list(tmp_path.rglob("*.part"))    # never left behind


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def test_registry_always_has_local_first():
    reg = NodeRegistry([_remote()])
    assert [n.name for n in reg.all()] == ["local", "forge"]


def test_registry_unknown_name_falls_back_to_local():
    reg = NodeRegistry([_remote()])
    assert reg.get("ghost").name == "local"
    assert reg.get(None).name == "local"
    assert reg.get("forge").name == "forge"


def test_registry_hides_disabled():
    reg = NodeRegistry([_remote(), _remote(name="deck", enabled=False)])
    assert [n.name for n in reg.all()] == ["local", "forge"]
    assert [n.name for n in reg.all(include_disabled=True)] == ["local", "forge", "deck"]


def test_registry_remote_excludes_local():
    reg = NodeRegistry([_remote()])
    assert [n.name for n in reg.remote()] == ["forge"]


def test_load_nodes_missing_file_is_local_only(tmp_path):
    reg = load_nodes(tmp_path / "nope.json")
    assert [n.name for n in reg.all()] == ["local"]


def test_load_nodes_malformed_file_is_local_only(tmp_path):
    p = tmp_path / "nodes.json"
    p.write_text("{ this is not json")
    assert [n.name for n in load_nodes(p).all()] == ["local"]


def test_load_nodes_skips_bad_entries(tmp_path):
    p = tmp_path / "nodes.json"
    p.write_text(json.dumps({"nodes": [
        {"host": "no-name.ts.net"},                       # missing "name"
        {"name": "forge", "host": "f.ts.net", "transport": "ssh"},
    ]}))
    assert [n.name for n in load_nodes(p).all()] == ["local", "forge"]


def test_load_nodes_ignores_local_override(tmp_path):
    p = tmp_path / "nodes.json"
    p.write_text(json.dumps({"nodes": [
        {"name": "local", "host": "evil.ts.net", "transport": "ssh"},
    ]}))
    reg = load_nodes(p)
    assert reg.get("local").transport == TRANSPORT_LOCAL   # built in, not overridable


def test_load_nodes_reads_real_example_config():
    p = Path(__file__).resolve().parents[1] / "config" / "nodes.example.json"
    names = [n.name for n in load_nodes(p).all(include_disabled=True)]
    assert names == ["local", "forge", "relay", "deck"]


def test_registry_status_probes_every_node(monkeypatch):
    monkeypatch.setattr(Node, "reachable", lambda self: self.is_local)
    rows = NodeRegistry([_remote()]).status()
    assert [r["name"] for r in rows] == ["local", "forge"]
    assert rows[0]["reachable"] is True
    assert rows[1]["reachable"] is False
    assert all("latency_ms" in r for r in rows)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
