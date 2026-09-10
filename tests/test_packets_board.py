"""AION-003: frontmatter parser, staleness, collisions, fixture scan.

Gate: python -m pytest tests/test_packets_board.py -q
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion.ui.packets_board import (  # noqa: E402
    Packet,
    PacketsBoard,
    parse_frontmatter,
    render_packets_board,
    scan_packets,
)

THEME = {"dim": "#9aabbb", "accent": "#5ad1ff", "ok": "#7CFFB2",
         "warn": "#FFD479", "err": "#FF8A8A", "faint": "#6b7d8d"}
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
FM = "---\nid: PR-101\ntrack: praxis\nphase: 0\nstatus: review\nrung: 1\ndepends: [PR-000]\n---\n\n# body\n"


def test_parse_wellformed():
    fm = parse_frontmatter(FM)
    assert fm["id"] == "PR-101" and fm["depends"] == ["PR-000"]
    assert fm["phase"] == "0" and fm["status"] == "review"


def test_parse_missing_and_malformed():
    assert parse_frontmatter("# no block") == {}
    assert parse_frontmatter("---\nunclosed") == {}
    assert parse_frontmatter("") == {}
    assert Packet.from_dict({}) is None
    assert Packet.from_dict({"id": "X"}).status == "todo"


def test_stale_math_fake_clock():
    old = Packet(id="A", status="claimed", claimed_at="2026-09-01T00:00:00Z")
    assert old.stale_claim(NOW)
    fresh = Packet(id="B", status="claimed",
                   claimed_at="2026-09-10T06:00:00Z")
    assert not fresh.stale_claim(NOW)
    assert not Packet(id="C", status="todo").stale_claim(NOW)
    assert not Packet(id="D", status="claimed",
                      claimed_at="garbage").stale_claim(NOW)


def test_fixture_scan_all_variants(tmp_path):
    (tmp_path / "a-todo.md").write_text(
        "---\nid: A-1\nphase: 0\nstatus: todo\n---\n")
    (tmp_path / "b-done.md").write_text(
        "---\nid: B-1\nphase: 1\nstatus: done\n---\n")
    (tmp_path / "c-blocked.md").write_text(
        "---\nid: C-1\nphase: 1\nstatus: blocked\n---\n")
    (tmp_path / "d-stale.md").write_text(
        "---\nid: D-1\nphase: 0\nstatus: claimed\nclaimed_by: bot\n"
        "claimed_at: 2026-08-01T00:00:00Z\n---\n")
    (tmp_path / "e-dup.md").write_text(
        "---\nid: A-1\nphase: 2\nstatus: todo\n---\n")
    import aion.ui.packets_board as pb
    board = pb.PacketsBoard.from_files([str(p) for p in tmp_path.iterdir()])
    assert len(board.packets) == 5
    assert len(board.by_status()["todo"]) == 2
    assert len(board.stale_claims(NOW)) == 1
    assert len(board.blocked()) == 1
    assert list(board.collisions()) == ["A-1"]
    out = render_packets_board(board, THEME, NOW)
    assert "collision A-1" in out and "blocked" in out and "stale claim" in out


def test_live_scan_runs():
    paths = scan_packets()
    assert isinstance(paths, list)
    board = PacketsBoard.from_files(paths)
    assert "packets" in board.summary()
    assert isinstance(render_packets_board(board, THEME), str)
