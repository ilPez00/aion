"""fleet.py — instance identity, state roots, and local peer discovery.

One machine can run several aion cockpits at once (a full-screen one, a
half-screen HUD, a headless web instance). Before this module they all wrote
to the same ~/.aion/*.json and fought over port 8765; last writer won.

The model is hybrid:

    ~/.aion/
      shared/                 todos.md, memory.json, boards.json, agents.json,
      |                       vault/ -- your data, one copy, every instance sees it
      instances/
        <id>/
          session.json        tasks belong to the process that spawned them
          meta.json           heartbeat: pid, port, host, timestamps

`meta.json` doubles as same-host discovery: an instance advertises itself by
writing one, and finds its neighbours by reading the others. No daemon, no
mDNS, no port scan.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

AION_HOME = Path.home() / ".aion"
BASE_PORT = 8765
DEFAULT_INSTANCE = "main"

# A peer that has not refreshed its heartbeat in this long is not trustworthy,
# even if its meta.json is still on disk (SIGKILL leaves no chance to clean up).
HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_STALE_S = 15.0


# ── Identity & paths ─────────────────────────────────────────────────────────
def instance_id() -> str:
    """This process's instance name. `AION_INSTANCE=hud ./aion.sh` to split."""
    raw = os.environ.get("AION_INSTANCE", "").strip()
    # keep it filesystem-safe: it becomes a directory name
    cleaned = "".join(c for c in raw if c.isalnum() or c in "-_")
    return cleaned or DEFAULT_INSTANCE


def shared_root() -> Path:
    """User data every instance reads and writes: todos, memory, vault."""
    p = AION_HOME / "shared"
    p.mkdir(parents=True, exist_ok=True)
    return p


def instance_root(iid: str | None = None) -> Path:
    """Per-instance private state: tasks, heartbeat."""
    p = AION_HOME / "instances" / (iid or instance_id())
    p.mkdir(parents=True, exist_ok=True)
    return p


def shared_path(name: str) -> Path:
    """A file every instance shares: todos, memory, boards, agents, vault."""
    return shared_root() / name


def instance_path(name: str, iid: str | None = None) -> Path:
    """A file private to one instance: tasks, heartbeat."""
    return instance_root(iid) / name


# Files that lived flat in ~/.aion before instances existed. Everything here is
# user data, so it moves to shared/ -- except the task registry, which belongs
# to whichever process spawned those tasks.
_LEGACY_SHARED = (
    "todos.md", "memory.json", "boards.json", "agents.json",
    "health.json", "vault_setup_done", "vault",
)
_LEGACY_INSTANCE = ("session.json",)


def migrate_legacy() -> list[str]:
    """Move pre-fleet state into the shared/ + instances/ layout.

    Idempotent, and never clobbers: if the destination already exists the
    legacy file is left alone rather than overwritten, so a half-finished
    migration cannot eat data on the next boot. Returns what moved.
    """
    moved: list[str] = []
    plan = [(n, shared_root() / n) for n in _LEGACY_SHARED]
    plan += [(n, instance_root(DEFAULT_INSTANCE) / n) for n in _LEGACY_INSTANCE]
    for name, dest in plan:
        src = AION_HOME / name
        if not src.exists() or dest.exists():
            continue
        try:
            src.rename(dest)
            moved.append(name)
        except OSError:
            continue    # cross-device or permissions: leave the original put
    return moved


def alloc_port(iid: str | None = None) -> int:
    """Deterministic RemoteServer port, so a peer's port is derivable from its
    name alone. The default instance keeps 8765 so existing configs still work.
    """
    iid = iid or instance_id()
    if iid == DEFAULT_INSTANCE:
        return BASE_PORT
    return BASE_PORT + (zlib.crc32(iid.encode()) % 100)


# ── Atomic writes ────────────────────────────────────────────────────────────
def write_json_atomic(path: str | Path, data: object) -> None:
    """Write JSON so a reader never sees a half-file.

    `SessionStore.save` claimed crash-safety but used a bare `write_text`,
    which truncates the target before writing the new bytes. Kill the process
    in that window and the file is empty. tmp + os.replace makes the swap
    atomic on POSIX, which also makes concurrent instances safe to interleave.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


# ── Shared secret ────────────────────────────────────────────────────────────
TOKEN_HEADER = "x-aion-token"


def token_path() -> Path:
    return AION_HOME / "token"


def load_or_create_token() -> str:
    """The shared secret guarding /run, /cancel and /status.

    /run executes arbitrary commands as you, so an unauthenticated listener on
    a LAN is remote code execution for anyone on the network. Generated once,
    stored 0600. To control a second machine, copy this file to it -- the
    fleet trusts one secret, not per-node keys.
    """
    p = token_path()
    try:
        existing = p.read_text().strip()
        if existing:
            return existing
    except (OSError, FileNotFoundError):
        pass
    token = secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    # write with the right mode from the start -- never leave a readable window
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    return token


def listen_host() -> str:
    """Loopback unless AION_LISTEN=lan is set explicitly.

    Binding 0.0.0.0 by default meant every aion ever started was reachable
    from the whole network. Exposure is now a decision, not an accident.
    """
    return "0.0.0.0" if os.environ.get("AION_LISTEN", "").lower() == "lan" else "127.0.0.1"


# ── Peers ────────────────────────────────────────────────────────────────────
@dataclass
class LocalPeer:
    """Another aion instance on this same machine, read from its meta.json."""
    id: str
    pid: int
    port: int
    started_at: float = 0.0
    updated_at: float = 0.0
    active_harness: str = ""
    running_count: int = 0
    is_self: bool = False

    @property
    def host(self) -> str:
        return "127.0.0.1"

    def age_s(self) -> float:
        return time.time() - self.updated_at if self.updated_at else 9999.0


def _pid_alive(pid: int) -> bool:
    """Signal 0 tests for existence without delivering anything."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists but owned by another user -- still alive
        return True


