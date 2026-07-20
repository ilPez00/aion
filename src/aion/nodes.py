"""
nodes.py — the machine seam: "where does this collector look?"

aion started machine-local: every collector shells out to a local `tmux`,
opens `~/.hermes/state.db` directly, forks a local pty. To see (and drive)
agent sessions on the *other* boxes, all of that has to go through one
object that knows whether "here" is this machine or a box across a
Tailscale/WireGuard overlay.

That object is `Node`. It exposes three primitives and nothing else:

    node.run(argv)          -> NodeResult      (like subprocess.run)
    node.fetch(remote_path) -> local Path      (copy-then-read, never mount)
    node.exists(path)       -> bool

A collector written against these works unchanged on every machine. The
local node is the degenerate case, not a special case — `LOCAL.run()` is a
plain `subprocess.run`, so nothing gets slower or more fragile for the
single-machine user.

Transport is ssh, deliberately. It gives us auth, encryption and multiplexing
for free, and needs no daemon deployed to each box (see the "trap to avoid"
in docs/PLAN.md). Every remote call rides one long-lived ControlMaster
connection, so a `run()` costs a round-trip, not a TCP+TLS handshake.

sqlite note: remote state.db files are *copied* into a local cache before
being opened. Opening sqlite over sshfs/NFS corrupts it — copy, don't mount.

No Textual, no bus, no I/O beyond subprocess: unit-testable in isolation,
same contract as store.py.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ssh multiplexing: first connection to a host opens a master socket, every
# later one reuses it. ControlPersist keeps it warm between HUD polls.
_SSH_BASE_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=10m",
    "-o", "BatchMode=yes",           # never block the HUD on a password prompt
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=accept-new",
]

DEFAULT_TIMEOUT = 10
# Remote files are cached this long before a re-fetch. HUD polls are frequent;
# state.db is a few MB. Re-copying it every tick would saturate the link.
FETCH_TTL_S = 30.0

TRANSPORT_LOCAL = "local"
TRANSPORT_SSH = "ssh"


def cache_dir() -> Path:
    """Where fetched remote files and ssh control sockets live."""
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "aion"
    return base


@dataclass
class NodeResult:
    """subprocess.run's useful subset, plus which node produced it."""
    node: str
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    # True when the transport itself failed (host down, ssh refused) rather
    # than the remote command exiting non-zero. Collectors degrade on this:
    # an unreachable node renders as offline, not as an empty agent list.
    unreachable: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.unreachable


