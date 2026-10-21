"""Parallel + cached fleet probes.

`meshmon.snapshot()` and `meshsrv.snapshot()` are called from
`collect_dashboard()`, which the render path reaches on every `_render_all()`
and every `_tick()`. Both fanned out SERIALLY over blocking `subprocess.run`
ssh, so a render cost the sum of every node's connect timeout — measured at
27.7s (meshmon) + 44.2s (meshsrv) on this box with three of five nodes down.

Two fixes, both here so every remote collector gets them:

    fan_out()   run the per-target probes concurrently instead of in series
    cached()    answer repeated calls from a short TTL instead of re-probing

The TTL is deliberate rather than a lie: uptime/load/disk freshness matters
for a HUD, so a 10s window bounds the worst-case staleness while turning a
render storm (header+rail+center+right+bottom, plus per-intent and per-tick
redraws) from N probes into 1. Set AION_PROBE_TTL=0 to disable caching.

A cache keyed on nothing but time would be a test bug waiting to happen: a
test injecting a fake transport after a real probe would read the real
result. `cached()` therefore never caches when the caller passes its own
transport — the production path always uses the module default.

Ponytail: a wall-clock TTL rather than invalidation-on-state-change. The HUD
has no cheap "fleet changed" signal; upgrade if one appears.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

DEFAULT_TTL = 10.0


def ttl(default: float = DEFAULT_TTL) -> float:
    """Seconds a probe result stays reusable. 0 disables caching."""
    raw = os.environ.get("AION_PROBE_TTL", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default          # a typo must not take the HUD down
    return max(0.0, value)


def fan_out(fn: Callable[[T], R], items: Iterable[T], workers: int = 8) -> list[R]:
    """Apply `fn` to every item concurrently, preserving input order.

    Used for per-node probes so the wall clock is the SLOWEST probe, not the
    sum. Probes soft-fail per node, so a raising `fn` is reported in place
    rather than cancelling its siblings.
    """
    items = list(items)
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        futures = [pool.submit(fn, i) for i in items]
        out: list[R] = []
        for fut in futures:
            try:
                out.append(fut.result())
            except Exception as e:  # keep order, keep going
                out.append(e)      # type: ignore[arg-type]
        return out


def cached(key: str, produce: Callable[[], Any], *,
           default_transport: Any, given_transport: Any,
           max_age: float | None = None) -> Any:
    """Return `produce()`, memoised for `max_age` seconds under `key`.

    Skips the cache whenever `given_transport` is not `default_transport`: a
    caller-supplied transport means a test (or a one-off probe) that must see
    its own result, never a previous real-network answer.

    `produce` is a CALLBACK, never a pre-built value — passing a computed dict
    would evaluate the expensive work at the call site, before this function
    ever gets to decide whether to skip it. That mistake makes the cache a
    no-op while still paying full price.
    """
    window = ttl() if max_age is None else max_age
    if given_transport is not default_transport or window <= 0:
        return produce()
    store = _CACHE.setdefault(key, {})
    now = time.monotonic()
    hit = store.get("value", _MISS)
    if hit is not _MISS and (now - store["at"]) < window:
        return hit
    value = produce()
    store["value"] = value
    store["at"] = now
    return value


_CACHE: dict[str, dict] = {}
_MISS = object()   # sentinel: a collector may legitimately return None, and
                   # store.get("value") cannot tell that from "no entry yet"


def invalidate(key: str | None = None) -> None:
    """Drop cached probe results (all, or one key). For tests and /refresh."""
    if key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key, None)
