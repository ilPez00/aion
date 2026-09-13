"""fleetview.py — one Fleet view-model over nodes, services, sessions, models.

The merged Fleet workspace needs a single data shape combining four sources
that today render in different places (or not at all):

  * nodes    — meshmon.snapshot() (SSH load/ram/disk per box)
  * services — meshsrv.snapshot() (tcp/unit/http probes + lifecycle cmds)
  * sessions — fleettask rows (agent-task queue: queued/running vs terminal)
  * models   — fleet-models.json (roles/caps/resources per model)

Collectors are injected (default to the real ones, lazily imported so tests
never touch the network), and every source soft-fails to an empty section —
one dead source must never blank the whole workspace.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

DEFAULT_MODELS = Path.home() / "dev/randomesh/fleet-models.json"


def _safe(fn: Callable[[], Any], fallback: Any) -> Any:
    try:
        return fn()
    except Exception:
        return fallback


def _real_nodes() -> dict:
    from . import meshmon
    return meshmon.snapshot()


def _real_services() -> dict:
    from . import meshsrv
    return meshsrv.snapshot()


def _real_sessions() -> list[dict]:
    from . import fleettask
    return [t.as_dict() for t in fleettask.read_local_tasks()]


def load_models(path: str | Path = DEFAULT_MODELS) -> list[dict]:
    """fleet-models.json models list; malformed/absent → [] (never raises)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    models = data.get("models", []) if isinstance(data, dict) else []
    return [m for m in models if isinstance(m, dict) and m.get("id")]


def collect(nodes_fn: Callable[[], dict] = _real_nodes,
            services_fn: Callable[[], dict] = _real_services,
            sessions_fn: Callable[[], list[dict]] = _real_sessions,
            models_path: str | Path = DEFAULT_MODELS) -> dict:
    """Assemble the Fleet view-model. Pure given its collectors."""
    nodes = _safe(nodes_fn, {"total": 0, "reachable": 0, "nodes": []})
    services = _safe(services_fn, {"total": 0, "up": 0, "services": []})
    sessions = _safe(sessions_fn, [])
    models = load_models(models_path)

    node_list = nodes.get("nodes", []) or []
    svc_list = services.get("services", []) or []
    live = [s for s in sessions if not s.get("terminal", False)]

    by_role: dict[str, int] = {}
    for m in models:
        if m.get("enabled", True):
            for r in m.get("roles", []) or []:
                by_role[r] = by_role.get(r, 0) + 1

    return {
        "nodes": {"total": nodes.get("total", len(node_list)),
                  "reachable": nodes.get("reachable", 0),
                  "rows": node_list},
        "services": {"total": services.get("total", len(svc_list)),
                     "up": services.get("up", 0),
                     "rows": svc_list},
        "sessions": {"total": len(sessions), "live": len(live),
                     "rows": sessions},
        "models": {"total": len(models), "by_role": by_role,
                   "rows": [{"id": m.get("id", ""), "node": m.get("node", ""),
                             "roles": m.get("roles", []),
                             "enabled": m.get("enabled", True)}
                            for m in models]},
    }


def summary(view: dict) -> str:
    """One glanceable line for the workspace header."""
    n, s, t, m = (view["nodes"], view["services"], view["sessions"],
                  view["models"])
    return (f"nodes {n['reachable']}/{n['total']} · "
            f"services {s['up']}/{s['total']} · "
            f"sessions {t['live']} live/{t['total']} · "
            f"models {m['total']}")
