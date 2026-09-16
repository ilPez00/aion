"""Probe budget: one knob for how long the cockpit waits on a host.

Every remote collector in here talks to nodes over `ssh`, and the ssh client's
patience becomes the shell's patience. In production the defaults below are what
you want — a slow node must read as slow, not as broken.

In the test suite they are poison: each app boot fans out probes to the whole
fleet, a box that is powered off costs its full connect timeout *per probe*, and
the Textual pilot gives up waiting for a screen that is still blocked in
`on_mount`. That is a test that fails for the state of the user's house, not for
the state of the code — which is worse than a slow test, because it trains you
to ignore red.

So the budget is an environment knob, read at call time:

    AION_PROBE_CONNECT_TIMEOUT   ssh -o ConnectTimeout, seconds (default 6)
    AION_PROBE_TIMEOUT           whole-command budget, seconds  (default 15)

`tests/conftest.py` shrinks both for the suite. Anything unparseable falls back
to the default rather than raising: a typo in an env var must not take the HUD
down with it.
"""
from __future__ import annotations

import os

DEFAULT_CONNECT_TIMEOUT = 6
DEFAULT_BUDGET = 15


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return value if value > 0 else default


def connect_timeout(default: int = DEFAULT_CONNECT_TIMEOUT) -> int:
    """Seconds ssh may spend establishing a connection."""
    return _positive_int("AION_PROBE_CONNECT_TIMEOUT", default)


def budget(default: int = DEFAULT_BUDGET) -> int:
    """Seconds a whole probe subprocess may take."""
    return _positive_int("AION_PROBE_TIMEOUT", default)


def ssh_opts(*, keepalive: int | None = None) -> list[str]:
    """Standard non-interactive ssh options, with the budgeted connect timeout."""
    opts = ["-o", f"ConnectTimeout={connect_timeout()}", "-o", "BatchMode=yes"]
    if keepalive:
        opts += ["-o", f"ServerAliveInterval={keepalive}"]
    return opts
