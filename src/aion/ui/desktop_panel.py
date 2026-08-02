"""desktop_panel.py — the landing HUD: status bar, dock, context widgets.

Lifted out of `AiOSApp._desktop_panel`. This is the first thing anyone sees,
it draws on every tick, and it is assembled entirely from a dashboard snapshot
plus a handful of app facts — so it takes those and returns a string instead
of reaching through `self` for eight different things.

The widget sections are all conditional on data that may not exist yet (no
projects scanned, no todos written, nothing running). A landing screen that
renders an empty PROJECTS heading is worse than one that omits it: an empty
heading claims the scan ran and found nothing.
"""
from __future__ import annotations

from .gauges import hbar
from .visualizers import pulse_radar

WIDTH = 50
UL, UR, H, V = "┌", "┐", "─", "│"

WS_ICONS = {"desktop": "⬡", "models": "◈", "tasks": "▤", "runs": "⟳",
            "agent": "✦", "vault": "📓", "system": "🖥", "term": "▣",
            "settings": "⚙", "net": "🌐"}

# Three bands, matching the gauges elsewhere in the cockpit. A number that goes
# red at 80 and the same number rendered flat somewhere else is how a dashboard
# stops being trusted.
BUSY_PCT = 80
WARM_PCT = 50


def load_colour(pct: float, theme: dict) -> str:
    if pct > BUSY_PCT:
        return theme["err"]
    if pct > WARM_PCT:
        return theme["warn"]
    return theme["ok"]


def _status_bar(data: dict, theme: dict, ctx_icon: str, ctx_label: str) -> list[str]:
    a, ok_, di = theme["accent"], theme["ok"], theme["dim"]
    cpu = data.get("cpu_pct", 0)
    ram = data.get("ram_pct", 0)
    disk = data.get("disk_pct", 0)
    return [
        f" [{a}]{UL}{H * 3} STATUS {H * 35}{UR}[/]",
        f" [{a}]{V}[/]"
        f"[{di}]CPU[/][{load_colour(cpu, theme)}] {cpu:2.0f}%[/]"
        f"  [{di}]RAM[/][{load_colour(ram, theme)}] {ram:2.0f}%[/]"
        f"  [{di}]DSK[/][{load_colour(disk, theme)}] {disk:2.0f}%[/]"
        f"  [{di}]TASKS[/] [{ok_}]●{data.get('tasks_running', 0)}[/]"
        f" [{ok_}]✓{data.get('tasks_done', 0)}[/]"
        f"  [{di}]{ctx_icon} {ctx_label}[/]"
        f"  [{a}]{V}[/]",
    ]


def _dock(workspaces: list[str], theme: dict) -> list[str]:
    a, di = theme["accent"], theme["dim"]
    rows = []
    for chunk in (workspaces[:4], workspaces[4:]):
        cells = "  ".join(f"[{a}]{WS_ICONS.get(w, '·')}[/] [{di}]{w[:6]}[/]"
                          for w in chunk)
        rows.append(f" [{a}]{V}[/]  {cells}         [{a}]{V}[/]")
    return [f" [{a}]{H * WIDTH}[/]", *rows]


def _projects(data: dict, theme: dict) -> str:
    proj = data.get("projects", [])
    if not proj:
        return ""
    a, ok_, di = theme["accent"], theme["ok"], theme["dim"]
    block = [f"[{a}]PROJECTS[/]"]
    for pr in proj[:3]:
        name = pr.get("name", pr.get("id", "?"))[:18]
        dirty, branch = pr.get("dirty", 0), pr.get("branch", "")
        badges = f"{f' ~{dirty}' if dirty else ''}{f' @{branch}' if branch else ''}"
        block.append(f" [{ok_}]⬢[/] [{di}]{name}{badges}[/]")
    return "\n".join(block)


def _todos(data: dict, theme: dict) -> str:
    todos = data.get("todos", [])
    if not todos:
        return ""
    a, wa, di = theme["accent"], theme["warn"], theme["dim"]
    block = [f"[{a}]TODOS[/]"]
    for t in todos[:3]:
        ic, cl = ("✓", di) if t["done"] else ("○", wa)
        block.append(f" [{cl}]{ic}[/] [{di}]{t['text'][:40]}[/]")
    return "\n".join(block)


