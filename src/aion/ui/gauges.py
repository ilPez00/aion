"""gauges.py — reusable Iron Man / Jarvis HUD widgets (pure, no Textual).

Every function returns a plain string (rich-markup compatible) so the same
helpers render in the TUI center/right rails. Kept dependency-free and
unit-testable (no I/O, no UI).
"""
from __future__ import annotations

import math
from typing import Sequence

# unicode block ramp for sparklines (low -> high)
_SPARK = " ▁▂▃▄▅▆▇█"
# vertical bar ramp for compact per-core CPU
_VBAR = "▏▎▍▌▋▊▉█"


def sparkline(values: Sequence[float], width: int = 24) -> str:
    """A 1-line trend using block chars. Handles flat / short / empty input."""
    vals = [float(v) for v in values]
    if not vals:
        return "·"
    if len(vals) == 1:
        return _SPARK[4]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        # flat series: show mid blocks so it reads as "steady"
        return _SPARK[4] * width
    out = []
    for v in vals:
        # map lo..hi -> 1..7 (skip blank for non-zero feel)
        idx = int(round((v - lo) / (hi - lo) * (len(_SPARK) - 2))) + 1
        idx = max(1, min(len(_SPARK) - 1, idx))
        out.append(_SPARK[idx])
    # pad/truncate to width
    s = "".join(out)
    if len(s) > width:
        s = s[-width:]
    elif len(s) < width:
        s = s + _SPARK[0] * (width - len(s))
    return s


def hbar(pct: float, width: int = 18, color: str = "#7CFFB2") -> str:
    """Horizontal progress bar (mirrors app.bar, exported for reuse)."""
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))
    return (f"[{color}]{'█' * filled}[/]"
            f"[#5a6b7b]{'░' * (width - filled)}[/] "
            f"{int(pct * 100):3d}%")


def vbars(percents: Sequence[float], width: int = 4, color: str = "#5ad1ff") -> str:
    """Stacked per-core mini bars (one short bar per value)."""
    out = []
    for p in percents:
        p = max(0.0, min(1.0, p))
        # width chars per core, ramp by fill level
        filled = int(round(p * width))
        out.append(f"[{color}]{_VBAR[min(len(_VBAR) - 1, filled * (len(_VBAR) - 1) // max(1, width)) if width else 0]}[/]" if filled else "·")
    return " ".join(out)


def core_grid(percents: Sequence[float], group: int = 8, color: str = "#5ad1ff") -> str:
    """Render per-core CPU usage as rows of tiny bars.

    `percents` is a list of 0..100 per logical core. We colour each cell by
    load (cool->hot) so a hot core pops out like a thermal map.
    """
    cells = []
    for p in percents:
        p = max(0.0, min(100.0, p))
        frac = p / 100.0
        filled = int(round(frac * (len(_VBAR) - 1)))
        filled = max(1, filled) if p > 1 else 0
        if p >= 80:
            col = "#FF6B6B"   # hot
        elif p >= 50:
            col = "#FFD479"   # warm
        else:
            col = color
        ch = _VBAR[filled] if filled else "·"
        cells.append(f"[{col}]{ch}[/]")
    rows = []
    for i in range(0, len(cells), group):
        rows.append("".join(cells[i:i + group]))
    return " ".join(rows) if len(rows) == 1 else "\n            ".join(rows)


def metric(label: str, value: str, unit: str = "", color: str = "#c7d3df") -> str:
    """A 'LABEL  value unit' HUD readout line."""
    uv = f" {unit}" if unit else ""
    return f"[{color}]{label}[/][#5a6b7b]:[/] [{color}]{value}{uv}[/]"


def gauge_panel(title: str, body: str, color: str = "#5ad1ff") -> str:
    """Wrap a block of HUD lines under a titled panel header."""
    return f"[{color}]▌ {title}[/]\n  {body}"


def mem_readable(nbytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(nbytes)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f}{units[i]}"


def bytes_per_sec(nbytes: float) -> str:
    return mem_readable(nbytes) + "/s"
