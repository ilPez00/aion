"""AION-004: DailyCheck verdict parsing, staleness, fallback rules.

Gate: python -m pytest tests/test_daily_push.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion.ui.daily_check import (  # noqa: E402
    STALE_AFTER_H,
    DailyCheck,
    default_daily_check_dir,
    read_daily_check,
    render_daily_check,
)

THEME = {"dim": "#9aabbb", "accent": "#5ad1ff", "ok": "#7CFFB2",
         "warn": "#FFD479", "err": "#FF8A8A", "faint": "#6b7d8d"}
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _write(root, name, text, age_h=1.0):
    # mtime is anchored to the fake clock NOW (wall clock runs ahead of it).
    p = root / name
    p.write_text(text)
    import os as _os
    ts = NOW.timestamp() - age_h * 3600
    _os.utime(p, (ts, ts))
    return p


def test_all_green_today(tmp_path):
    _write(tmp_path, "2026-09-10.md", "ALL GREEN — 5 nodes up\n- node: ok\n")
    c = read_daily_check(tmp_path, now=NOW)
    assert c.verdict.startswith("ALL GREEN") and c.is_today and not c.red
    assert "ALL GREEN" in render_daily_check(c, THEME)


def test_yesterday_labelled_not_current(tmp_path):
    _write(tmp_path, "2026-09-09.md", "2 ITEMS need attention\n", age_h=20.0)
    c = read_daily_check(tmp_path, now=NOW)
    assert not c.is_today and "(yesterday)" in c.status_line()
    assert c.red  # non-green verdict


def test_missing_file(tmp_path):
    c = read_daily_check(tmp_path, now=NOW)
    assert c.missing and c.red
    assert render_daily_check(c, THEME).count("no daily check yet") == 1


def test_malformed_first_line(tmp_path):
    _write(tmp_path, "2026-09-10.md", "\n\n   \n")
    c = read_daily_check(tmp_path, now=NOW)
    assert c.missing  # blank file = as if absent


def test_stale_boundary(tmp_path):
    _write(tmp_path, "2026-09-10.md", "ALL GREEN\n", age_h=STALE_AFTER_H + 1)
    assert read_daily_check(tmp_path, now=NOW).stale
    _write(tmp_path, "2026-09-10.md", "ALL GREEN\n", age_h=STALE_AFTER_H - 1)
    assert not read_daily_check(tmp_path, now=NOW).stale


def test_stale_file_flips_red(tmp_path):
    _write(tmp_path, "2026-09-10.md", "ALL GREEN\n", age_h=50.0)
    c = read_daily_check(tmp_path, now=NOW)
    assert c.stale and c.red and "STALE" in render_daily_check(c, THEME)


def test_counts_extracted(tmp_path):
    _write(tmp_path, "2026-09-10.md",
           "1 ITEM\n- memd: down\n- disk: full\n- memd: stale\n")
    c = read_daily_check(tmp_path, now=NOW)
    assert c.counts.get("memd") == 2 and "memd:2" in render_daily_check(c, THEME)


def test_default_dir_shape():
    assert str(default_daily_check_dir()).endswith("randomesh/daily-check")
