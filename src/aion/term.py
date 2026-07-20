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
  - RemoteTermHarness is the same class with one thing swapped: the argv
    handed to the pty. `ssh -tt` gives the remote program a real tty, so the
    pyte layer above cannot tell local from remote.
"""
from __future__ import annotations

import os
import pty
import asyncio
import select
import shlex
import shutil
import signal
import threading
import fcntl
import termios
import struct

import pyte

from .harnesses import Harness, HarnessConfig, Task, TaskRegistry, Bus  # noqa: E402
from .nodes import LOCAL, Node, NodeRegistry, load_nodes  # noqa: E402


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
    def argv(self) -> list[str]:
        """What the pty should exec. The single seam subclasses swap."""
        return shlex.split(self.command)

    def ensure_running(self) -> None:
        if self.running:
            return
        argv = self.argv()
        pid, master = pty.fork()
        if pid == 0:  # child
            try:
                os.environ["TERM"] = "xterm-256color"
                os.environ["COLUMNS"] = str(self.cols)
                os.environ["LINES"] = str(self.rows)
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


class RemoteTermHarness(TermHarness):
    """A Term pane whose program runs on another machine.

    Everything above the pty is unchanged: `ssh -tt` allocates a real tty on
    the far side, so pyte parses the same escape sequences it would locally
    and app.py's TermPane needs no knowledge of nodes at all.

    Config (`HarnessConfig`):
        remote           node name from config/nodes.json ("forge")
        extra.pane       tmux target to attach to ("%3", or "agents:0.0")
        extra.read_only  attach without taking control (default True)
        command          any command, when no pane is given

    Sizing gotcha: a tmux session sizes to its smallest attached client, so
    attaching aion's 110x32 pane to a session you are using at 200x50 shrinks
    *your* view. Read-only attach is the default for that reason; to type into
    a remote agent prefer `send_keys()` in collectors/agents.py, which needs no
    attach at all. If you do want an interactive attach, set
    `setw -g window-size latest` on the remote tmux.
    """

    def __init__(self, cfg: HarnessConfig, bus: Bus, registry: TaskRegistry,
                 store=None, node: Node | None = None,
                 nodes: NodeRegistry | None = None):
        super().__init__(cfg, bus, registry, store)
        extra = cfg.extra or {}
        if node is not None:
            self.node = node
        else:
            # cfg.remote used to be a "host:port" stub (lesson #6); it is now
            # a node name, which is the same idea with the transport solved.
            self.node = (nodes or load_nodes()).get(cfg.remote or extra.get("node"))
        self.pane = extra.get("pane")
        self.read_only = bool(extra.get("read_only", True))

    def argv(self) -> list[str]:
        inner = self._attach_argv() if self.pane else shlex.split(self.command)
        return self.node.ssh_interactive_argv(inner)

    def _attach_argv(self) -> list[str]:
        argv = ["tmux", "attach", "-t", str(self.pane)]
        if self.read_only:
            argv += ["-r"]
        # A pane target like "agents:0.0" attaches to the session but does not
        # select the window/pane, so chain the selects. tmux treats a literal
        # ";" argument as a command separator; shlex.join quotes it, which the
        # remote shell strips back to a plain argument rather than an operator.
        target = str(self.pane)
        if ":" in target:
            argv += [";", "select-window", "-t", target]
            if "." in target.split(":", 1)[1]:
                argv += [";", "select-pane", "-t", target]
        return argv

    def stop(self) -> None:
        # Killing the local ssh client is enough: -tt means the far side sees
        # its tty close and the remote command exits with it. We deliberately
        # do NOT kill the remote tmux session — that is the user's agent.
        super().stop()

    @property
    def label(self) -> str:
        where = self.node.label
        return f"{where}:{self.pane}" if self.pane else f"{where}:{self.command}"
