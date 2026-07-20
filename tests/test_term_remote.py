"""test_term_remote.py — RemoteTermHarness argv construction.

The pty half is already covered by test_term.py; what is new here is *what
argv the pty gets*. These tests never open a socket: they assert on the
command aion would exec, which is the whole of the remote/local difference.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.core import Bus, TaskRegistry  # noqa: E402
from aion.harnesses import HarnessConfig, build_harnesses  # noqa: E402
from aion.nodes import Node, NodeRegistry  # noqa: E402
from aion.term import RemoteTermHarness, TermHarness  # noqa: E402


def _forge() -> Node:
    return Node(name="forge", host="forge.ts.net", transport="ssh",
                user="gio", home="/home/gio")


def _harness(**extra) -> RemoteTermHarness:
    cfg = HarnessConfig(id="t", type="remote-term", name="term",
                        remote="forge", command=extra.pop("command", "btop"),
                        extra=extra)
    reg = NodeRegistry([_forge()])
    return RemoteTermHarness(cfg, Bus(), TaskRegistry(Bus()), nodes=reg)


# --------------------------------------------------------------------------
# the local case must not have changed
# --------------------------------------------------------------------------
def test_local_argv_is_still_a_bare_split():
    cfg = HarnessConfig(id="t", type="term", name="term", command="btop -p 1")
    h = TermHarness(cfg, Bus(), TaskRegistry(Bus()))
    assert h.argv() == ["btop", "-p", "1"]


def test_remote_pointing_at_local_node_is_bare():
    cfg = HarnessConfig(id="t", type="remote-term", name="term",
                        remote="local", command="btop")
    h = RemoteTermHarness(cfg, Bus(), TaskRegistry(Bus()), nodes=NodeRegistry())
    assert h.argv() == ["btop"]      # no ssh hop to ourselves


def test_unknown_node_falls_back_to_local():
    cfg = HarnessConfig(id="t", type="remote-term", name="term",
                        remote="ghost", command="btop")
    h = RemoteTermHarness(cfg, Bus(), TaskRegistry(Bus()), nodes=NodeRegistry())
    assert h.node.name == "local"    # stale config must not break the pane


# --------------------------------------------------------------------------
# remote command
# --------------------------------------------------------------------------
def test_remote_command_gets_a_tty():
    argv = _harness(command="btop").argv()
    assert argv[:2] == ["ssh", "-tt"]   # pyte needs real escape sequences
    assert argv[-2] == "gio@forge.ts.net"
    assert argv[-1] == "btop"


def test_remote_command_is_one_argument():
    argv = _harness(command="hermes chat --cli").argv()
    assert argv[-1] == "hermes chat --cli"   # not split across ssh args


# --------------------------------------------------------------------------
# tmux attach — the reason any of this exists
# --------------------------------------------------------------------------
def test_pane_attach_is_read_only_by_default():
    argv = _harness(pane="%3").argv()
    assert argv[-1] == "tmux attach -t %3 -r"


def test_pane_attach_can_take_control():
    argv = _harness(pane="%3", read_only=False).argv()
    assert argv[-1] == "tmux attach -t %3"


def test_jump_hint_target_selects_window_and_pane():
    # "agents:0.0" attaches the session but lands on whatever was last active,
    # so the selects are what actually put you on the agent's pane
    remote = _harness(pane="agents:0.0").argv()[-1]
    assert remote.startswith("tmux attach -t agents:0.0 -r")
    assert "select-window -t agents:0.0" in remote
    assert "select-pane -t agents:0.0" in remote


def test_session_only_target_does_not_select_pane():
    remote = _harness(pane="agents").argv()[-1]
    assert "select-window" not in remote
    assert "select-pane" not in remote


def test_window_target_selects_window_only():
    remote = _harness(pane="agents:2").argv()[-1]
    assert "select-window -t agents:2" in remote
    assert "select-pane" not in remote


def test_command_separator_survives_quoting():
    # shlex.join quotes ";" so the remote *shell* passes it to tmux as an
    # argument; tmux then reads it as its own command separator
    remote = _harness(pane="agents:0.0").argv()[-1]
    assert "';'" in remote


def test_pane_overrides_command():
    argv = _harness(pane="%3", command="btop").argv()
    assert "btop" not in argv[-1]


def test_label_is_node_qualified():
    assert _harness(pane="%3").label == "forge:%3"
    assert _harness(command="btop").label == "forge:btop"


# --------------------------------------------------------------------------
# factory wiring
# --------------------------------------------------------------------------
def test_build_harnesses_knows_remote_term():
    out = build_harnesses(
        [{"id": "rt", "type": "remote-term", "name": "forge term",
          "remote": "local", "command": "btop"}],
        Bus(), TaskRegistry(Bus()))
    assert isinstance(out["rt"], RemoteTermHarness)


def test_build_harnesses_term_still_local():
    out = build_harnesses(
        [{"id": "t", "type": "term", "name": "term", "command": "btop"}],
        Bus(), TaskRegistry(Bus()))
    assert isinstance(out["t"], TermHarness)
    assert not isinstance(out["t"], RemoteTermHarness)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
