"""The render path must not pay a serial SSH fan-out per redraw.

Regression guard for the freeze measured on pansa: collect_dashboard() ->
meshmon.snapshot() / meshsrv.snapshot() ran every fleet node sequentially
over blocking subprocess, so one _render_all() cost 27.7s + 44.2s with three
nodes offline, and the Textual pilot gave up (WaitForScreenTimeout).

Two properties, both asserted here rather than asserted in a comment:
  1. fan_out overlaps, so N slow probes cost ONE probe, not N
  2. a repeated production-path probe is served from cache, not re-run
"""
import time

from aion import probecache


def test_fan_out_overlaps_instead_of_summing():
    calls = []

    def slow(item):
        calls.append(item)
        time.sleep(0.20)
        return item.upper()

    t0 = time.perf_counter()
    out = probecache.fan_out(slow, list("abcde"))
    elapsed = time.perf_counter() - t0

    assert out == ["A", "B", "C", "D", "E"], "order must be preserved"
    assert len(calls) == 5, "every item must be probed"
    # serial would be >= 1.0s; overlapped is ~0.2s
    assert elapsed < 0.6, f"fan-out ran serially: {elapsed:.2f}s for 5x0.2s"


def test_fan_out_reports_a_raising_item_without_losing_siblings():
    def flaky(item):
        if item == "b":
            raise RuntimeError("probe blew up")
        return item

    out = probecache.fan_out(flaky, ["a", "b", "c"])
    assert out[0] == "a" and out[2] == "c"
    assert isinstance(out[1], RuntimeError)


def test_fan_out_handles_empty_and_single():
    assert probecache.fan_out(lambda x: x, []) == []
    assert probecache.fan_out(lambda x: x, ["only"]) == ["only"]


def test_cached_runs_produce_once_within_the_window():
    probecache.invalidate("t.produce")
    runs = []

    def produce():
        runs.append(1)
        return {"v": len(runs)}

    default = object()
    a = probecache.cached("t.produce", produce,
                          default_transport=default, given_transport=default)
    b = probecache.cached("t.produce", produce,
                          default_transport=default, given_transport=default)
    assert a == b == {"v": 1}
    assert len(runs) == 1, "second call must come from cache"


def test_cached_stores_a_none_result_instead_of_re_probing():
    """A collector returning None is a real result, not a cache miss.
    Guard the sentinel: `store.get("value")` alone silently re-probes
    forever for any None-returning backend."""
    probecache.invalidate("t.none")
    runs = []
    default = object()

    def produce():
        runs.append(1)
        return None

    for _ in range(3):
        assert probecache.cached("t.none", produce,
                                 default_transport=default,
                                 given_transport=default) is None
    assert len(runs) == 1, f"None result must still be cached, ran {len(runs)}x"


def test_cached_expires_after_the_window():
    probecache.invalidate("t.expire")
    default = object()
    runs = []
    for _ in range(3):
        probecache.cached("t.expire", lambda: runs.append(1),
                          default_transport=default, given_transport=default,
                          max_age=0.05)
        time.sleep(0.06)
    assert len(runs) == 3, "an expired entry must be re-probed"


def test_cached_never_serves_a_previous_result_to_an_injected_transport():
    """A test (or one-off probe) supplying its own transport must see its own
    answer, not a cached real-network result from an earlier call."""
    probecache.invalidate("t.injected")
    default = object()
    fake = object()

    probecache.cached("t.injected", lambda: "real",
                      default_transport=default, given_transport=default)
    got = probecache.cached("t.injected", lambda: "fake",
                            default_transport=default, given_transport=fake)
    assert got == "fake"


def test_invalidate_drops_one_key_or_all():
    d = object()
    probecache.cached("t.a", lambda: 1, default_transport=d, given_transport=d)
    probecache.cached("t.b", lambda: 2, default_transport=d, given_transport=d)
    probecache.invalidate("t.a")
    assert "t.a" not in probecache._CACHE and "t.b" in probecache._CACHE
    probecache.invalidate()
    assert probecache._CACHE == {}


def test_ttl_zero_disables_caching(monkeypatch):
    monkeypatch.setenv("AION_PROBE_TTL", "0")
    assert probecache.ttl() == 0.0
    d = object()
    runs = []
    for _ in range(3):
        probecache.cached("t.zero", lambda: runs.append(1),
                          default_transport=d, given_transport=d)
    assert len(runs) == 3, "ttl=0 must re-probe every time"


def test_ttl_ignores_a_typo_instead_of_raising(monkeypatch):
    monkeypatch.setenv("AION_PROBE_TTL", "not-a-number")
    assert probecache.ttl() == probecache.DEFAULT_TTL
