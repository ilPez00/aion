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
import tempfile
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


# ── Settings ─────────────────────────────────────────────────────────────────
# Precedence: environment > config/layout.json "fleet" block > these defaults.
# The environment wins because it is the per-launch, most explicit signal:
#   AION_INSTANCE=hud AION_LISTEN=lan ./aion.sh
@dataclass
class FleetSettings:
    instance: str = DEFAULT_INSTANCE
    listen: str = "local"           # "local" (loopback) or "lan" (0.0.0.0)
    heartbeat_s: float = 5.0
    local_stale_s: float = 15.0
    local_offline_s: float = 30.0
    remote_stale_s: float = 20.0
    remote_offline_s: float = 60.0

    # Editable from Settings; the first two only take effect on restart because
    # they decide the state root and the listening socket at boot.
    RESTART_KEYS = ("instance", "listen")
    FIELDS = ("instance", "listen", "heartbeat_s", "local_stale_s",
              "local_offline_s", "remote_stale_s", "remote_offline_s")

    def normalised(self) -> "FleetSettings":
        """Clamp to values that cannot produce a nonsensical panel.

        A stale threshold above the offline one would make "stale" unreachable
        and nodes would jump straight from live to offline, so ordering is
        enforced rather than trusted.
        """
        inst = "".join(c for c in self.instance.strip() if c.isalnum() or c in "-_")
        listen = self.listen.strip().lower()
        heartbeat = max(1.0, float(self.heartbeat_s))
        l_stale = max(heartbeat, float(self.local_stale_s))
        r_stale = max(1.0, float(self.remote_stale_s))
        return FleetSettings(
            instance=inst or DEFAULT_INSTANCE,
            listen="lan" if listen == "lan" else "local",
            heartbeat_s=heartbeat,
            local_stale_s=l_stale,
            local_offline_s=max(l_stale + 1.0, float(self.local_offline_s)),
            remote_stale_s=r_stale,
            remote_offline_s=max(r_stale + 1.0, float(self.remote_offline_s)),
        )

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}


_settings = FleetSettings()


def configure(block: dict | None) -> FleetSettings:
    """Apply the config/layout.json "fleet" block. Call once, early in boot."""
    global _settings
    known = {k: v for k, v in (block or {}).items() if k in FleetSettings.FIELDS}
    _settings = FleetSettings(**{**FleetSettings().as_dict(), **known}).normalised()
    return _settings


def settings() -> FleetSettings:
    return _settings


def describe_settings() -> list[dict]:
    """Current effective values plus where each one came from — the Settings
    workspace shows the source so an env override is never a mystery."""
    s = settings()
    env_backed = {"instance": "AION_INSTANCE", "listen": "AION_LISTEN"}
    out = []
    for key in FleetSettings.FIELDS:
        env_var = env_backed.get(key)
        from_env = bool(env_var and os.environ.get(env_var, "").strip())
        value = {"instance": instance_id(),
                 "listen": "lan" if listen_host() == "0.0.0.0" else "local"}.get(
            key, getattr(s, key))
        out.append({
            "key": key,
            "value": value,
            "source": f"env {env_var}" if from_env else "config",
            "restart": key in FleetSettings.RESTART_KEYS,
        })
    return out


# ── Identity & paths ─────────────────────────────────────────────────────────
def instance_id() -> str:
    """This process's instance name. `AION_INSTANCE=hud ./aion.sh` to split."""
    raw = os.environ.get("AION_INSTANCE", "").strip()
    # keep it filesystem-safe: it becomes a directory name
    cleaned = "".join(c for c in raw if c.isalnum() or c in "-_")
    return cleaned or settings().instance


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

    The temp file is unique per call (mkstemp), not per pid: several harnesses
    checkpoint from their own threads at once, and a shared `.tmp<pid>` name
    let one call's cleanup unlink the file another was about to os.replace,
    which surfaced as "session.json.tmp... -> session.json (No such file)".
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # ensure_ascii=False, because these files are read and edited by
            # people. The default escapes every non-ASCII character, so saving
            # one setting rewrote all of config/layout.json turning the
            # workspace icons into ⬡ — and did the same to any accented
            # word in a todo or a memory fact. Encoding is pinned rather than
            # left to the locale, since that is what makes it safe to write
            # non-ASCII at all.
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)      # atomic; tmp no longer exists after this
    finally:
        try:
            tmp.unlink()           # only runs if os.replace didn't happen
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
    env = os.environ.get("AION_LISTEN", "").strip().lower()
    if env:
        return "0.0.0.0" if env == "lan" else "127.0.0.1"
    return "0.0.0.0" if settings().listen == "lan" else "127.0.0.1"


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


# Defaults live on FleetSettings; these names are kept for callers that want
# the shipped values rather than the configured ones.
#
# Local peers heartbeat every heartbeat_s (5s) via an atomic file write -- there
# is almost nothing to fail, so three missed beats already means something is
# wrong and six means it is gone. Remote peers are polled over HTTP, where the
# network is the unreliable part rather than the node, so a dropped packet must
# not paint the panel red: those thresholds are deliberately more patient.
LOCAL_STALE_S = FleetSettings.local_stale_s
LOCAL_OFFLINE_S = FleetSettings.local_offline_s
REMOTE_STALE_S = FleetSettings.remote_stale_s
REMOTE_OFFLINE_S = FleetSettings.remote_offline_s


def node_health(ever_seen: bool, age_s: float, local: bool = False,
                stale_after: float | None = None,
                offline_after: float | None = None) -> str:
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
    s = settings()
    if stale_after is None:
        stale_after = s.local_stale_s if local else s.remote_stale_s
    if offline_after is None:
        offline_after = s.local_offline_s if local else s.remote_offline_s
    if age_s >= offline_after:
        return HEALTH_OFFLINE
    if age_s >= stale_after:
        return HEALTH_STALE
    return HEALTH_LIVE