def _sessions(data: dict, theme: dict) -> str:
    """Live work: tasks, AI sessions, and anything that died mid-run.

    Interrupted work is listed with the running work rather than filed
    somewhere quieter, because it is the category most likely to need a
    decision and the least likely to be looked for.
    """
    a, ok_, wa, er, di = (theme["accent"], theme["ok"], theme["warn"],
                          theme["err"], theme["dim"])
    active = [t for t in data.get("active_tasks", [])
              if t["state"] in ("running", "pending")]
    sessions = data.get("recent_sessions", [])
    interrupted = data.get("interrupted_tasks", [])
    if not (active or sessions or interrupted):
        return ""
    block = [f"[{a}]SESSIONS[/]"]
    for t in active[:2]:
        ic = "⏸" if t.get("paused") else "●"
        block.append(f" [{wa}]{ic}[/] [{di}]{t['harness'][:6]}[/] {t['label'][:18]}"
                     f" {hbar(t['progress'], 6, wa)}")
    for s in sessions[:4]:
        s_ic = {"running": "●", "ended": "✓", "zombie": "⊘"}.get(s["status"], "?")
        s_cl = {"running": wa, "ended": ok_, "zombie": er}.get(s["status"], di)
        block.append(f" [{s_cl}]{s_ic}[/] [{di}]{s.get('repo', '')[:8]:8s}[/] "
                     f"{s['model'][:16]}  [{s_cl}]{s['status']}[/]")
    for t in interrupted[:2]:
        block.append(f" [{er}]⊘[/] [{di}]{t['harness'][:6]}[/] "
                     f"{t['label'][:18]} [{er}]interrupted[/]")
    return "\n".join(block)


def _viz(theme: dict, stats: dict, running: int, tick: int) -> list[str]:
    a = theme["accent"]
    sys_ = (stats or {}).get("system") or {}
    cpu = (sys_.get("cpu") or {}).get("total_pct", 0) / 100.0
    mem = (sys_.get("mem") or {}).get("pct", 0) / 100.0
    rings = [
        {"label": "CPU", "value": cpu, "items": ["■"] * int(cpu * 12)},
        {"label": "RAM", "value": mem, "items": ["■"] * int(mem * 12)},
        {"label": "TASKS", "value": min(running / 5.0, 1.0),
         "items": ["●"] * min(running, 12)},
    ]
    out = [f" [{a}]{H * 3} VIZ {H * 40}┐[/]"]
    out += [f" [{a}]{V}[/] {line}"
            for line in pulse_radar(rings, tick, size=10).split("\n")]
    return out


def render_desktop(data: dict, theme: dict, *, workspaces: list[str],
                   ctx_icon: str = "", ctx_label: str = "",
                   workflows=None, workflows_live: bool = False,
                   stats: dict | None = None, running: int = 0,
                   tick: int = 0) -> str:
    """The landing HUD. `data` is the dashboard snapshot; nothing else is read."""
    data = data or {}
    a = theme["accent"]
    p = _status_bar(data, theme, ctx_icon, ctx_label)
    p += _dock(list(workspaces or []), theme)

    widgets = [b for b in (_projects(data, theme), _todos(data, theme),
                           _sessions(data, theme)) if b]
    if widgets:
        p.append(f" [{a}]{H * WIDTH}[/]")
        for block in widgets:
            p += [f" [{a}]{V}[/] {line}" for line in block.split("\n")]

    if workflows and workflows_live:
        from ..workflows import desktop_missions
        p.append(f" [{a}]{H * 3} MISSIONS {H * 36}┐[/]")
        p += [f" [{a}]{V}[/] {line}"
              for line in desktop_missions(workflows, theme, max_rows=3).split("\n")]
    else:
        p += _viz(theme, stats or {}, running, tick)

    p.append(f" [{a}]{H * 3} COMMANDS {H * 35}┐[/]")
    p.append(f" [{a}]{V}[/] [{theme['dim']}]Ctrl-K: say what you want[/]"
             f"  [{a}]{V}[/]")
    p.append(f" [{a}]{V}[/]"
             f" [{theme['dim']}]'todo <t>' · 'swarm <goal>' · 'compare <q>' · "
             f"'agent create <n>'[/]"
             f"  [{a}]{V}[/]")
    p.append(f" [{a}]└" + "─" * 48 + "┘[/]")
    return "\n".join(p)