class Heartbeat:
    """Advertises this instance to same-host peers via meta.json."""

    def __init__(self, iid: str | None = None, port: int | None = None) -> None:
        self.id = iid or instance_id()
        self.port = port or alloc_port(self.id)
        self.path = instance_root(self.id) / "meta.json"
        self.started_at = time.time()

    def beat(self, active_harness: str = "", running_count: int = 0) -> None:
        write_json_atomic(self.path, {
            "id": self.id,
            "pid": os.getpid(),
            "port": self.port,
            "hostname": socket.gethostname(),
            "started_at": self.started_at,
            "updated_at": time.time(),
            "active_harness": active_harness,
            "running_count": running_count,
        })

    def clear(self) -> None:
        """Remove our advertisement on clean shutdown."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def discover_local(include_self: bool = True) -> list[LocalPeer]:
    """Every aion instance on this machine, newest heartbeat first.

    Prunes meta.json files whose pid is gone -- a SIGKILLed instance cannot
    clean up after itself, so whoever notices does it.
    """
    root = AION_HOME / "instances"
    if not root.exists():
        return []
    me = instance_id()
    peers: list[LocalPeer] = []
    for d in sorted(root.iterdir()):
        meta = d / "meta.json"
        if not d.is_dir() or not meta.exists():
            continue
        try:
            raw = json.loads(meta.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pid = int(raw.get("pid", 0))
        if pid and not _pid_alive(pid):
            try:
                meta.unlink()
            except OSError:
                pass
            continue
        peer = LocalPeer(
            id=raw.get("id", d.name),
            pid=pid,
            port=int(raw.get("port", BASE_PORT)),
            started_at=float(raw.get("started_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            active_harness=raw.get("active_harness", ""),
            running_count=int(raw.get("running_count", 0)),
            is_self=(raw.get("id", d.name) == me),
        )
        if peer.is_self and not include_self:
            continue
        peers.append(peer)
    peers.sort(key=lambda p: p.age_s())
    return peers


# ── Health ───────────────────────────────────────────────────────────────────
# The Fleet panel colours and sorts every node by the string this returns, so
# it is the one function that decides what the whole workspace looks like.
HEALTH_LIVE = "live"        # answering promptly            -> theme["ok"]
HEALTH_STALE = "stale"      # answering, but lagging        -> theme["warn"]
HEALTH_OFFLINE = "offline"  # was reachable, now is not     -> theme["err"]
HEALTH_UNKNOWN = "unknown"  # configured, never contacted   -> theme["dim"]


# Local peers heartbeat every HEARTBEAT_INTERVAL_S (5s) via an atomic file
# write -- there is almost nothing to fail, so three missed beats already means
# something is wrong and six means it is gone.
LOCAL_STALE_S = 15.0
LOCAL_OFFLINE_S = 30.0

# Remote peers are polled every ~3s over HTTP with a 5s timeout. The network is
# the unreliable part, not the node, so a dropped packet or a WiFi roam must not
# paint the panel red -- these are deliberately more patient than the local ones.
REMOTE_STALE_S = 20.0
REMOTE_OFFLINE_S = 60.0


def node_health(ever_seen: bool, age_s: float, local: bool = False) -> str:
    """Classify one node into exactly one of the four HEALTH_* constants.

    Args:
        ever_seen: has this node EVER answered? (False = freshly configured,
                   never contacted -- distinct from "it died")
        age_s:     seconds since its last successful contact / heartbeat.
                   Only meaningful when ever_seen is True.
        local:     same-machine peer (heartbeat file) rather than a remote node
                   (HTTP poll). Picks which threshold pair applies.

    Load does not buy grace: a node too busy to answer is a node you want
    flagged, which is the whole point of watching it.
    """
    if not ever_seen:
        return HEALTH_UNKNOWN
    stale_after = LOCAL_STALE_S if local else REMOTE_STALE_S
    offline_after = LOCAL_OFFLINE_S if local else REMOTE_OFFLINE_S
    if age_s >= offline_after:
        return HEALTH_OFFLINE
    if age_s >= stale_after:
        return HEALTH_STALE
    return HEALTH_LIVE
