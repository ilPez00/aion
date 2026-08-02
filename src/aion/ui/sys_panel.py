"""sys_panel.py — the Iron Man HUD: machine and body, as gauges.

Lifted out of `AiOSApp._sys_panel`, which was 165 lines reachable only by
constructing a Textual app. Everything it needed from the app was four values
— the stats snapshot, an animation tick, whether the deck is plugged in, and
how many tasks are live — so it takes those and returns a string.

The reason that matters is not line count. This panel renders every tick from
whatever `sysinfo`/`health`/`physis` last put in the stats dict, and each of
those can be missing, partial, or degraded independently. That is a table of
cases, and a table of cases wants tests rather than a comment saying it copes.

Every section is optional and every one fails soft: a HUD that renders nothing
because one sensor went away is worse than a HUD with a gap in it.
"""
from __future__ import annotations

from .gauges import (bytes_per_sec, core_grid, cyclops_panel, gauge_panel,
                     hbar, mem_readable, metric, sparkline)
from .visualizers import holo_gauge, spectrum_eq

# Above these, a CPU sensor is worth looking at. Two thresholds, not one: warm
# is information, hot is a reason to stop what you are doing.
TEMP_HOT_C = 85
TEMP_WARM_C = 70


def temp_colour(celsius: float, theme: dict) -> str:
    if celsius > TEMP_HOT_C:
        return theme["err"]
    if celsius > TEMP_WARM_C:
        return theme["warn"]
    return theme["ok"]


def _pcts(sys_: dict | None) -> tuple[float, float, float]:
    """(cpu, mem, disk) as 0..1. Zeroes when stats are missing. Pure."""
    if not sys_ or not sys_.get("ok"):
        return 0.0, 0.0, 0.0
    cpu = sys_["cpu"]["total_pct"] / 100.0
    mem = sys_["mem"]["pct"] / 100.0
    disk = max((d["pct"] for d in sys_.get("disks", [])), default=0) / 100.0
    return cpu, mem, disk


def _holo_row(sys_: dict, theme: dict, tick: int) -> str:
    """Three gauges side by side, merged row by row."""
    cpu, mem, disk = _pcts(sys_)
    gauges = [
        holo_gauge(cpu, tick, width=14, label="CPU", color=theme["warn"]),
        holo_gauge(mem, tick, width=14, label="RAM", color=theme["accent"]),
        holo_gauge(disk, tick, width=14, label="DSK", color=theme["warn"]),
    ]
    split = [g.split("\n") for g in gauges]
    return "\n".join("  ".join(g[row] for g in split if row < len(g))
                     for row in range(3))


def _computer_blocks(sys_: dict, theme: dict) -> list[str]:
    cpu = sys_["cpu"]
    cpu_line = (metric("CPU", f"{cpu['total_pct']}", "%", theme["warn"]) +
                f"  [{theme['dim']}]{cpu['cores']}c load {cpu['load1']}[/]")
    ram_line = (metric("RAM", f"{sys_['mem']['pct']}", "%", theme["accent"]) +
                f"  [{theme['dim']}]{mem_readable(sys_['mem']['used'])}/"
                f"{mem_readable(sys_['mem']['total'])}[/]")
    ram_bar = hbar(sys_["mem"]["pct"] / 100.0, width=20, color=theme["accent"])
    blocks = [gauge_panel("COMPUTER",
                          "\n  ".join([cpu_line, core_grid(cpu["per_core_pct"]),
                                       ram_line, ram_bar]),
                          theme["accent"])]

    disk_lines: list[str] = []
    for d in sys_["disks"]:
        disk_lines.append(metric(d["mount"], f"{d['pct']}", "%", theme["warn"]) +
                          f"  [{theme['dim']}]{mem_readable(d['free'])} free[/]")
        disk_lines.append("  " + hbar(d["pct"] / 100.0, width=16,
                                      color=theme["warn"]))
    if disk_lines:
        blocks.append(gauge_panel("STORAGE", "\n  ".join(disk_lines),
                                  theme["warn"]))

    net = sys_["net"]
    net_line = (metric("up", bytes_per_sec(net["up_bps"]), "", theme["ok"]) + "\n  " +
                metric("dn", bytes_per_sec(net["down_bps"]), "", theme["ok"]) + "\n  " +
                f"[{theme['dim']}]{net['conns']} active conns[/]")
    blocks.append(gauge_panel("NETWORK", net_line, theme["ok"]))

    gpu = sys_.get("gpu") or {}
    if "gpu_util_pct" in gpu:
        g = (metric("util", f"{gpu['gpu_util_pct']}", "%", theme["accent"]) + "\n  " +
             hbar(gpu["gpu_util_pct"] / 100.0, width=16, color=theme["accent"]) + "\n  " +
             f"[{theme['dim']}]{gpu.get('gpu_mem_mb', 0)}/"
             f"{gpu.get('gpu_mem_total_mb', 0)} MB[/]")
        blocks.append(gauge_panel("GPU", g, theme["accent"]))
    elif "gpu_models" in gpu:
        blocks.append(gauge_panel(
            "GPU", f"[{theme['ok']}]{gpu['gpu_models']} model(s) loaded · "
                   f"{gpu.get('gpu_vram_mb', 0)}MB[/]", theme["accent"]))
    return blocks


