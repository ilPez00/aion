"""Tests for the Mesh NAS backend pure logic (no network)."""
from aion.nas import _parse_df_line, NasSnapshot


def test_parse_df_line_ok():
    line = "pansa-ts:/mnt/bigstore 916G 200G 716G 22% /mnt/bigstore"
    s = _parse_df_line(line, "/mnt/bigstore", "bigstore")
    assert s.mounted is True
    assert s.name == "bigstore"
    assert s.total_gb == 916.0
    assert s.used_gb == 200.0
    assert s.avail_gb == 716.0
    assert s.used_pct == 22
    assert s.health == "ok"


def test_parse_df_line_warn_at_90():
    line = "pansa-ts:/mnt/bigstore 916G 880G 36G 96% /mnt/bigstore"
    s = _parse_df_line(line, "/mnt/bigstore", "bigstore")
    assert s.used_pct == 96
    assert s.health == "fail"


def test_parse_df_line_short_is_unreachable():
    s = _parse_df_line("garbage", "/mnt/bigstore", "bigstore")
    assert s.mounted is False
    assert s.health == "unreachable"


def test_snapshot_dict_shape():
    snap = NasSnapshot(reachable=True, note="ok")
    d = snap.as_dict()
    assert d["reachable"] is True
    assert "shares" in d
