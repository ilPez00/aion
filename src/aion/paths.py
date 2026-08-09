"""paths.py — where aion's files live, decided in one place.

Every module used to answer this question for itself, and every one of them
answered it the same wrong way::

    Path(__file__).resolve().parents[2] / "config" / "layout.json"

That expression is not a path, it is an assumption: "I live at
``<root>/src/aion/``". It holds in a git checkout and nowhere else. Installed
into a virtualenv the package lives at ``site-packages/aion/``, so
``parents[2]`` is ``lib/python3.13`` — a real directory, owned by the
interpreter, that will never contain a ``config/``. Nothing raises. The config
is simply never found, defaults are used forever, and ``save_config_section``
tries to write into the standard library tree.

So the resolver has to know which of the two layouts it is in, and there is
exactly one honest signal: a checkout has a ``pyproject.toml`` two levels up
and a parent directory named ``src``. An installed package has neither.

Precedence, highest first:

1. ``$AION_CONFIG`` / ``$AION_DATA`` — explicit, always wins, no guessing.
2. The checkout, when running from one. This is not the tidier choice; XDG
   would be. It is the compatible one: every node in the fleet today runs
   from a checkout and reads ``<root>/config/layout.json``, and quietly
   relocating their config to ``~/.config`` would hand each of them factory
   defaults on next restart. A packaging change must not reconfigure a
   running fleet.
3. XDG (``$XDG_CONFIG_HOME``, ``$XDG_DATA_HOME``), then the ``~/.config`` and
   ``~/.local/share`` defaults. This is the installed-user path.

``checkout_root()`` returns ``None`` rather than a guess when there is no
checkout. Callers that need a git tree — self-update — must be able to tell
"no repository here" from "a repository somewhere". A wrong directory would
read as the second.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "checkout_root", "config_file", "data_dir", "notes_dir", "running_installed",
]

_PKG = Path(__file__).resolve().parent


def checkout_root() -> Path | None:
    """The source checkout this package is running from, or None.

    Both conditions are required. ``src`` alone is a common directory name;
    ``pyproject.toml`` alone can sit above a virtualenv in someone's project.
    Together they are the layout this repository actually has.
    """
    if _PKG.parent.name != "src":
        return None
    root = _PKG.parents[1]
    return root if (root / "pyproject.toml").is_file() else None


def running_installed() -> bool:
    """True when aion was installed rather than run from its source tree."""
    return checkout_root() is None


def _xdg(var: str, default: str) -> Path:
    raw = os.environ.get(var, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / default


def config_file() -> Path:
    """Path to ``layout.json``. May not exist yet; callers handle absence."""
    override = os.environ.get("AION_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    root = checkout_root()
    if root is not None:
        return root / "config" / "layout.json"
    return _xdg("XDG_CONFIG_HOME", ".config") / "aion" / "layout.json"


def data_dir() -> Path:
    """Directory for aion's mutable data (notes, captures, logs)."""
    override = os.environ.get("AION_DATA", "").strip()
    if override:
        return Path(override).expanduser()
    root = checkout_root()
    if root is not None:
        return root
    return _xdg("XDG_DATA_HOME", ".local/share") / "aion"


def notes_dir() -> Path:
    return data_dir() / "notes"
