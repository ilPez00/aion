"""Tests for the vault note writer (Cycle C)."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.notes import write_note, vault_root


def test_write_note_creates_md_with_frontmatter():
    with tempfile.TemporaryDirectory() as d:
        out = write_note("ideas/agent-loop", "the agent can now write notes",
                         root=Path(d), tags=["ai", "cockpit"])
        p = Path(out)
        assert p.exists()
        assert p.name == "agent-loop.md"
        assert p.parent.name == "ideas"
        text = p.read_text()
        assert "title: Agent-Loop" in text
        assert "tags: [ai, cockpit]" in text
        assert "the agent can now write notes" in text


def test_write_note_sanitizes_path():
    with tempfile.TemporaryDirectory() as d:
        # '..' and absolute attempts must be neutralized
        out = write_note("../../etc/evil", "x", root=Path(d))
        assert ".." not in out  # resolved inside root
        assert out.startswith(str(Path(d)))


def test_write_note_empty_path_raises():
    with pytest.raises(ValueError):
        write_note("", "x", root=Path("/tmp"))
