"""test_collectors_nodes.py — node-parameterized agent + session collectors.

The local path is asserted to be unchanged (it must stay the fast /proc +
pgrep path). The remote path is driven with a fake Node so no ssh, no
network, no second machine is needed: we assert on the argv aion would send
and on how it parses what comes back.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.nodes import LOCAL, Node, NodeRegistry, NodeResult  # noqa: E402
from aion.hermes.hud.collectors import agents as A  # noqa: E402
from aion.hermes.hud.collectors import sessions as S  # noqa: E402


class FakeNode(Node):
    """A Node whose run/fetch are scripted. Records every argv it was given."""

    def __init__(self, name="forge", responses=None, files=None, **kw):
        super().__init__(name=name, host="forge.ts.net", transport="ssh",
                         user="gio", home="/home/gio", **kw)
        object.__setattr__(self, "_responses", responses or {})
        object.__setattr__(self, "_files", files or {})
        object.__setattr__(self, "calls", [])

    def run(self, argv, timeout=10, input_text=None) -> NodeResult:
        self.calls.append(argv)
        for key, res in self._responses.items():
            if key in " ".join(argv):
                return res
        return NodeResult(self.name, argv, 1, "", "no fake response")

    def fetch(self, path, ttl_s=30.0):
        return self._files.get(self.path(path))


def _ok(stdout: str, node="forge") -> NodeResult:
    return NodeResult(node, [], 0, stdout, "")


def _down(node="forge") -> NodeResult:
    return NodeResult(node, [], 255, "", "no route to host", unreachable=True)


PS_OUTPUT = """\
 1234  204800 02:15:30 pts/3    hermes chat --cli
 1235   51200 00:04:01 pts/5    node /home/gio/.local/bin/opencode
 1236    2048 10:00:00 ?        /usr/bin/sshd -D
 1237  102400 01:00:00 pts/7    claude --dangerously-skip-permissions
