"""Tests for the RandoMesh node monitor pure logic (no network)."""
from aion.meshmon import _parse_stat, probe_node, snapshot, NODES


def _fake(rc=0, out=""):
    def t(method, target, cmd):
        return rc, out
    return t


BLOCK = (
    " 12:34:56 up 3 days,  4:00,  2 users,  load average: 0.42, 0.30, 0.21"
    "__S__0.42 0.30 0.21 1/234 5678"
    "__S__Mem:  16000000000 8000000000 8000000000  0  0"
    "__S__/dev/sda2  916G 200G 716G 22% /"
)


def test_parse_stat_full():
    s = _parse_stat(BLOCK, "pansa", "storage-node")
    assert s.reachable is False  # pure parser: reachability set by probe_node
    assert s.load1 == 0.42
    assert s.ram_pct == 50
    assert s.disk_pct == 22
    assert "3 days" in s.uptime
    assert s.role == "storage-node"


def test_probe_node_unreachable():
    s = probe_node("air", transport=_fake(1, ""))
    assert s.reachable is False
    assert s.note == "unreachable"


def test_probe_node_with_fake_block():
    s = probe_node("pansa", transport=_fake(0, BLOCK))
    assert s.reachable is True
    assert s.disk_pct == 22


def test_snapshot_counts_and_soft_fails():
    # air down, pansa up; others unreachable fake
    def mixed(method, target, cmd):
        return (0, BLOCK) if target == "pansa-ts" else (1, "")
    snap = snapshot(transport=mixed)
    assert snap["total"] == len(NODES)
    # only pansa reachable in this fake
    assert snap["reachable"] == 1
    names = {n["name"] for n in snap["nodes"]}
    assert names == set(NODES.keys())
    # unreachable nodes still present, marked down (soft-fail, no crash)
    air = [n for n in snap["nodes"] if n["name"] == "air"][0]
    assert air["reachable"] is False
