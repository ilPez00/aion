"""
term.py — the Jarvis "Term" workspace: a real embedded terminal pane.

Runs a real program (default btop, else top/htop, else bash) inside a pty
and renders its screen with pyte — so full-screen TUIs like btop/htop work
*inside* aion's center pane, and your keystrokes are piped straight to the
pty. No fake "snapshot" — you get the actual live process.

Design:
  - TermHarness owns the pty + pyte.Screen + a pump thread (reads master fd
    off the event loop).
  - It is LAZY: spawned on first entry to the Term workspace, killed on exit
    (considerate on the constrained host — no btop running 24/7).
  - app.py mounts a TermPane widget that re-renders screen.display on a fast
    timer, and forwards keys to harness.send() while the workspace is active.
"""
from __future__ import annotations

import os
import pty
import asyncio
import select
import shutil
import signal
import threading
import fcntl
import termios
import struct

import pyte

from .harnesses import Harness, HarnessConfig, Task, TaskRegistry, Bus  # noqa: E402


def _detect_cmd() -> str:
    for c in ("btop", "top", "htop"):
        if shutil.which(c):
            return c
    return os.environ.get("SHELL", "/bin/bash")


class TermHarness(Harness):
    """Owns one pty-bound program + its pyte screen."""

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry, store=None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        # cfg.command is the standard HarnessConfig field; fall back to
        # extra["command"] then auto-detect (btop > top > htop > shell)
        self.command = cfg.command or extra.get("command") or _detect_cmd()
        self.cols = int(extra.get("cols", 110))
        self.rows = int(extra.get("rows", 32))
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.ByteStream(self.screen)
        self.pid: int | None = None
        self.master: int | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.running = False

    # ---- lifecycle -------------------------------------------------------
    def ensure_running(self) -> None:
        if self.running:
            return
        pid, master = pty.fork()
        if pid == 0:  # child
            try:
                os.environ["TERM"] = "xterm-256color"
                os.environ["COLUMNS"] = str(self.cols)
                os.environ["LINES"] = str(self.rows)
                import shlex
                argv = shlex.split(self.command)
                os.execvp(argv[0], argv)
            except Exception:
                os._exit(127)
        # parent
        self.pid, self.master = pid, master
        # set the pty window size so TUIs (btop/htop/top) don't choke on 0x0
        try:
            winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        self.running = True

    def stop(self) -> None:
        pid = self.pid
        master = self.master
        self.master = None
        self.running = False
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            # force-kill if it ignores TERM (e.g. `cat` waits on stdin EOF)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            # reap so it doesn't linger as a zombie
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
        if master is not None:
            try:
                os.close(master)   # EOF on the slave -> `cat` etc. exit
            except OSError:
                pass

    def _pump(self) -> None:
        master = self.master
        while master is not None:
            try:
                r, _, _ = select.select([master], [], [], 0.1)
                if not r:
                    continue
                data = os.read(master, 65536)
                if not data:
                    break
                with self._lock:
                    self.stream.feed(data)
            except OSError:
                break

    # ---- io --------------------------------------------------------------
    def send(self, data: bytes) -> None:
        if self.master is not None:
            try:
                os.write(self.master, data)
            except OSError:
                pass

    def render(self) -> str:
        with self._lock:
            return "\n".join(self.screen.display)

    async def run(self, task: Task, prompt: str = "") -> None:  # pragma: no cover
        # TermHarness is a lazy pane owned by the app, not a task.
        # If ever launched as a task, just hold the pty until it exits.
        self.ensure_running()
        while self.pid:
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
                if pid != 0:
                    break
            except ChildProcessError:
                break
            await asyncio.sleep(0.2)
        self.stop()