"""

TMUX_OUTPUT = (
    "%0\t/dev/pts/3\tagents\t0\t0\thermes\t1234\n"
    "%1\t/dev/pts/5\tagents\t1\t0\tnode\t1235\n"
)


# --------------------------------------------------------------------------
# local path must not regress
# --------------------------------------------------------------------------
def test_local_agents_still_uses_proc_fast_path(monkeypatch):
    called = []
    monkeypatch.setattr(A, "_scan_processes_local",
                        lambda: called.append("local") or [])
    monkeypatch.setattr(A, "_list_tmux_panes", lambda node: [])
    monkeypatch.setattr(A, "_get_recent_sessions",
                        lambda d, node=LOCAL, **k: [])
    state = A.collect_agents()
    assert called == ["local"]
    assert state.node == "local"
    assert not state.unreachable


def test_local_sessions_reads_db_in_place(tmp_path):
    db = _make_db(tmp_path)
    state = S.collect_sessions(str(tmp_path))
    assert state.node == "local"
    assert not state.unreachable
    assert [s.id for s in state.sessions] == ["s1"]
    assert state.sessions[0].node == "local"
    assert db.exists()   # never copied, never moved


# --------------------------------------------------------------------------
# remote process scan: ONE ps for the whole node
# --------------------------------------------------------------------------
def test_remote_scan_is_a_single_ps():
    node = FakeNode(responses={"ps -eo": _ok(PS_OUTPUT), "readlink": _ok("")})
    procs, unreachable = A._scan_processes_remote(node)
    assert not unreachable
    ps_calls = [c for c in node.calls if c[:2] == ["ps", "-eo"]]
    assert len(ps_calls) == 1          # not one per agent binary


def test_remote_scan_finds_agents_and_tags_node():
    node = FakeNode(responses={"ps -eo": _ok(PS_OUTPUT), "readlink": _ok("")})
    procs, _ = A._scan_processes_remote(node)
    live = {p.name: p for p in procs if p.running}
    assert set(live) == {"hermes", "opencode", "claude"}
    assert live["hermes"].pid == 1234
    assert live["hermes"].node == "forge"
    assert live["hermes"].tty == "pts/3"
    assert live["hermes"].mem_mb == 200.0
    assert live["hermes"].uptime_seconds == 2 * 3600 + 15 * 60 + 30


def test_remote_scan_emits_idle_rows_for_absent_agents():
    node = FakeNode(responses={"ps -eo": _ok(PS_OUTPUT), "readlink": _ok("")})
    procs, _ = A._scan_processes_remote(node)
    idle = {p.name for p in procs if not p.running}
    assert "aider" in idle and "codex" in idle   # HUD still renders their rows
    assert all(p.node == "forge" for p in procs)


def test_remote_scan_ignores_our_own_probe():
    # our ssh'd `ps` shows up in the remote table and contains no agent name,
    # but a probe that did would otherwise be reported as a live agent
    noisy = PS_OUTPUT + " 9999 1024 00:00:01 ?        sh -c ps -eo pid=,args= hermes\n"
    node = FakeNode(responses={"ps -eo": _ok(noisy), "readlink": _ok("")})
    procs, _ = A._scan_processes_remote(node)
    assert 9999 not in [p.pid for p in procs]


def test_remote_scan_unreachable_is_not_empty_fleet():
    node = FakeNode(responses={"ps -eo": _down()})
    procs, unreachable = A._scan_processes_remote(node)
    assert unreachable and procs == []


def test_remote_ps_failure_still_lists_agents_idle():
    node = FakeNode(responses={"ps -eo": NodeResult("forge", [], 1, "", "boom")})
    procs, unreachable = A._scan_processes_remote(node)
    assert not unreachable            # host answered; ps just failed
    assert procs and all(not p.running for p in procs)


def test_remote_cwd_batched_into_one_call():
    readlink = _ok("1234 /home/gio/aion\n1235 /home/gio/cyclops\n")
    node = FakeNode(responses={"ps -eo": _ok(PS_OUTPUT), "readlink": readlink})
    procs, _ = A._scan_processes_remote(node)
    rl_calls = [c for c in node.calls if "readlink" in " ".join(c)]
    assert len(rl_calls) == 1         # not one ssh per pid
    by_pid = {p.pid: p for p in procs}
    assert by_pid[1234].cwd == "~/aion"          # shortened vs REMOTE home
    assert by_pid[1235].cwd == "~/cyclops"


def test_remote_cwd_shortens_against_remote_home_not_ours():
    node = FakeNode(responses={"ps -eo": _ok(PS_OUTPUT),
                               "readlink": _ok("1234 /data/x\n")},
                    )
    node.home = "/data"
    procs, _ = A._scan_processes_remote(node)
    assert {p.pid: p.cwd for p in procs}[1234] == "~/x"


# --------------------------------------------------------------------------
# remote tmux
# --------------------------------------------------------------------------
def test_remote_tmux_panes_tagged_with_node():
    node = FakeNode(responses={"list-panes": _ok(TMUX_OUTPUT)})
    panes = A._list_tmux_panes(node)
    assert [p.pane_id for p in panes] == ["%0", "%1"]
    assert all(p.node == "forge" for p in panes)
    assert panes[0].pane_pid == 1234


def test_pane_target_is_node_qualified():
    node = FakeNode(responses={"list-panes": _ok(TMUX_OUTPUT)})
    remote_pane = A._list_tmux_panes(node)[0]
    assert remote_pane.target == "forge:%0"
    local_pane = A.TmuxPane("%0", "s", 0, 0, "/dev/pts/1", "hermes", 1)
    assert local_pane.target == "%0"     # local stays unqualified


def test_remote_tmux_absent_is_empty_not_error():
    node = FakeNode(responses={"list-panes": NodeResult("forge", [], 127, "",
                                                        "tmux: not found")})
    assert A._list_tmux_panes(node) == []


def test_matching_reuses_tty_from_ps_without_extra_call():
    node = FakeNode(responses={"ps -eo": _ok(PS_OUTPUT), "readlink": _ok(""),
                               "list-panes": _ok(TMUX_OUTPUT)})
    procs, _ = A._scan_processes_remote(node)
    panes = A._list_tmux_panes(node)
    node.calls.clear()
    A._match_processes_to_panes(procs, panes, node)
    # tty already came out of the single ps -> no `ps -o pid=,tty=` round trip
    assert not [c for c in node.calls if "tty=" in " ".join(c)]
    hermes = next(p for p in procs if p.name == "hermes")
    assert hermes.tmux_pane == "%0"
    assert hermes.tmux_jump_hint == "agents:0.0"


def test_capture_preview_targets_the_node():
    node = FakeNode(responses={"capture-pane": _ok("line one\n\nline two\n")})
    assert A._capture_pane_preview("%0", node=node) == ["line one", "line two"]
    assert node.calls[0][:2] == ["tmux", "capture-pane"]


# --------------------------------------------------------------------------
# send_keys: the control half
# --------------------------------------------------------------------------
def test_send_keys_uses_literal_flag_then_enter():
    node = FakeNode(responses={"send-keys": _ok("")})
    assert A.send_keys("%0", "yes", node=node) is True
    assert node.calls[0] == ["tmux", "send-keys", "-t", "%0", "-l", "yes"]
    assert node.calls[1] == ["tmux", "send-keys", "-t", "%0", "Enter"]


def test_send_keys_can_skip_enter():
    node = FakeNode(responses={"send-keys": _ok("")})
    A.send_keys("%0", "partial", node=node, enter=False)
    assert len(node.calls) == 1


def test_send_keys_literal_prevents_keyname_interpretation():
    # "Enter" as *text* must not be sent as the Enter key
    node = FakeNode(responses={"send-keys": _ok("")})
    A.send_keys("%0", "Enter", node=node, enter=False)
    assert "-l" in node.calls[0]


def test_send_keys_reports_failure():
    node = FakeNode(responses={"send-keys": _down()})
    assert A.send_keys("%0", "yes", node=node) is False


# --------------------------------------------------------------------------
# collect_agents end to end, plus multi-node
# --------------------------------------------------------------------------
def test_collect_agents_remote_short_circuits_when_down():
    node = FakeNode(responses={"ps -eo": _down()})
    state = A.collect_agents(node=node)
    assert state.unreachable
    assert state.node == "forge"
    # no point paying more round trips on a node that just refused
    assert not [c for c in node.calls if "tmux" in " ".join(c)]


def test_collect_agents_multi_keys_by_node(monkeypatch):
    monkeypatch.setattr(A, "_scan_processes_local", lambda: [])
    monkeypatch.setattr(A, "_list_tmux_panes", lambda node: [])
    monkeypatch.setattr(A, "_get_recent_sessions", lambda d, node=LOCAL, **k: [])
    reg = NodeRegistry([FakeNode(responses={"ps -eo": _down()})])
    states = A.collect_agents_multi(reg)
    assert set(states) == {"local", "forge"}
    assert states["forge"].unreachable
    assert not states["local"].unreachable


def test_merge_keeps_node_attribution():
    a = A.AgentsState(processes=[A.AgentProcess("hermes", "hermes", True, 1,
                                                node="local")], node="local")
    b = A.AgentsState(processes=[A.AgentProcess("hermes", "hermes", True, 2,
                                                node="forge")], node="forge")
    merged = A.merge_agent_states({"local": a, "forge": b})
    assert merged.node == "all"
    assert {p.node for p in merged.processes} == {"local", "forge"}
    assert merged.live_count == 2


def test_merge_does_not_propagate_one_dead_node():
    good = A.AgentsState(node="local")
    dead = A.AgentsState(node="forge", unreachable=True)
    # one box asleep must not make the whole fleet render as down
    assert not A.merge_agent_states({"local": good, "forge": dead}).unreachable


# --------------------------------------------------------------------------
# sessions over a node
# --------------------------------------------------------------------------
def _make_db(dirpath: Path) -> Path:
    db = dirpath / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE sessions (
        id TEXT, source TEXT, title TEXT, started_at REAL, ended_at REAL,
        message_count INT, tool_call_count INT, input_tokens INT,
        output_tokens INT, cache_read_tokens INT, cache_write_tokens INT,
        reasoning_tokens INT, estimated_cost_usd REAL, model TEXT,
        model_config TEXT)""")
    conn.execute("CREATE TABLE messages (id INT, session_id TEXT, tool_calls TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1','cli','t',1700000000,"
                 "1700003600,10,4,100,200,0,0,0,0.01,'opus-4.8',NULL)")
    conn.commit()
    conn.close()
    return db


