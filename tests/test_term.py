"""
test_term.py — unit tests for the embedded Term pane (pyte + pty).

We don't spawn a real TUI (btop/htop) in CI; instead we verify the harness
pumps a pty program's output into the pyte screen and renders it, and that
keystrokes are written to the pty master. Uses a tiny shell loop as the
command so it runs anywhere without extra binaries.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.term import TermHarness  # noqa: E402
from aion.harnesses import HarnessConfig  # noqa: E402


def test_term_harness_spawns_and_renders():
    # a tiny program that prints a marker so we know the pty pipeline works
    cfg = HarnessConfig.from_dict(
        {"id": "term", "type": "term",
         "command": "sh -c 'echo AION_TERM_MARKER; sleep 5'",
         "cols": 80, "rows": 24})
    h = TermHarness(cfg, bus=None, registry=None)
    try:
        h.ensure_running()
        # give the pump thread time to read + feed pyte
        for _ in range(50):
            if "AION_TERM_MARKER" in h.render():
                break
            time.sleep(0.05)
        out = h.render()
        assert "AION_TERM_MARKER" in out, out
        assert h.running
        print("ok: pty program output reaches pyte screen")
    finally:
        h.stop()


def test_term_harness_send_writes_pty():
    # spawn a `cat` that echoes stdin -> we send bytes and expect them back
    cfg = HarnessConfig.from_dict(
        {"id": "term", "type": "term",
         "command": "cat", "cols": 80, "rows": 24})
    h = TermHarness(cfg, bus=None, registry=None)
    try:
        h.ensure_running()
        time.sleep(0.1)
        h.send(b"HELLO_PTY\n")
        got = b""
        for _ in range(50):
            # read back from the master to confirm the byte was written
            import os, select
            r, _, _ = select.select([h.master], [], [], 0.05)
            if r:
                got += os.read(h.master, 1024)
            if b"HELLO_PTY" in got:
                break
        assert b"HELLO_PTY" in got, got
        print("ok: send() writes keystrokes to the pty master")
    finally:
        h.stop()


def test_term_harness_stop_is_clean():
    cfg = HarnessConfig.from_dict(
        {"id": "term", "type": "term",
         "command": "sleep 30", "cols": 80, "rows": 24})
    h = TermHarness(cfg, bus=None, registry=None)
    h.ensure_running()
    assert h.running and h.pid
    h.stop()
    # give SIGTERM a moment
    time.sleep(0.2)
    assert not h.running
    import os
    try:
        os.kill(h.pid, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive, "child still alive after stop()"
    print("ok: stop() terminates the child process")


def _run():
    test_term_harness_spawns_and_renders()
    test_term_harness_send_writes_pty()
    test_term_harness_stop_is_clean()
    print("\nALL TERM TESTS PASSED")


if __name__ == "__main__":
    _run()
