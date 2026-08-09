"""Where aion looks for its files, under both layouts it can be run from.

This file exists because 1868 tests could not see that an installed aion
reads its config from `lib/python3.13/config/layout.json`. They could not see
it because every one of them runs from the checkout, where the broken
expression `Path(__file__).parents[2]` happens to be right.

So the checkout is not the interesting case here. The installed case is, and
it cannot be reached by importing the module normally — the answer is baked
into `aion/paths.py`'s own location. The tests below reach it by pointing the
resolver's `_PKG` at a directory laid out like site-packages and re-asking.

Every test clears AION_CONFIG/AION_DATA/XDG_*. Inheriting a developer's
environment would make these pass or fail based on the machine.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion import paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AION_CONFIG", "AION_DATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)


def _as_installed(monkeypatch, tmp_path: Path) -> Path:
    """Relocate the resolver into a site-packages-shaped tree.

    `<tmp>/lib/python3.11/site-packages/aion/` — deliberately with a real
    directory two levels up, because that is exactly what made the original
    bug silent: `parents[2]` was not a missing path, it was the wrong one.
    """
    pkg = tmp_path / "lib" / "python3.11" / "site-packages" / "aion"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(paths, "_PKG", pkg)
    return pkg


def _as_checkout(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "aion"
    (root / "src" / "aion").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='aion'\n")
    monkeypatch.setattr(paths, "_PKG", root / "src" / "aion")
    return root


# --- detecting the layout ------------------------------------------------

def test_a_real_checkout_is_recognised(monkeypatch, tmp_path):
    root = _as_checkout(monkeypatch, tmp_path)
    assert paths.checkout_root() == root
    assert paths.running_installed() is False


def test_site_packages_is_not_a_checkout(monkeypatch, tmp_path):
    _as_installed(monkeypatch, tmp_path)
    assert paths.checkout_root() is None
    assert paths.running_installed() is True


def test_src_without_pyproject_is_not_a_checkout(monkeypatch, tmp_path):
    """Someone else's `src/` directory is not this repository.

    Both halves of the signal are load-bearing; a test for each.
    """
    pkg = tmp_path / "vendored" / "src" / "aion"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(paths, "_PKG", pkg)
    assert paths.checkout_root() is None


def test_pyproject_above_a_non_src_parent_is_not_a_checkout(monkeypatch, tmp_path):
    """A virtualenv inside someone's project sees their pyproject.toml."""
    pkg = tmp_path / "proj" / "venv" / "aion"
    pkg.mkdir(parents=True)
    (tmp_path / "proj" / "pyproject.toml").write_text("[project]\nname='theirs'\n")
    monkeypatch.setattr(paths, "_PKG", pkg)
    assert paths.checkout_root() is None


# --- the bug, stated directly --------------------------------------------

def test_installed_config_never_lands_inside_the_interpreter(monkeypatch, tmp_path):
    """The original defect: config resolved to `<venv>/lib/python3.11`.

    Not merely "somewhere else" — asserted as "not under the tree that holds
    the package", because that tree is the one aion must never write to.
    """
    pkg = _as_installed(monkeypatch, tmp_path)
    cfg = paths.config_file()
    assert tmp_path / "lib" not in cfg.parents
    assert pkg not in cfg.parents
    assert cfg == Path.home() / ".config" / "aion" / "layout.json"


def test_checkout_config_stays_where_the_fleet_expects_it(monkeypatch, tmp_path):
    """Nodes in the field read `<root>/config/layout.json` today.

    If this ever moves to XDG, every deployed node silently boots on factory
    defaults. That is the reason the checkout outranks XDG, so it is a test.
    """
    root = _as_checkout(monkeypatch, tmp_path)
    assert paths.config_file() == root / "config" / "layout.json"


# --- precedence ----------------------------------------------------------

def test_explicit_override_beats_a_checkout(monkeypatch, tmp_path):
    _as_checkout(monkeypatch, tmp_path)
    monkeypatch.setenv("AION_CONFIG", str(tmp_path / "elsewhere.json"))
    assert paths.config_file() == tmp_path / "elsewhere.json"


def test_blank_override_is_not_an_override(monkeypatch, tmp_path):
    """`AION_CONFIG=` in a unit file means unset, not "use the empty path"."""
    root = _as_checkout(monkeypatch, tmp_path)
    monkeypatch.setenv("AION_CONFIG", "   ")
    assert paths.config_file() == root / "config" / "layout.json"


def test_xdg_is_honoured_when_installed(monkeypatch, tmp_path):
    _as_installed(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdgdata"))
    assert paths.config_file() == tmp_path / "xdg" / "aion" / "layout.json"
    assert paths.data_dir() == tmp_path / "xdgdata" / "aion"


def test_xdg_is_ignored_in_a_checkout(monkeypatch, tmp_path):
    root = _as_checkout(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths.config_file() == root / "config" / "layout.json"


# --- data ----------------------------------------------------------------

def test_notes_live_under_the_data_dir_in_both_layouts(monkeypatch, tmp_path):
    root = _as_checkout(monkeypatch, tmp_path)
    assert paths.notes_dir() == root / "notes"
    _as_installed(monkeypatch, tmp_path)
    assert paths.notes_dir() == Path.home() / ".local" / "share" / "aion" / "notes"


def test_data_override_is_separate_from_config_override(monkeypatch, tmp_path):
    """Two variables, two effects. Setting one must not move the other."""
    _as_installed(monkeypatch, tmp_path)
    monkeypatch.setenv("AION_DATA", str(tmp_path / "d"))
    assert paths.data_dir() == tmp_path / "d"
    assert paths.config_file() == Path.home() / ".config" / "aion" / "layout.json"


# --- the callers actually go through the resolver ------------------------

def test_config_readers_all_agree(monkeypatch, tmp_path):
    """`core.config_path` and `procgraph.read_harnesses` resolved the path
    independently, with two copies of the same wrong expression. One resolver
    now, and this asserts they did not keep private copies."""
    import json

    from aion import core, procgraph

    target = tmp_path / "layout.json"
    target.write_text(json.dumps({"harnesses": [{"id": "h1", "name": "one"}]}))
    monkeypatch.setenv("AION_CONFIG", str(target))

    assert core.config_path() == target
    assert [h["id"] for h in procgraph.read_harnesses()] == ["h1"]


def test_status_reports_no_revision_when_installed(monkeypatch, tmp_path):
    """An installed aion has no git tree. `/status` must say so rather than
    report whatever HEAD sits above site-packages — a peer distinguishes
    "not a git install" from "unreadable git install" by this field."""
    from aion import fleet

    _as_installed(monkeypatch, tmp_path)
    monkeypatch.setattr(fleet, "_REVISION_CACHE", None)
    assert fleet._self_revision() == {"sha": "", "branch": "", "dirty": False}
    monkeypatch.setattr(fleet, "_REVISION_CACHE", None)
