"""sentinelx.py — SentinelX agent visibility (and lifecycle) across the fleet.

SentinelX (https://sentinelx.app) gives an MCP client — Claude, ChatGPT, any MCP
client — an allowlisted, auditable shell on a host, over a single OUTBOUND
WebSocket to its hub (`mcp.sentinelx.app`). No inbound port. aion's job here is
the same as for the rest of the fleet: make the agents *visible* on one surface
(which hosts have one, is it enrolled, is the unit up, how many commands its
allowlist carries, when did it last connect) and let the cockpit stop/start it.

Design (per aion-extend-backend): collectors are PURE and all I/O sits behind an
injectable ``transport(method, target, cmd) -> (rc, out)`` callable, so the parse
logic unit-tests against canned blocks and never touches the network. One SSH
round trip per host, one probe script; every host soft-fails to a row, because a
dead box must never blank the panel.

Two deliberate omissions:

* **The enrollment token is never read.** ``/etc/sentinelx/identity.json`` is
  0600 root:sentinelx; the HUD only needs to know it exists. For a host without
  it we show the enrollment URL (a token, not a credential, is what comes back
  from that dashboard page — and pasting it is the operator's step, not ours).
* **No sudo password plumbing.** Lifecycle control runs ``sudo -n systemctl``,
  which works only if the operator has granted *this* user a scoped NOPASSWD
  rule for that one unit (see ``grant_hint()``). Without the grant the action
  fails cleanly and says so, instead of aion ever handling a password.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

Transport = Callable[[str, str, str], tuple[int, str]]

HUB = "https://mcp.sentinelx.app"
CONNECTOR = f"{HUB}/mcp/mcp"
UNIT = "sentinelx-cloud-core"
INSTALL_DIR = "/opt/sentinelx-cloud-core"
ETC_DIR = "/etc/sentinelx"
IDENTITY = f"{ETC_DIR}/identity.json"
CONFIG = f"{ETC_DIR}/config.yaml"
HOST_ID_FILE = f"{ETC_DIR}/host_id"

# The host a command means when it omits one. The hub's FREE plan covers a
# single machine, so pansa (this cockpit's own box) is the default; point
# AION_SENTINELX_HOST elsewhere when the account covers more than one.
DEFAULT_HOST = "pansa"


def default_host() -> str:
    """Default target host, overridable with AION_SENTINELX_HOST."""
    import os
    return os.environ.get("AION_SENTINELX_HOST", "").strip() or DEFAULT_HOST

# Fallback node table. The real one comes from randomesh CONFIG.md → fleet.json
# (single source of truth, same rule as meshmon): a node added there appears in
# this panel without touching aion.
NODES: dict[str, str] = {
    "air": "air-ts",
    "pi": "pi-ts",
    "feather": "feather-ts",
    "omo": "omo-ts",
    "pansa": "pansa-ts",
}


def _load_hosts() -> dict[str, str]:
    """name -> ssh alias, from fleet.json when present, else the built-ins."""
    import json
    import os

    path = os.environ.get("AION_FLEET_CONFIG", "") or os.path.expanduser(
        "~/dev/randomesh/fleet.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        hosts = dict(NODES)
        for n in data.get("nodes", []):
            name = n.get("name")
            if name:
                hosts[name] = n.get("tailscale") or f"{name}-ts"
        return hosts
    except Exception:
        return dict(NODES)      # absent/unreadable config is not an error here


# One probe script, one SSH. Every line is `key=value`; anything a host cannot
# answer (journal access without the systemd-journal group, an unreadable
# sudoers dir) comes back empty and is reported as unknown rather than "broken".
PROBE = "\n".join([
    f"U={UNIT}",
    'echo "unit=$(systemctl is-active $U 2>/dev/null)"',
    'echo "enabled=$(systemctl is-enabled $U 2>/dev/null)"',
    'echo "since=$(systemctl show -p ActiveEnterTimestamp --value $U 2>/dev/null)"',
    'echo "restarts=$(systemctl show -p NRestarts --value $U 2>/dev/null)"',
    f'echo "installed=$([ -d {INSTALL_DIR} ] && echo 1 || echo 0)"',
    f'echo "host_id=$(cat {HOST_ID_FILE} 2>/dev/null)"',
    f'echo "identity=$([ -f {IDENTITY} ] && echo 1 || echo 0)"',
    'echo "sudoers=$([ -f /etc/sudoers.d/sentinelx ] && echo 1 || echo 0)"',
    # allowed_commands: is a YAML list; counting every non-comment line of the
    # file (915 of them) would report a number that means nothing. Count the
    # list items themselves: the number of commands the agent may actually run.
    f"""echo "cmds=$(awk '/^allowed_commands:/{{f=1;next}} f&&/^[a-zA-Z_]/{{f=0}} f&&/^[[:space:]]*-[[:space:]]/{{n++}} END{{print n+0}}' {CONFIG} 2>/dev/null)" """,
    f"""echo "hub=$(timeout 6 curl -s -o /dev/null -w '%{{http_code}}' {CONNECTOR} 2>/dev/null)" """,
    """echo "conn=$(journalctl -u $U -n 300 --no-pager 2>/dev/null | grep -o 'connected; session=[A-Za-z0-9_]*' | tail -1)" """,
    # Agent process age. Unprivileged and exact: the unit is a system unit, so
    # `systemctl show` is readable and /proc/<pid> stat is world-readable.
    'echo "up=$(ps -o etimes= -p $(systemctl show -p MainPID --value $U) 2>/dev/null | tr -d \' \')"',
    'echo "sudo=$(sudo -n true 2>/dev/null && echo 1 || echo 0)"',
])

# States, healthy-first for rendering: the agent you can use sorts above the one
# that needs a browser. "unenrolled" is separate from "stopped" on purpose — one
# needs a visit to the hub dashboard, the other needs a verb.
STATES = ("live", "stopped", "unenrolled", "absent", "down")
_STATE_RANK = {s: i for i, s in enumerate(STATES)}


def enroll_url(host_id: str) -> str:
    """Dashboard URL that mints an enrollment token for this host_id."""
    if not host_id:
        return ""
    return f"{HUB}/auth/dashboard/enroll?host_id={host_id}"


def span(seconds: int) -> str:
    """Compact duration for a HUD row ("45s" / "19m" / "6h" / "3d")."""
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def grant_hint(user: str = "gio", alias: str = "<host>") -> str:
    """The exact, scoped privilege rule that makes HUD control work on a host.

    Deliberately one unit, four verbs: the cockpit needs start/stop/restart and
    a read of is-active, and nothing else on the box becomes root-reachable.
    sudoers syntax needs each command spec COMMA-separated — joining them with
    spaces yields a rule that parses but matches nothing, so the specs are built
    as one comma-joined string here and covered by a test.
    """
    specs = ", ".join(f"/usr/bin/systemctl {v} {UNIT}"
                      for v in ("start", "stop", "restart", "is-active"))
    rule = f"{user} ALL=(root) NOPASSWD: {specs}"
    return (f"ssh {alias} \"echo '{rule}' "
            f"| sudo tee /etc/sudoers.d/{user}-sentinelx >/dev/null "
            f"&& sudo visudo -c\"")


@dataclass
class SentinelHost:
    name: str = ""
    host: str = ""            # ssh / tailscale alias
    reachable: bool = False
    installed: bool = False
    enrolled: bool = False
    unit: str = ""            # systemctl is-active output
    unit_enabled: str = ""
    host_id: str = ""
    cmd_count: int = 0        # commands the agent's allowlist permits
    conn_session: str = ""    # last "connected; session=…" from the journal
    hub_code: str = ""
    sudoers: bool = False     # agent has the passwordless-sudo rule
    can_control: bool = False # THIS user can drive systemctl (scoped grant)
    since: str = ""
    restarts: int = 0
    up_s: int = 0            # agent process age (seconds); 0 when the unit is down
    is_default: bool = False # the host a bare `sentinelx <verb>` means
    note: str = ""

    @property
    def state(self) -> str:
        if not self.reachable:
            return "down"
        if not self.installed:
            return "absent"
        if not self.enrolled:
            return "unenrolled"
        return "live" if self.unit == "active" else "stopped"

    @property
    def hub_ok(self) -> bool:
        """The node can reach the hub (401 = reachable, just unauthenticated)."""
        return self.hub_code in ("200", "401", "403")

    @property
    def enroll_url(self) -> str:
        return enroll_url(self.host_id)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state
        d["hub_ok"] = self.hub_ok
        d["enroll_url"] = self.enroll_url
        return d


def _int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _flag(value: str) -> bool:
    return str(value).strip() == "1"


# Keys a real probe block must contain at least one of. A non-zero exit code
# alone is not evidence of a dead host (the last probe line can fail on its
# own), but a non-zero exit with NO recognizable key means we never got data.
_PROBE_KEYS = ("unit", "installed", "identity", "host_id", "cmds", "conn", "hub", "up")


def _probe_keys(block: str) -> set[str]:
    keys: set[str] = set()
    for line in (block or "").splitlines():
        key, sep, _ = line.partition("=")
        if sep and key.strip() in _PROBE_KEYS:
            keys.add(key.strip())
    return keys


def parse_probe(block: str, name: str, alias: str) -> SentinelHost:
    """Parse one probe block into a SentinelHost. Pure; never raises."""
    fields: dict[str, str] = {}
    for line in (block or "").splitlines():
        key, sep, val = line.partition("=")
        if sep:
            fields[key.strip()] = val.strip()
    h = SentinelHost(
        name=name,
        host=alias,
        installed=_flag(fields.get("installed", "")),
        enrolled=_flag(fields.get("identity", "")),
        unit=fields.get("unit", ""),
        unit_enabled=fields.get("enabled", ""),
        host_id=fields.get("host_id", ""),
        cmd_count=_int(fields.get("cmds", "")),
        hub_code=fields.get("hub", ""),
        sudoers=_flag(fields.get("sudoers", "")),
        can_control=_flag(fields.get("sudo", "")),
        since=fields.get("since", ""),
        restarts=_int(fields.get("restarts", "")),
        up_s=_int(fields.get("up", "")),
        reachable=bool(_probe_keys(block)),
    )
    conn = fields.get("conn", "")
    h.conn_session = conn.split("session=", 1)[-1] if "session=" in conn else ""
    h.note = "reachable" if h.installed else "not installed"
    return h


def probe_host(name: str, alias: str, transport: Optional[Transport] = None) -> SentinelHost:
    """Probe one host. Unreachable/errored hosts return state 'down', not an exception."""
    if transport is None:
        transport = _ssh_transport
    try:
        rc, out = transport("ssh", alias, PROBE)
    except Exception as e:                     # timeout, DNS, ssh hiccup
        return SentinelHost(name=name, host=alias, reachable=False,
                            note=f"{type(e).__name__}: {str(e)[:60]}")
    if rc != 0 and not _probe_keys(out):
        return SentinelHost(name=name, host=alias, reachable=False,
                            note=(out or "unreachable").strip().replace("\n", " ")[:80])
    h = parse_probe(out, name, alias)
    if not h.reachable:
        h.note = "empty probe"
    return h


def snapshot(transport: Optional[Transport] = None,
             hosts: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Probe every node. Never raises; healthy hosts sort above broken ones.

    Probes run CONCURRENTLY: a HUD refresh should wait for the slowest node, not
    for the sum of them, and on a fleet where two boxes are down that difference
    is the whole refresh budget (2 x ssh timeout, every cycle).
    """
    table = _load_hosts() if hosts is None else hosts
    rows: list[SentinelHost] = []

    def one(name: str, alias: str) -> SentinelHost:
        try:
            return probe_host(name, alias, transport)
        except Exception as e:                 # belt and braces: row, not crash
            return SentinelHost(name=name, host=alias, reachable=False,
                                note=f"{type(e).__name__}: {str(e)[:60]}")

    items = list((table or {}).items())
    if not items:
        rows = []
    elif len(items) == 1:
        rows = [one(*items[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
            rows = list(pool.map(lambda kv: one(*kv), items))
    dflt = default_host()
    for h in rows:
        h.is_default = h.name == dflt
    # Default host first inside its state group, then alphabetical: the machine
    # a bare verb acts on should be the one your eye lands on.
    rows.sort(key=lambda h: (_STATE_RANK.get(h.state, 9),
                             not h.is_default, h.name))
    return {
        "hosts": [h.as_dict() for h in rows],
        "total": len(rows),
        "live": sum(1 for h in rows if h.state == "live"),
        "unenrolled": sum(1 for h in rows if h.state == "unenrolled"),
        "hub": HUB,
        "connector": CONNECTOR,
        "unit": UNIT,
        "default": dflt,
    }


def control(name: str = "", action: str = "", transport: Optional[Transport] = None,
            hosts: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """start | stop | restart the SentinelX agent on one host.

    Returns a result dict, never raises. ``--no-password`` (``sudo -n``) is
    intentional: a cockpit must not be able to prompt for a password, and a
    missing grant has to be visible in the output rather than silently ignored.
    """
    name = name or default_host()      # bare verb -> the default machine
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "name": name, "action": action,
                "error": f"unknown action {action!r} (start|stop|restart)"}
    table = _load_hosts() if hosts is None else hosts
    alias = (table or {}).get(name)
    if not alias:
        return {"ok": False, "name": name, "action": action,
                "error": f"unknown host {name!r}"}
    if transport is None:
        transport = _ssh_transport
    cmd = f"sudo -n systemctl {action} {UNIT}"
    try:
        rc, out = transport("ssh", alias, cmd)
    except Exception as e:
        return {"ok": False, "name": name, "action": action, "host": alias,
                "cmd": cmd, "error": f"{type(e).__name__}: {str(e)[:80]}"}
    text = (out or "").strip()
    res: dict[str, Any] = {"ok": rc == 0, "name": name, "action": action,
                           "host": alias, "cmd": cmd, "rc": rc,
                           "out": text[-200:]}
    if rc != 0:
        low = text.lower()
        if "password is required" in low or "a password" in low or "interactive authentication" in low:
            res["error"] = "needs root: no scoped sudoers grant for this user"
            res["hint"] = grant_hint(alias=alias)
        else:
            res["error"] = f"rc={rc}"
    return res


def _ssh_transport(method: str, target: str, cmd: str) -> tuple[int, str]:
    """Real transport. Imported lazily so unit tests never shell out."""
    import subprocess

    if method != "ssh":
        raise ValueError(f"unsupported transport method {method}")
    # Same budget as meshmon: a dead node must cost one timeout, not a stall
    # in the refresh cycle that also carries the reachable ones.
    p = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
         "-o", "ServerAliveInterval=15", target, cmd],
        capture_output=True, text=True, timeout=15,
    )
    return p.returncode, p.stdout + p.stderr
