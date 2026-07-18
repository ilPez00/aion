"""
apps.py — the TUI "desktop programs" registry.

aion does not reimplement mail/editors/spreadsheets; it wraps best-in-class
terminal apps and runs them live inside the Term workspace (TermHarness pty).
Each app is a fallback chain of candidate binaries — first one installed wins,
same pattern as the btop > top > htop probe in term.py. Nothing is bundled:
if no candidate is installed, resolve() returns the install hint instead.

Palette commands (wired in store._run_command):
    apps               list every app + availability
    app <name> [args]  launch app in the Term workspace (args appended)
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:
    binary: str                  # what to probe with shutil.which
    args: str = ""               # default args appended after the binary
    install: str = ""            # one-line install hint (Debian-first)

    @property
    def command(self) -> str:
        return f"{self.binary} {self.args}".strip()


@dataclass(frozen=True)
class AppSpec:
    id: str                      # palette name: "app mail"
    label: str
    category: str
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)


APPS: dict[str, AppSpec] = {a.id: a for a in (
    AppSpec("mail", "Email", "comms", (
        Candidate("aerc", install="sudo apt install aerc"),
        Candidate("neomutt", install="sudo apt install neomutt"),
        Candidate("himalaya", install="cargo install himalaya"),
        Candidate("mutt", install="sudo apt install mutt"),
    )),
    AppSpec("edit", "Editor", "text", (
        Candidate("micro", install="sudo apt install micro"),
        Candidate("hx", install="sudo apt install helix"),
        Candidate("nvim", install="sudo apt install neovim"),
        Candidate("vim", install="sudo apt install vim"),
        Candidate("nano", install="sudo apt install nano"),
    )),
    AppSpec("sheet", "Spreadsheet", "data", (
        Candidate("vd", install="sudo apt install visidata"),
        Candidate("sc-im", install="sudo apt install sc-im"),
    )),
    AppSpec("files", "File manager", "files", (
        Candidate("yazi", install="cargo install --locked yazi-fm"),
        Candidate("lf", install="sudo apt install lf"),
        Candidate("ranger", install="sudo apt install ranger"),
        Candidate("mc", install="sudo apt install mc"),
    )),
    AppSpec("git", "Git UI", "dev", (
        Candidate("lazygit", install="sudo apt install lazygit"),
        Candidate("gitui", install="sudo apt install gitui"),
        Candidate("tig", install="sudo apt install tig"),
    )),
    AppSpec("rss", "RSS reader", "comms", (
        Candidate("newsboat", install="sudo apt install newsboat"),
    )),
    AppSpec("monitor", "System monitor", "sys", (
        Candidate("btop", install="sudo apt install btop"),
        Candidate("htop", install="sudo apt install htop"),
        Candidate("top"),
    )),
)}


def resolve(app_id: str, extra_args: str = "") -> tuple[str | None, str]:
    """Return (command, note) for an app id.

    command is the launchable command line of the first installed candidate
    (with extra_args appended), or None if nothing is installed — then note
    carries the install hint for the preferred candidate.
    """
    spec = APPS.get(app_id)
    if spec is None:
        return None, f"unknown app '{app_id}' — try: {' '.join(APPS)}"
    for c in spec.candidates:
        if shutil.which(c.binary):
            cmd = f"{c.command} {extra_args}".strip()
            return cmd, c.binary
    hint = spec.candidates[0].install if spec.candidates else ""
    return None, f"{spec.label}: nothing installed — {hint}"


def list_apps() -> list[str]:
    """One line per app: id, label, resolved binary or install hint."""
    lines = []
    for spec in APPS.values():
        cmd, note = resolve(spec.id)
        status = f"-> {note}" if cmd else f"missing ({note.split(' — ')[-1]})"
        lines.append(f"app {spec.id:<8} {spec.label:<15} {status}")
    return lines
