"""containers_panel.py — render the container surface (pure, no I/O).

Follows fleet_panel.py's contract: rows in, Rich markup out, nothing here
touches a process or a socket. `_sys_panel` appends this block every tick, so
anything slow in here freezes the cockpit — that is why all state logic (the
health ranking, the colour choice) is pure and the callers only pass data.
"""
from __future__ import annotations

WIDTH = 54

# worst-first, matching the Fleet panel's "the sick node sorts to the top" rule
_HEALTH_RANK = {
    "CrashLoopBackOff": 0, "Error": 1, "ImagePullBackOff": 2,
    "Pending": 3, "exited": 3, "ContainerCreating": 4, "created": 4,
    "Running": 5, "running": 5, "Succeeded": 6, "completed": 6,
}


def state_color(state: str, theme: dict) -> str:
    s = state.lower()
    if "crash" in s or s in ("error", "imagepullbackoff"):
        return theme["err"]
    if s in ("pending", "exited", "containercreating", "created"):
        return theme["warn"]
    if s in ("running", "succeeded", "completed"):
        return theme["ok"]
    return theme["dim"]


def _age_label(seconds: float) -> str:
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f" {seconds:.0f}s"
    if seconds < 3600:
        return f" {seconds / 60:.0f}m"
    return f" {seconds / 3600:.0f}h"


def _row(row: dict, theme: dict) -> str:
    a, di = theme["accent"], theme["dim"]
    sc = state_color(row.get("state", ""), theme)
    name = f"{row.get('name', '?')[:20]:20s}"
    state = f"{row.get('state', '?')[:16]:16s}"
    image = row.get("image", "")[:24]
    backend = row.get("backend", "?")[0]  # k/d — one letter, no room for more
    pad = max(1, WIDTH - 4 - len(name) - len(state))
    return (f" [{sc}]●[/] [{a}]{name}[/][{di}]{state}[/]"
            f"{' ' * pad}[{di}]{image}[/] [{di}]{backend}[/]"
            f"{_age_label(row.get('age_s', 0.0))}")


def render_containers(rows: list[dict], theme: dict, *, errors: list[str] | None = None,
                      backends: str = "") -> str:
    """The whole container block. Empty string when there is nothing to show —
    a heading claiming 'no containers' when there is no docker is a lie."""
    a, di, er = theme["accent"], theme["dim"], theme["err"]
    rows = sorted(rows, key=lambda r: (_HEALTH_RANK.get(r.get("state", ""), 9),
                                       r.get("name", "")))
    running = sum(1 for r in rows
                  if r.get("state", "").lower() in ("running",))
    out = [f"[{a}]CONTAINERS[/] [{di}]· {len(rows)} total · {running} running"
           f" · {backends}[/]",
           f"[{a}]{'━' * WIDTH}[/]"]
    if errors:
        for e in errors[:2]:
            out.append(f" [{er}]⚠[/] [{di}]{e}[/]")
    if not rows:
        out.append(f" [{di}]no containers visible[/]")
        return "\n".join(out)
    for r in rows[:10]:  # worst-first slice; a HUD is not a pod list dump
        out.append(_row(r, theme))
    if len(rows) > 10:
        out.append(f" [{di}]… and {len(rows) - 10} more[/]")
    out.append(f"[{di}]Ctrl-K: 'containers logs <id>' · 'mirror <pod> <cmd>'[/]")
    return "\n".join(out)
