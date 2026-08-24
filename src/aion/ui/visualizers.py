"""visualizers.py — animated sci-fi HUD visualizers (pure, no Textual).

Every function returns a rich-markup string. Accept `tick` (int, incremented
each frame) for time-based animation. All are pure and unit-testable.
"""
from __future__ import annotations

import math
from typing import Sequence


# ── palette ──────────────────────────────────────────────────────────────────
# Named aliases onto the audited tokens (ui/theme.py) so the visualizers can't
# drift from the rest of the cockpit.
from .theme import TOKENS as _T
_CYAN = _T["accent"]
_GREEN = _T["ok"]
_YELLOW = _T["warn"]
_RED = _T["err"]
_DIM = _T["dim"]
_WHITE = _T["fg"]

_BLOCKS = " ▏▎▍▌▋▊▉█"


def _gradient(v: float, cold: str = _CYAN, hot: str = _RED) -> str:
    """Interpolate color between cold and hot based on v in 0..1."""
    r1, g1, b1 = int(cold[1:3], 16), int(cold[3:5], 16), int(cold[5:7], 16)
    r2, g2, b2 = int(hot[1:3], 16), int(hot[3:5], 16), int(hot[5:7], 16)
    v = max(0.0, min(1.0, v))
    r = int(r1 + (r2 - r1) * v)
    g = int(g1 + (g2 - g1) * v)
    b = int(b1 + (b2 - b1) * v)
    return f"#{r:02x}{g:02x}{b:02x}"


def _shade(v: float, base: str = _CYAN, bright: str = _WHITE) -> str:
    """Blend base toward bright by v (0-1)."""
    return _gradient(v, base, bright)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Spectrum Equalizer
