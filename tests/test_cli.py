"""`aion` with a flag must not launch the cockpit.

`main()` used to ignore argv entirely, so `aion --help` — the first thing
anyone types after installing something — started a full-screen Textual app.
These tests assert that no flag path reaches `.run()`, which is the only part
that actually matters; the exact wording of the help text is not pinned.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.ui import app as appmod


@pytest.fixture(autouse=True)
def _never_launch(monkeypatch):
    """Any call to AiOSApp is a test failure, not a hung test run."""
    def boom(*a, **k):
        raise AssertionError("main() launched the cockpit for a flag")
    monkeypatch.setattr(appmod, "AiOSApp", boom)


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage(flag, capsys):
    appmod.main([flag])
    assert "usage: aion" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["-V", "--version"])
def test_version_prints_a_version(flag, capsys):
    appmod.main([flag])
    out = capsys.readouterr().out.strip()
    assert out and out[0].isdigit()


@pytest.mark.parametrize("flag", ["-w", "--where"])
def test_where_reports_the_resolved_paths(flag, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AION_CONFIG", str(tmp_path / "layout.json"))
    monkeypatch.setenv("AION_DATA", str(tmp_path / "data"))
    appmod.main([flag])
    out = capsys.readouterr().out
    assert str(tmp_path / "layout.json") in out
    assert str(tmp_path / "data") in out
    # The file does not exist; say so rather than implying it was read.
    assert "not created yet" in out


def test_unknown_flag_exits_nonzero_instead_of_launching(capsys):
    with pytest.raises(SystemExit) as e:
        appmod.main(["--verison"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--verison" in err and "usage: aion" in err
