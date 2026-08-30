"""fleet_panel.py — render the Fleet workspace.

Pure rendering: takes already-collected rows and returns Rich markup. No
filesystem, no network. `_center_line` calls the panel on every render tick,
so anything that blocks here blocks the whole cockpit.

Design notes:
  - Four health states, not two. "Never contacted" and "died" look different
    because they need different reactions from you.
  - No box frame. Every other panel in aion is wrapped in a double-line box;
    the fleet reads as a bank of vital signs instead, so nodes doing work
    visibly pulse and idle ones sit flat.
  - Sort order is information: local before remote, healthy before sick.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..fleet import (
    HEALTH_LIVE, HEALTH_OFFLINE, HEALTH_STALE, HEALTH_UNKNOWN,
)
from .gauges import sparkline

WIDTH = 54

# Ranks health worst-first so the node needing attention sorts to the top of
# its group. LIVE last: a healthy node is the one you do not need to look at.
_HEALTH_RANK = {
    HEALTH_OFFLINE: 0,
    HEALTH_STALE: 1,
    HEALTH_UNKNOWN: 2,
    HEALTH_LIVE: 3,
}

_GLYPH = {
    HEALTH_LIVE: "●",
    HEALTH_STALE: "◐",
    HEALTH_OFFLINE: "○",
    HEALTH_UNKNOWN: "·",
}

# One rotation of the pulse, shown only on nodes with running work.
_PULSE = "▁▃▅▇▅▃"


@dataclass
class FleetRow:
    """One instance in the fleet, local or remote."""
    id: str
    addr: str                  # host:port
    health: str                # one of fleet.HEALTH_*
    local: bool = False
    is_self: bool = False
    running: int = 0
    harness: str = ""
    age_s: float = 0.0
    history: list[float] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    pair: str = ""


def _age_label(seconds: float) -> str:
    """Compact age. Never shows the 9999 sentinel as a duration."""
    if seconds >= 9000:
        return "never"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"


def _health_color(health: str, theme: dict) -> str:
    return {
        HEALTH_LIVE: theme["ok"],
        HEALTH_STALE: theme["warn"],
        HEALTH_OFFLINE: theme["err"],
        HEALTH_UNKNOWN: theme["dim"],
    }.get(health, theme["dim"])


def _status_label(row: FleetRow) -> str:
    if row.health == HEALTH_UNKNOWN:
        return "never seen"
    if row.health == HEALTH_OFFLINE:
        return f"offline {_age_label(row.age_s)}"
    if row.health == HEALTH_STALE:
        return f"lagging {_age_label(row.age_s)}"
    if row.running:
        return f"{row.running} running"
    return "idle"


def _sort_key(row: FleetRow) -> tuple:
    # self first (you can kill it), then local, then worst-health first
    return (not row.is_self, not row.local, _HEALTH_RANK.get(row.health, 9), row.id)


def _node_lines(row: FleetRow, theme: dict, tick: int) -> list[str]:
    a, di = theme["accent"], theme["dim"]
    hc = _health_color(row.health, theme)
    dead = row.health in (HEALTH_OFFLINE, HEALTH_UNKNOWN)

    # identity line: glyph · name · address · status
    name_color = a if not dead else di
    name = f"{row.id[:14]:14s}"
    status = _status_label(row)
    status_color = theme["warn"] if row.running and not dead else hc
    pad = max(1, WIDTH - 4 - 14 - len(row.addr) - len(status))
    lines = [
        f" [{hc}]{_GLYPH.get(row.health, '·')}[/] [{name_color}]{name}[/]"
        f"[{di}]{row.addr}[/]{' ' * pad}[{status_color}]{status}[/]"
    ]

    # vitals line: sparkline of recent task load + active harness + pulse
    if dead:
        return lines
    spark = sparkline(row.history[-16:], width=16) if row.history else "─" * 16
    harness = f" {row.harness[:12]}" if row.harness else ""
    pulse = ""
    if row.running:
        pulse = f"  [{theme['warn']}]{_PULSE[tick % len(_PULSE)]}[/]"
    lines.append(f"   [{hc}]{spark}[/] [{di}]{harness}[/]{pulse}")
    return lines


def render_fleet(rows: list[FleetRow], theme: dict, tick: int = 0,
                 listen_port: int = 8765, listen_host: str = "127.0.0.1") -> str:
    """Render the whole Fleet workspace to Rich markup."""
    a, di, ok_ = theme["accent"], theme["dim"], theme["ok"]
    rows = sorted(rows, key=_sort_key)
    live = sum(1 for r in rows if r.health == HEALTH_LIVE)

    summary = f"{len(rows)} node{'s' if len(rows) != 1 else ''} · {live} live"
    head_pad = max(1, WIDTH - len("FLEET") - len(summary))
    out = [
        f"[{a}]FLEET[/]{' ' * head_pad}[{di}]{summary}[/]",
        f"[{a}]{'━' * WIDTH}[/]",
    ]

    if not rows:
        out.append(f" [{di}]No instances found. This one is not advertising yet.[/]")
        out.append(f" [{di}]Start another: AION_INSTANCE=hud ./aion.sh[/]")
        return "\n".join(out)

    group = None
    pair_group = None
    for row in rows:
        label = "THIS NODE" if row.is_self else ("LOCAL" if row.local else "REMOTE")
        pair_label = f"PAIRED: {row.pair}" if row.pair else ""
        if pair_label != pair_group:
            if pair_group is not None:
                out.append(f"[{di}]{'·' * WIDTH}[/]")
            if pair_label:
                out.append(f"[{di}]{pair_label}[/]")
            pair_group = pair_label
        if label != group:
            if group is not None:
                out.append(f"[{di}]{'·' * WIDTH}[/]")
            out.append(f"[{di}]{label}[/]")
            group = label
        out.extend(_node_lines(row, theme, tick))

    # Exposure is worth a glance: :8765 alone does not say who can reach you.
    lan = listen_host == "0.0.0.0"
    reach = "LAN" if lan else "this machine only"
    reach_color = theme["warn"] if lan else theme["dim"]
    out.append(f"[{a}]{'━' * WIDTH}[/]")
    out.append(f"[{di}]listening :{listen_port} ·[/] [{reach_color}]{reach}[/]"
               f"[{di}] · Ctrl-K 'remote run <node> <prompt>'[/]")
    return "\n".join(out)