# ═══════════════════════════════════════════════════════════════════════════════
def spectrum_eq(values: Sequence[float], tick: int, *,
                height: int = 5, labels: Sequence[str] | None = None,
                width: int = 40) -> str:
    """Animated color equalizer — vertical bars with sweeping scan line.

    `values`: 0..1 per metric. Renders as columns of filled blocks.
    A bright scan line bounces up/down each tick.
    """
    n = len(values)
    if n == 0:
        return f"[{_DIM}](no data)[/]"
    labels = labels or [""] * n
    col_w = max(5, (width - n - 1) // n)
    out: list[str] = []
    # scan-line position (bouncing)
    period = max(1, height * 2 - 2)
    scan = tick % period
    if scan >= height:
        scan = period - scan

    for row in range(height - 1, -1, -1):
        cells: list[str] = []
        for i, v in enumerate(values):
            threshold = (row + 0.5) / height
            filled = v >= threshold
            if filled:
                c = _gradient(row / height, _CYAN, _WHITE)
                glint = _WHITE if row == scan else c
                ch = "█"
                # pulse shimmer: extra glow at scan line
                cells.append(f"[{glint}]{ch}[/]")
            else:
                cells.append(" " * 2 if len(values) <= 8 else " ")
        sep = "  " if n <= 8 else " "
        line = sep.join(cells)
        out.append(f"  {line}")

    # label row
    label_cells = [f"{l[:col_w]:^{col_w}s}" for l in labels]
    out.append(f"  {sep.join(label_cells)}")

    # value row
    val_cells = [f"{int(v * 100):3d}%" for v in values]
    out.append(f"  [{_DIM}]{sep.join(val_cells)}[/]")

    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Holo Gauge (semicircular tachometer)
# ═══════════════════════════════════════════════════════════════════════════════
def holo_gauge(pct: float, tick: int, *,
               width: int = 20, label: str = "", color: str = _CYAN) -> str:
    """Semicircular tachometer-style gauge with animated needle pulse.

    Returns a 3-line gauge block:

      ╭──────────────────────╮
      │ ▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░ │ 78% CPU
      ╰──────────────────────╯

    A bright pulse dot travels across the filled portion each cycle.
    """
    pct = max(0.0, min(1.0, pct))
    filled = int(round(pct * width))

    # pulse: glowing dot sweeps across filled portion
    if filled > 0:
        pulse_pos = tick % filled
    else:
        pulse_pos = -1

    cells = []
    for i in range(width):
        if i < filled:
            if i == pulse_pos:
                cells.append(f"[{_WHITE}]▓[/]")
            else:
                cells.append(f"[{color}]▓[/]")
        else:
            cells.append(f"[{_DIM}]░[/]")

    bar = "".join(cells)
    val = f"{int(pct * 100):3d}%"
    label_str = f" {label}" if label else ""

    return (f" [{color}]╭{'─' * (width + 2)}╮[/]\n"
            f" [{color}]│[/] {bar} [{color}]│[/] [{_GREEN}]{val}{label_str}[/]\n"
            f" [{color}]╰{'─' * (width + 2)}╯[/]")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Pulse Radar
# ═══════════════════════════════════════════════════════════════════════════════
def pulse_radar(rings: Sequence[dict], tick: int, *,
                size: int = 16) -> str:
    """Concentric radar with animated dots.

    Each ring: ``{"label": str, "value": float, "items": list[str]}``.
    Renders as a two-layer dot grid with pulsing active dots.
    """
    n_rings = len(rings)
    if n_rings == 0:
        return f"[{_DIM}](idle)[/]"

    # grid of dots: each ring gets a row of `size` dots
    lines: list[str] = []
    for ri, ring in enumerate(rings):
        items = ring.get("items", [])
        val = ring.get("value", 0.0)
        dots: list[str] = []
        active_col = len(items)
        # animate: dots pulse in sequence
        phase = tick % max(active_col, 1)
        for di in range(size):
            is_active = di < active_col
            is_pulse = is_active and (di == phase)
            if is_pulse:
                dots.append(f"[{_WHITE}]◉[/]")
            elif is_active:
                dots.append(f"[{_GREEN}]◉[/]")
            elif di < int(val * size):
                dots.append(f"[{_DIM}]○[/]")
            else:
                dots.append(f"[{_DIM}]·[/]")

        label = ring.get("label", "")
        val_str = f"{int(val * 100):3d}%"
        lines.append(
            f" [{_CYAN}]▌{label}[/]  {''.join(dots)}  [{_DIM}]{val_str}[/]")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Neural Graph
# ═══════════════════════════════════════════════════════════════════════════════
def neural_graph(nodes: Sequence[tuple[str, float]],
                 edges: Sequence[tuple[int, int]],
                 tick: int, *,
                 width: int = 44, height: int = 8) -> str:
    """Graph of harnesses/agents as nodes with pulsing activity.

    `nodes`: [(label, activity_0..1)]
    `edges`: [(from_idx, to_idx)]

    Renders a 2-row node grid. Active nodes pulse white. Braille dots
    between nodes indicate connections (animated flow).
    """
    n = len(nodes)
    if n == 0:
        return f"[{_DIM}](no active nodes)[/]"

    # Position nodes evenly on 2 rows
    row_size = (n + 1) // 2
    positions: list[tuple[int, int]] = []
    for i in range(n):
        row = 0 if i < row_size else 2  # row 0 or 2 (with blank row 1 for edges)
        col = i if i < row_size else i - row_size
        positions.append((col, row))

    # Build 2D char grid
    grid_w = row_size * 6 + 1
    grid_h = 4
    grid = [[" " for _ in range(grid_w)] for _ in range(grid_h)]

    # Draw edges with animated flow
    for fi, ti in edges:
        if fi >= n or ti >= n:
            continue
        x1, y1 = positions[fi]
        x2, y2 = positions[ti]
        cx1, cy1 = x1 * 6 + 3, y1 + 1
        cx2, cy2 = x2 * 6 + 3, y2 + 1
        flow = (tick // 2) % 3
        # Simple horizontal/vertical line segments
        if cy1 == cy2:
            for x in range(min(cx1, cx2) + 1, max(cx1, cx2)):
                grid[cy1][x] = f"[{_DIM}]─[/]"
            # animated flow dot
            flow_x = cx1 + ((cx2 - cx1) * ((tick % 8) + 1) // 8)
            if 0 <= flow_x < grid_w:
                grid[cy1][flow_x] = f"[{_WHITE}]◉[/]"
        else:
            for x in range(min(cx1, cx2) + 1, max(cx1, cx2)):
                grid[1][x] = f"[{_DIM}]─[/]"
            grid[1][cx1] = f"[{_DIM}]│[/]"
            grid[1][cx2] = f"[{_DIM}]│[/]"
            flow_x = cx1 + ((cx2 - cx1) * ((tick % 8) + 1) // 8)
            if 0 <= flow_x < grid_w:
                grid[1][flow_x] = f"[{_WHITE}]◉[/]"

    # Draw nodes
    for i, (label, activity) in enumerate(nodes):
        x, y = positions[i]
        cx = x * 6
        cy = y
        # node activity pulse
        pulse = math.sin(tick * 0.2 + i) * 0.5 + 0.5
        combined = min(1.0, activity * 0.7 + pulse * 0.3)
        if combined > 0.7:
            node_color = _WHITE
            node_ch = "◆"
        elif combined > 0.3:
            node_color = _GREEN
            node_ch = "◇"
        else:
            node_color = _DIM
            node_ch = "○"

        label_short = label[:5]
        row = grid[cy]
        for ci, ch in enumerate(node_ch + f" {label_short}"):
            if cx + ci < grid_w:
                row[cx + ci] = f"[{node_color}]{ch}[/]"

    return "\n".join("".join(row) for row in grid)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Task Wave (heart-monitor style)
# ═══════════════════════════════════════════════════════════════════════════════
def task_wave(history: Sequence[float], tick: int, *,
              width: int = 40, label: str = "TASK") -> str:
    """Horizontal amplitude waveform like a heart monitor.

    `history`: recent values (0..1). Plotted as amplitude over time.
    Animated scanning dot at the leading edge.
    """
    vals = [float(v) for v in history] if history else [0.0]
    if len(vals) < 2:
        vals = [0.0, 0.0]

    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi = lo + 0.1

    # Normalize to 0..1 and scale to amplitude height
    amplitude = 4  # rows of wave
    norm = [(v - lo) / (hi - lo) for v in vals]

    # Build rows of the waveform
    rows: list[list[str]] = [[" " for _ in range(width)] for _ in range(amplitude)]

    for xi in range(min(width, len(norm))):
        v = norm[xi]
        peak_row = int(v * (amplitude - 1))
        ri = amplitude - 1 - peak_row  # convert to row index (0=top)
        # fill the column at peak height
        if 0 <= ri < amplitude:
            color = _gradient(v, _GREEN, _RED)
            rows[ri][xi] = f"[{color}]█[/]"
            # fill below peak
            for r in range(ri + 1, amplitude):
                rows[r][xi] = f"[{_DIM}]█[/]"

    # Scanning dot at leading edge
    scan_x = tick % width
    for r in range(amplitude):
        if rows[r][scan_x] == " ":
            rows[r][scan_x] = f"[{_WHITE}]◉[/]"
            break
    else:
        rows[amplitude - 1][scan_x] = f"[{_WHITE}]◉[/]"

    lines = ["  " + "".join(row) for row in rows]
    lines.append(f"  [{_DIM}]{label} · {len(vals)} samples[/]")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Matrix Stream
# ═══════════════════════════════════════════════════════════════════════════════
_CHARSET = "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン"


def matrix_stream(columns: Sequence[dict], tick: int, *,
                  width: int = 40) -> str:
    """Scrolling data columns ala Matrix.

    Each column: ``{"label": str, "value": float, "stream": list[str]}``.
    Green-on-black style. Characters drop and fade.
    """
    if not columns:
        return f"[{_DIM}]matrix idle[/]"

    n = len(columns)
    col_w = max(4, width // n)
    num_rows = 6

    # Build grid of characters with brightness
    rows: list[list[str]] = [[" " for _ in range(n * col_w)] for _ in range(num_rows)]

    for ci, col in enumerate(columns):
        stream = col.get("stream", list(_CHARSET))
        val = col.get("value", 0.0)
        label = col.get("label", "?")[:col_w]
        length = max(1, int(val * num_rows))

        for ri in range(num_rows):
            offset = (tick + ri) % max(len(stream), 1)
            ch = stream[offset]
            # brightness: brighter near the head, dim near tail
            if ri < length:
                dist = length - 1 - ri
                if dist == 0:
                    colr = _WHITE
                elif dist == 1:
                    colr = _GREEN
                else:
                    brightness = 1.0 - dist / max(length, 1)
                    colr = _shade(brightness, _GREEN, _WHITE)
                x = ci * col_w + (ri % col_w)
                if x < n * col_w:
                    rows[ri][x] = f"[{colr}]{ch}[/]"

    # overlay labels on last row
    for ci in range(n):
        x = ci * col_w
        for j, ch in enumerate(columns[ci]["label"][:col_w]):
            if x + j < n * col_w:
                rows[-1][x + j] = f"[{_CYAN}]{ch}[/]"

    return "\n".join("  " + "".join(row) for row in rows)


# ── Viz rotation helper ───────────────────────────────────────────────────────
def pick_viz(kind: str, data: dict, tick: int, **kw) -> str:
    """Dispatch to a visualizer by name with data from a dict.

    Kind: spectrum | gauge | radar | graph | wave | matrix | flow
    """
    if kind == "spectrum":
        return spectrum_eq(data.get("values", []), tick, **kw)
    if kind == "gauge":
        return holo_gauge(data.get("pct", 0.0), tick, label=data.get("label", ""), **kw)
    if kind == "radar":
        return pulse_radar(data.get("rings", []), tick, **kw)
    if kind == "graph":
        return neural_graph(data.get("nodes", []), data.get("edges", []), tick, **kw)
    if kind == "wave":
        return task_wave(data.get("history", []), tick, **kw)
    if kind == "matrix":
        return matrix_stream(data.get("columns", []), tick, **kw)
    if kind == "flow":
        return flow_pipeline(data.get("scores", []), tick, **kw)
    return f"[{_DIM}]unknown viz: {kind}[/]"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Life-Flow Pipeline — machine · body · people · money
# ═══════════════════════════════════════════════════════════════════════════════
def flow_pipeline(scores: Sequence[tuple[str, float]], tick: int, *,
                  height: int = 6, width: int = 44) -> str:
    """Vertical fill columns, one per life domain, labels on the base row.

    `scores`: (name, 0..1) pairs in display order. A dead domain (0.0) draws
    an explicit ○ gap marker instead of silence — a hole in the HUD is
    information. The bright cell sweeps upward with `tick` so the pipeline
    reads as flowing even when values hold steady.
    """
    n = len(scores)
    if n == 0:
        return f"[{_DIM}](no data)[/]"

    # Last row of the height budget belongs to the labels.
    bar_rows = max(1, height - 1)
    col_w = max(4, (width - n - 1) // n)

    grid: list[list[str]] = [[f"[{_DIM}]{'·' * col_w}[/]" for _ in range(n)]
                             for _ in range(bar_rows)]

    for ci, (name, v) in enumerate(scores):
        v = max(0.0, min(1.0, float(v)))
        fill = int(round(v * bar_rows))
        if v > 0.0 and fill == 0:
            fill = 1  # alive but barely — still show a sliver
        for ri in range(bar_rows):
            # ri counts UP from the top row; filled cells live in the bottom
            # `fill` rows of the column.
            row_from_bottom = bar_rows - 1 - ri
            if v <= 0.001:
                # dead domain: one explicit gap marker on the base row
                if row_from_bottom == 0:
                    grid[ri][ci] = f"[{_DIM}]{'○'.center(col_w)}[/]"
                continue
            if row_from_bottom >= fill:
                continue
            color = _gradient(v, cold=_CYAN, hot=_GREEN)
            # sweep: one highlighted cell travels up the filled section
            sweep_row = (bar_rows - 1) - ((tick + ci) % max(fill, 1))
            ch = "█" if ri != sweep_row else "▓"
            cell = ch * col_w if v >= 0.99 else ch * max(1, col_w - 2)
            cell = cell[:col_w]
            grid[ri][ci] = f"[{_shade(v, color, _WHITE)}]{cell}[/]" \
                if ri == sweep_row else f"[{color}]{cell}[/]"

    labels = " ".join(name.upper()[:col_w].center(col_w) for name, _ in scores)
    lines = ["".join(row) for row in grid] + [f"[{_CYAN}]{labels}[/]"]
    return "\n".join(lines)