def test_remote_sessions_fetch_then_open(tmp_path):
    db = _make_db(tmp_path)
    node = FakeNode(files={"/home/gio/.hermes/state.db": db})
    state = S.collect_sessions(node=node)
    assert state.node == "forge"
    assert not state.unreachable
    assert [s.id for s in state.sessions] == ["s1"]
    assert state.sessions[0].node == "forge"      # attribution survives


def test_remote_sessions_resolve_tilde_against_remote_home(tmp_path):
    db = _make_db(tmp_path)
    # keyed on the REMOTE resolution of ~/.hermes; a local expanduser misses
    node = FakeNode(files={"/home/gio/.hermes/state.db": db})
    assert S.collect_sessions(node=node).total_sessions == 1


def test_remote_sessions_ignore_our_hermes_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/opt/my-local-hermes")
    db = _make_db(tmp_path)
    node = FakeNode(files={"/home/gio/.hermes/state.db": db})
    # HERMES_HOME describes this box only; the remote must not inherit it
    assert S.collect_sessions(node=node).total_sessions == 1


def test_remote_sessions_unreachable_flagged():
    node = FakeNode(files={})           # fetch returns None
    state = S.collect_sessions(node=node)
    assert state.unreachable
    assert state.total_sessions == 0


def test_local_missing_db_is_not_unreachable(tmp_path):
    state = S.collect_sessions(str(tmp_path / "nowhere"))
    assert not state.unreachable       # our own box is never "down"
    assert state.total_sessions == 0


def test_collect_sessions_multi_keys_by_node(tmp_path):
    # an explicit hermes_dir is an explicit override: it applies to every node
    db = _make_db(tmp_path)
    reg = NodeRegistry([FakeNode(files={str(db): db})])
    states = S.collect_sessions_multi(reg, str(tmp_path))
    assert set(states) == {"local", "forge"}
    assert states["forge"].total_sessions == 1
    assert states["local"].total_sessions == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
