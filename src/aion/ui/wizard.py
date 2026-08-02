"""wizard.py — the setup wizard's decisions, separated from its widgets.

The wizard itself stays in `app.py`: it drives Textual widgets, focuses an
input, and shells out to a package manager, none of which belong here. What
moved is the part that decides things, and one piece in particular.

`_wizard_finish` rewrites `~/.env` — the file holding every API key the user
owns — by truncating it and writing a merged copy back. It had no test and no
atomic write, so a crash or a full disk between truncate and write left an
empty file and no key. `merge_env` is now a pure text-to-text function with
the cases written down, and `write_env` writes through a temporary file so the
original survives anything that goes wrong.
"""
from __future__ import annotations

import os
from pathlib import Path

# What each install step can be in. Named because the render and the
# transition both switch on them and were doing so with bare strings.
FOUND, MISSING, INSTALLING, FAILED, SKIPPED = (
    "found", "missing", "installing", "failed", "skipped")

# Statuses from which Enter simply moves on. INSTALLING is deliberately absent:
# a keypress during an install must not advance past work still running.
DONE_STATES = (FOUND, SKIPPED, FAILED)


def parse_env(text: str) -> dict[str, str]:
    """`KEY=value` lines into a dict. Comments and blanks ignored. Pure."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def merge_env(existing: str, collected: dict[str, str]) -> str:
    """The new contents of ~/.env. Pure.

    Rules, all of which exist because this file is hand-edited:

      * A key already present is REPLACED IN PLACE, so the ordering and
        grouping someone built up by hand survives.
      * Comments, blank lines and anything that is not `KEY=value` are copied
        through untouched. A wizard that strips a user's comments out of their
        own credentials file has overstepped.
      * Empty values are dropped by the caller, not written as `KEY=`, which
        would shadow a real value exported elsewhere in the shell.
      * New keys go at the end, in the order collected.
    """
    collected = {k: v for k, v in (collected or {}).items() if v}
    lines: list[str] = []
    seen: set[str] = set()
    for line in (existing or "").splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in collected and key not in seen:
                lines.append(f"{key}={collected[key]}")
                seen.add(key)
                continue
        lines.append(line)
    for key, value in collected.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n" if lines else ""


def write_env(path: Path, text: str) -> None:
    """Replace the env file without ever truncating the original.

    `write_text` opens with O_TRUNC: the old contents are gone before a single
    byte of the new ones lands. For a file of API keys that is the difference
    between a failed save and a lost account.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)                 # atomic within one filesystem


def key_preview(value: str) -> str:
    """A secret, shown safely. Never the whole thing, even on your own screen."""
    if not value:
        return ""
    if len(value) <= 12:
        return value[:2] + "…"
    return f"{value[:8]}...{value[-4:]}"


def install_advice(step: dict) -> str:
    """The command that would install this step's dependency. Pure."""
    if "install_cmd" in step:
        return "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
    return f"npm install -g {step.get('pkg', '')}"


def next_action(step_type: str, status: str, found: bool) -> str:
    """What Enter does on this step: "advance", "install" or "wait". Pure.

    `found` wins over a stale status: a binary installed in another terminal
    while the wizard sat open should not still be offered for installation.
    """
    if step_type != "install":
        return "advance"
    if found or status in DONE_STATES:
        return "advance"
    if status == MISSING:
        return "install"
    return "wait"


def install_result(returncode: int, present: bool) -> str:
    """Status after an install attempt. Pure.

    Both conditions, not either: npm exits 0 having installed something the
    PATH cannot see often enough that trusting the exit code alone reports
    success for a binary that is not there.
    """
    return FOUND if (returncode == 0 and present) else FAILED
