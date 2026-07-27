"""opener.py — hand a path to the editor the user actually uses.

The HUD knows where everything is; the thing you want next is usually "open
this in my editor". That is a one-line action and a surprisingly sharp edge:
the web HUD is LAN-reachable, so "run a program on a path" is exactly the
primitive an attacker wants.

The safety model is therefore narrow on purpose:

  * the editor is chosen from a fixed ALLOWLIST, never from the request
  * the path is resolved and confined to the filesystem sandbox by the caller
    (`fsgraph.resolve_in_root`) before it reaches here
  * the command is built as an argv list and spawned without a shell, so
    nothing in a filename can be interpreted as syntax
  * `--` terminates option parsing, so a file called `-R` opens rather than
    reconfiguring the editor

Detection is pure and testable; only `launch()` touches a process.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

# Ordered by preference. `gui` editors get detached and keep running; terminal
# editors are listed so `AION_EDITOR=nvim` still resolves, but they are only
# useful when the HUD and the terminal share a display.
ALLOWLIST: tuple[tuple[str, bool], ...] = (
    ("zed", True),
    ("code", True),
    ("cursor", True),
    ("subl", True),
    ("gnome-text-editor", True),
    ("kate", True),
    ("nvim", False),
    ("vim", False),
    ("micro", False),
    ("helix", False),
    ("xdg-open", True),      # last resort: whatever the desktop associates
)
ALLOWED = {name for name, _ in ALLOWLIST}
GUI = {name for name, gui in ALLOWLIST if gui}


class OpenError(RuntimeError):
    """No usable editor, or one that was asked for is not permitted."""


@dataclass
class Editor:
    name: str
    path: str
    gui: bool

    @property
    def detached(self) -> bool:
        return self.gui


def available() -> list[Editor]:
    """Every allowlisted editor present on this machine, in preference order."""
    out: list[Editor] = []
    for name, gui in ALLOWLIST:
        found = shutil.which(name)
        if found:
            out.append(Editor(name=name, path=found, gui=gui))
    return out


def pick(preferred: str | None = None) -> Editor:
    """Resolve which editor to use.

    `AION_EDITOR` wins when set, but only if it is on the allowlist — an env
    var is not a licence to run an arbitrary binary, since anything that can
    write the environment of this process could otherwise pick `sh`.
    """
    want = (preferred or os.environ.get("AION_EDITOR", "")).strip()
    found = available()
    if want:
        base = os.path.basename(want)
        if base not in ALLOWED:
            raise OpenError(
                f"editor {base!r} is not allowlisted "
                f"(allowed: {', '.join(sorted(ALLOWED))})")
        for e in found:
            if e.name == base:
                return e
        raise OpenError(f"editor {base!r} is not installed")
    if not found:
        raise OpenError("no supported editor found on this machine")
    return found[0]


def command_for(path: str, editor: Editor, *, line: int | None = None) -> list[str]:
    """Build the argv. Never a string, never through a shell.

    `--` before the path stops the editor treating a filename beginning with
    a dash as an option. Zed and VS Code take `file:line` for a line jump;
    the rest are opened at the top rather than guessing wrong syntax.
    """
    target = path
    if line and editor.name in ("zed", "code", "cursor"):
        target = f"{path}:{line}"
    return [editor.path, "--", target]


def launch(path: str, *, preferred: str | None = None,
           line: int | None = None) -> dict:
    """Open `path`. The caller MUST have sandboxed the path already.

    Returns a description of what was run, so the HUD can say which editor it
    used instead of silently doing nothing when several are installed.
    """
    if not path:
        raise OpenError("no path given")
    editor = pick(preferred)
    argv = command_for(path, editor, line=line)
    try:
        if editor.detached:
            # start_new_session detaches from our process group, so closing
            # the HUD does not take the editor down with it.
            subprocess.Popen(argv, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(argv, start_new_session=True)
    except OSError as e:
        raise OpenError(f"could not launch {editor.name}: {e}") from e
    return {"editor": editor.name, "path": path, "argv": argv, "detached": editor.detached}
