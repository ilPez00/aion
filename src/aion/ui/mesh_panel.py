"""mesh_panel.py — render the RandoMesh workspace (read-only monitor).

Pure rendering: takes the already-collected `mesh` snapshot (from meshmon)
and returns Rich markup. No network, no filesystem — called every render
tick, so it must never block.

Phase 1 = visibility only: node vital signs + pansa storage. Control
(restart/deploy) arrives in a later phase as Intents; this panel shows state.
"""

from __future__ import annotations

from typing import Any

WIDTH = 54


def _glyph(reachable: bool, load1: float) -> str:
    if not reachable:
        return "○"  # down
    if load1 > 2.0:
        return "◆"  # hot
    if load1 > 0.5:
        return "●"  # live, working
    return "·"  # idle


def _bar(pct: int, theme: dict) -> str:
    filled = max(0, min(10, round(pct / 10)))
    color = theme.get("ok", "#7CFFB2") if pct < 85 else (
        theme.get("warn", "#FFD479") if pct < 95 else theme.get("err", "#FF8A8A"))
    return f"[{color}]{'█' * filled}{'░' * (10 - filled)}[{theme.get('faint', '#6b7d8d')}][/]"


def render_mesh(data: dict[str, Any], theme: dict) -> str:
    nodes = data.get("nodes", [])
    total = data.get("total", len(nodes))
    reachable = data.get("reachable", 0)
    out: list[str] = []

    title = f"[b {theme.get('accent', '#5ad1ff')}]⏣ RandoMesh[/]  " \
            f"[{theme.get('dim', '#9aabbb')}]{reachable}/{total} nodes up[/]"
    out.append(title)
    out.append("")

    if not nodes:
        out.append(f"[{theme.get('faint', '#6b7d8d')}]no mesh data[/]")
        return "\n".join(out)

    # Sort: reachable first, then by load descending (hot nodes to the top).
    order = sorted(nodes, key=lambda n: (not n.get("reachable", False), -n.get("load1", 0.0)))
    for n in order:
        name = n.get("name", "?")
        role = n.get("role", "")
        g = _glyph(n.get("reachable", False), n.get("load1", 0.0))
        if not n.get("reachable", False):
            out.append(f"  [{theme.get('faint', '#6b7d8d')}]{g} {name}  [{theme.get('err', '#FF8A8A')}]DOWN[/] — {n.get('note', '')}[/]")
            continue
        ram = n.get("ram_pct", 0)
        disk = n.get("disk_pct", 0)
        load = n.get("load1", 0.0)
        line = (f"  {g} [{theme.get('fg', '#dbe6f0')}]{name}[/] "
                f"[{theme.get('dim', '#9aabbb')}]{role}[/]\n"
                f"     load {load:.2f}  ram {_bar(ram, theme)} {ram}%  "
                f"disk {_bar(disk, theme)} {disk}%")
        out.append(line)

    # pansa storage block (folded in from nas backend)
    storage = data.get("storage") or {}
    if storage.get("reachable"):
        out.append("")
        out.append(f"[{theme.get('dim', '#9aabbb')}]storage (pansa):[/]")
        for sh in storage.get("shares", []):
            used = sh.get("used_pct", 0)
            out.append(f"  {sh.get('name', '?')} {_bar(used, theme)} {sh.get('used_gb', 0)}/{sh.get('total_gb', 0)}G ({used}%)")

    return "\n".join(out)