def _thermal_block(sys_: dict | None, theme: dict) -> str:
    th = (sys_ or {}).get("thermal") if sys_ and sys_.get("ok") else None
    if not th:
        return ""
    cpu = th.get("cpu") or []
    other = th.get("other") or []
    lines: list[str] = []
    if cpu:
        max_c = th.get("max_cpu_c", max((t["current"] for t in cpu), default=0))
        lines.append(metric("max", f"{max_c:.1f}", "°C", temp_colour(max_c, theme)))
        for t in cpu[:4]:            # top 4 cpu sensors
            c = t["current"]
            lines.append("  " + metric(t["label"], f"{c:.1f}", "°C",
                                       temp_colour(c, theme)))
    for t in other[:4]:              # top 4 everything else, never coloured:
        c = t["current"]             # a disk or a battery has its own limits
        lines.append("  " + metric(t["label"], f"{c:.1f}", "°C", theme["dim"]))
    if not lines:
        return ""
    return gauge_panel("THERMAL", "\n  ".join(lines), theme["err"])


def _health_block(health: dict | None, theme: dict) -> str:
    if not (health and health.get("ok")):
        return gauge_panel("REAL LIFE",
                           "[#9aabbb](no health data — source: google/apple/json)[/]",
                           theme["ok"])
    av = health.get("avg_7d", {})
    latest = health.get("latest") or {}
    lines = [
        metric("steps", str(latest.get("steps", 0)), "", theme["ok"]),
        metric("bpm", str(latest.get("heart_rate", 0)), "", theme["err"]),
        metric("sleep", f"{latest.get('sleep_hours', 0)}", "h", theme["accent"]),
        metric("active", f"{latest.get('active_calories', 0)}", "kcal", theme["warn"]),
        f"[{theme['dim']}](7d avg steps {av.get('steps', 0)} · "
        f"sleep {av.get('sleep_hours', 0)}h)[/]",
    ]
    series = health.get("series", {})
    if series.get("steps"):
        lines.append("  " + sparkline(series["steps"], width=20))
    return gauge_panel("REAL LIFE", "\n  ".join(lines), theme["ok"])


def _physis_block(physis: dict | None, theme: dict) -> str:
    if not physis:
        return ""
    a, di, ok, warn = theme["accent"], theme["dim"], theme["ok"], theme["warn"]
    status = f"[{warn}]DEGRADED[/]" if physis.get("degraded") else f"[{ok}]LIVE[/]"
    lines = [f"  engine: {status}  embedder: [{a}]{physis.get('kind', '?')}[/]  "
             f"semantic: {'yes' if physis.get('semantic') else 'no'}"]
    graph = physis.get("graph", {}) or {}
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    edges = graph.get("edges", []) if isinstance(graph, dict) else []
    if nodes:
        lines.append(f"[{di}]holarchy: {len(nodes)} nodes · {len(edges)} edges[/]")
        for n in nodes[:5]:
            label = n.get("label", n.get("id", "?")) if isinstance(n, dict) else str(n)
            lines.append(f"  [{a}]•[/] {label[:40]}")
    return gauge_panel("PHYSIS", "\n  ".join(lines), theme["accent"])


def render_sys(stats: dict, theme: dict, *, tick: int = 0,
               deck_available: bool = False, running: int = 0) -> str:
    """The whole panel. `stats` is `store.state.stats`; nothing else is read.

    Every section is optional. A missing sensor leaves a gap; it never raises,
    because this renders on a timer and one exception here takes the cockpit
    down rather than one panel.
    """
    stats = stats or {}
    sys_ = stats.get("system")
    healthy = bool(sys_ and sys_.get("ok"))
    blocks: list[str] = []

    if healthy:
        blocks.append(_holo_row(sys_, theme, tick))
        blocks.extend(_computer_blocks(sys_, theme))
    else:
        blocks.append(gauge_panel("COMPUTER", "[#9aabbb](stats unavailable)[/]",
                                  theme["accent"]))

    for block in (_thermal_block(sys_, theme),
                  _health_block(stats.get("health"), theme),
                  _physis_block(stats.get("physis"), theme)):
        if block:
            blocks.append(block)

    blocks.append(cyclops_panel(
        ring=stats.get("ring"),
        deck={"available": deck_available, "baud": 115200},
        physis=stats.get("physis")))

    cpu, mem, disk = _pcts(sys_)
    spec = spectrum_eq([cpu, mem, disk, min(running / 3.0, 1.0)], tick,
                       height=4, labels=["CPU", "RAM", "DSK", "TASK"])
    blocks.append(gauge_panel("SPECTRUM", spec, theme["accent"]))
    return "\n\n".join(blocks)