@dataclass
class Node:
    """One machine aion can observe and drive.

    name      short id used in config, UI labels and cache paths ("forge")
    host      ssh destination; ignored for the local transport
    transport "local" | "ssh"
    user      ssh user; None -> ssh config decides
    home      remote $HOME. Needed because we resolve "~/.hermes/state.db"
              ourselves rather than letting a remote shell expand it (the
              ssh command runs without a login shell under BatchMode).
    identity  path to a private key, if not the agent default
    port      ssh port
    enabled   skip without deleting from config
    """
    name: str
    host: str = "localhost"
    transport: str = TRANSPORT_LOCAL
    user: str | None = None
    home: str | None = None
    identity: str | None = None
    port: int | None = None
    enabled: bool = True
    extra: dict = field(default_factory=dict)

    # ---- construction ----------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        known = {"name", "host", "transport", "user", "home",
                 "identity", "port", "enabled"}
        return cls(
            name=d["name"],
            host=d.get("host", "localhost"),
            transport=d.get("transport", TRANSPORT_LOCAL),
            user=d.get("user"),
            home=d.get("home"),
            identity=d.get("identity"),
            port=d.get("port"),
            enabled=d.get("enabled", True),
            extra={k: v for k, v in d.items() if k not in known},
        )

    def as_dict(self) -> dict:
        return {
            "name": self.name, "host": self.host, "transport": self.transport,
            "user": self.user, "home": self.home, "port": self.port,
            "enabled": self.enabled, "local": self.is_local,
        }

    @property
    def is_local(self) -> bool:
        return self.transport == TRANSPORT_LOCAL

    @property
    def label(self) -> str:
        """What the HUD prints next to a session/pane."""
        return "local" if self.is_local else self.name

    # ---- path resolution -------------------------------------------------
    def path(self, p: str | Path) -> str:
        """Resolve a path *on this node*, expanding ~ against the right home.

        Local: normal expanduser. Remote: substitute the configured home, or
        fall back to /home/<user> — we never let the remote shell expand it,
        because these strings get shlex-quoted into an ssh argv.
        """
        s = str(p)
        if self.is_local:
            return str(Path(s).expanduser())
        if not s.startswith("~"):
            return s
        home = self.home or (f"/home/{self.user}" if self.user else None)
        if home is None:
            # Last resort: let the remote side expand. Works because we only
            # hit this when the caller gave no home hint and no user.
            return s
        return home + s[1:]

    # ---- primitives ------------------------------------------------------
    def run(self, argv: list[str], timeout: int = DEFAULT_TIMEOUT,
            input_text: str | None = None) -> NodeResult:
        """Run argv on this node. Never raises: failures come back as results.

        Collectors run on a HUD timer; an exception there kills a poller, so
        every failure mode is folded into NodeResult instead.
        """
        full = argv if self.is_local else self._ssh_argv(argv)
        try:
            proc = subprocess.run(
                full, capture_output=True, text=True, timeout=timeout,
                input=input_text,
            )
        except subprocess.TimeoutExpired:
            return NodeResult(self.name, argv, 124, "",
                              f"timeout after {timeout}s", unreachable=not self.is_local)
        except (FileNotFoundError, OSError) as e:
            return NodeResult(self.name, argv, 127, "", str(e),
                              unreachable=not self.is_local)
        # ssh exits 255 on transport failure; the remote command never ran.
        unreachable = (not self.is_local) and proc.returncode == 255
        return NodeResult(self.name, argv, proc.returncode,
                          proc.stdout or "", proc.stderr or "",
                          unreachable=unreachable)

    def exists(self, path: str | Path) -> bool:
        resolved = self.path(path)
        if self.is_local:
            return Path(resolved).exists()
        return self.run(["test", "-e", resolved], timeout=8).ok

    def fetch(self, path: str | Path, ttl_s: float = FETCH_TTL_S) -> Path | None:
        """Make a remote file readable locally; return the local path.

        Local nodes short-circuit to the file itself — no copy, no cache.
        Remote files land in ~/.cache/aion/nodes/<node>/<flattened path> and
        are reused for ttl_s. Returns None when the file is missing or the
        copy failed, so callers treat it like a missing local file.

        Use this for anything sqlite opens. Copy-then-read is the whole point.
        """
        resolved = self.path(path)
        if self.is_local:
            p = Path(resolved)
            return p if p.exists() else None

        dest = cache_dir() / "nodes" / self.name / resolved.lstrip("/").replace("/", "_")
        if dest.exists() and (time.time() - dest.stat().st_mtime) < ttl_s:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        tmp = dest.with_suffix(dest.suffix + ".part")
        argv = ["scp", "-q", *self._ssh_opts(),
                f"{self._ssh_dest()}:{shlex.quote(resolved)}", str(tmp)]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            tmp.unlink(missing_ok=True)
            return dest if dest.exists() else None
        if proc.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            # Serve a stale copy rather than nothing: a node that just went
            # down should show its last known state, not vanish from the HUD.
            return dest if dest.exists() else None
        tmp.replace(dest)
        return dest

    def reachable(self) -> bool:
        if self.is_local:
            return True
        return self.run(["true"], timeout=8).ok

    # ---- ssh plumbing ----------------------------------------------------
    def _ssh_dest(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _ssh_opts(self) -> list[str]:
        sock_dir = cache_dir() / "ssh"
        sock_dir.mkdir(parents=True, exist_ok=True)
        opts = [*_SSH_BASE_OPTS, "-o", f"ControlPath={sock_dir}/%r@%h:%p"]
        if self.identity:
            opts += ["-i", str(Path(self.identity).expanduser())]
        if self.port:
            opts += ["-p", str(self.port)]
        return opts

    def _ssh_argv(self, argv: list[str]) -> list[str]:
        # shlex.join because the remote side runs this through a shell; an
        # unquoted project path with a space would otherwise split.
        return ["ssh", *self._ssh_opts(), self._ssh_dest(), shlex.join(argv)]

    def ssh_interactive_argv(self, argv: list[str]) -> list[str]:
        """argv for a *pty-attached* remote command, for RemoteTermHarness.

        `-tt` forces a tty so full-screen TUIs (tmux attach, btop) render.
        Local nodes get the bare argv — term.py forks a pty either way.
        """
        if self.is_local:
            return argv
        return ["ssh", "-tt", *self._ssh_opts(), self._ssh_dest(), shlex.join(argv)]


# The always-present node. Collectors default to this, so single-machine
# behaviour is identical to before nodes existed.
LOCAL = Node(name="local", host="localhost", transport=TRANSPORT_LOCAL)


# --------------------------------------------------------------------------
# registry / config
# --------------------------------------------------------------------------
def default_config_path() -> Path:
    # repo layout: .../aion/src/aion/nodes.py -> config at repo root /config
    return Path(__file__).resolve().parents[2] / "config" / "nodes.json"


class NodeRegistry:
    """All machines aion knows, with `local` guaranteed present and first."""

    def __init__(self, nodes: list[Node] | None = None) -> None:
        self._nodes: dict[str, Node] = {LOCAL.name: LOCAL}
        for n in nodes or []:
            self._nodes[n.name] = n

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self):
        return iter(self.all())

    def all(self, include_disabled: bool = False) -> list[Node]:
        """local first, then config order — the HUD lists your box on top."""
        out = [self._nodes[LOCAL.name]]
        out += [n for name, n in self._nodes.items()
                if name != LOCAL.name and (include_disabled or n.enabled)]
        return out

    def get(self, name: str | None) -> Node:
        """Unknown name falls back to local: a stale config never breaks boot."""
        if not name:
            return self._nodes[LOCAL.name]
        return self._nodes.get(name, self._nodes[LOCAL.name])

    def add(self, node: Node) -> None:
        self._nodes[node.name] = node

    def remote(self) -> list[Node]:
        return [n for n in self.all() if not n.is_local]

    def status(self) -> list[dict]:
        """One probe per node for the HUD's node panel. Serial, not parallel:
        with a warm ControlMaster each probe is a few ms, and node counts are
        small (single digits). Revisit if that stops being true."""
        out = []
        for n in self.all():
            t0 = time.time()
            up = n.reachable()
            out.append({**n.as_dict(), "reachable": up,
                        "latency_ms": round((time.time() - t0) * 1000, 1)})
        return out


def load_nodes(path: str | Path | None = None) -> NodeRegistry:
    """Read config/nodes.json. Missing or malformed file -> local only."""
    p = Path(path) if path is not None else default_config_path()
    if not p.exists():
        return NodeRegistry()
    try:
        data = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"[nodes] failed to load {p}: {e}; local only")
        return NodeRegistry()

    raw = data.get("nodes", data) if isinstance(data, dict) else data
    nodes: list[Node] = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            node = Node.from_dict(entry)
        except (KeyError, TypeError) as e:  # noqa: PERF203
            print(f"[nodes] skipping bad entry {entry!r}: {e}")
            continue
        if node.name == LOCAL.name:
            continue  # local is built in and not overridable
        nodes.append(node)
    return NodeRegistry(nodes)
