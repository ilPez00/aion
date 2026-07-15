#!/usr/bin/env python3
"""filetui.py — a minimal, dependency-free TUI file manager.

Renders to stdout (captured by aion's PTY host like micro does) so it lives
inside the HUD as the "organic file visualizer"'s navigable companion. No curses
(works under pyte). Arrow keys navigate, Enter opens dir / edits file in micro,
Backspace goes up, Q quits. Self-contained — no external binary needed.

This is the lightweight stand-in for yazi/lf (not installed on this host); swap
the launch command for `lf`/`yazi` once available.
"""
import os, sys, termios, tty

HERE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())


def list_dir(p):
    try:
        entries = sorted(os.listdir(p))
    except Exception:
        return ["<unreadable>"]
    out = []
    for e in entries:
        if e.startswith("."):
            continue
        full = os.path.join(p, e)
        out.append(("d" if os.path.isdir(full) else "f", e))
    return out


def render(path, items, sel):
    cols, rows = 80, 24
    try:
        cols = os.get_terminal_size().columns
        rows = os.get_terminal_size().lines
    except Exception:
        pass
    lines = []
    lines.append(f"\x1b[36maion files › {path}\x1b[0m")
    lines.append("\x1b[2m↑↓ navigate · ⏎ open · ⌫ up · Q quit\x1b[0m")
    lines.append("")
    visible = rows - 4
    start = max(0, min(sel - visible // 2, max(0, len(items) - visible)))
    for i in range(start, min(len(items), start + visible)):
        kind, name = items[i]
        mark = "▶" if i == sel else " "
        col = "\x1b[36m" if kind == "d" else "\x1b[32m"
        lines.append(f"{mark} {col}{name}\x1b[0m" + ("/" if kind == "d" else ""))
    while len(lines) < rows - 1:
        lines.append("")
    sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(lines) + "\n")
    sys.stdout.flush()


def main():
    path = HERE
    items = list_dir(path)
    sel = 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        while True:
            render(path, items, sel)
            ch = sys.stdin.read(1)
            if ch == "q" or ch == "Q":
                break
            elif ch == "\x1b":  # arrow escape
                sys.stdin.read(1)  # [
                arrow = sys.stdin.read(1)
                if arrow == "A":  # up
                    sel = max(0, sel - 1)
                elif arrow == "B":  # down
                    sel = min(len(items) - 1, sel + 1)
            elif ch in ("\r", "\n"):  # enter
                if not items:
                    continue
                kind, name = items[sel]
                full = os.path.join(path, name)
                if kind == "d":
                    path = full
                    items = list_dir(path)
                    sel = 0
                else:
                    os.system(f"micro {full!r}")
                    items = list_dir(path)
            elif ch == "\x7f":  # backspace
                parent = os.path.dirname(path)
                if parent != path:
                    path = parent
                    items = list_dir(path)
                    sel = 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
