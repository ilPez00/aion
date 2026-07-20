"""Collect session data from Hermes state.db.

Works against any machine in the node registry: the db is resolved through
`Node.fetch`, which is the identity function locally and a cached scp for a
remote node. sqlite is never pointed at a network path — see nodes.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from ....nodes import LOCAL, Node, NodeRegistry
from ..models import DailyStats, SessionInfo, SessionsState
from .utils import default_hermes_dir, parse_timestamp, safe_get


def _extract_tool_usage(db_path: str) -> dict[str, int]:
    """Extract tool usage counts from tool_calls JSON in messages."""
    usage: dict[str, int] = {}
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL AND tool_calls != ''"
        )
        for (tc_json,) in cursor.fetchall():
            try:
                calls = json.loads(tc_json)
                if isinstance(calls, list):
                    for call in calls:
                        fn = call.get("function", {})
                        name = fn.get("name", "unknown")
                        usage[name] = usage.get(name, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        conn.close()
    except Exception:
        pass
    return usage


def collect_sessions(hermes_dir: str | None = None,
                     node: Node = LOCAL) -> SessionsState:
    """Collect session data from state.db on `node` (default: this machine)."""
    if hermes_dir is None:
        # A remote node's HERMES_HOME is not ours: only trust the env var
        # locally, and fall back to ~/.hermes resolved against its own home.
        hermes_dir = default_hermes_dir(None) if node.is_local else "~/.hermes"

    remote_db = str(Path(hermes_dir) / "state.db")
    local_db = node.fetch(remote_db)
    if local_db is None:
        # Missing file and unreachable host look the same from here; only a
        # remote node can be "down", so that is the only case we flag.
        return SessionsState(node=node.label, unreachable=not node.is_local)
    db_path = str(local_db)
    if not os.path.exists(db_path):
        return SessionsState(node=node.label)

    sessions: list[SessionInfo] = []
    daily_stats: list[DailyStats] = []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Fetch all sessions
        cursor.execute("""
            SELECT id, source, title, started_at, ended_at,
                   message_count, tool_call_count,
                   input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens,
                   reasoning_tokens, estimated_cost_usd, model, model_config
            FROM sessions
            ORDER BY started_at DESC
        """)

        for row in cursor.fetchall():
            try:
                # Handles unix int/float and ISO strings; rows without a
                # usable start time are skipped rather than dated 1970.
                started = parse_timestamp(safe_get(row, "started_at"))
                if started is None:
                    continue
                ended = parse_timestamp(safe_get(row, "ended_at"))

                # Try to extract model from model_config JSON
                model = safe_get(row, "model")
                mc_raw = safe_get(row, "model_config")
                if not model and mc_raw:
                    try:
                        mc = json.loads(mc_raw)
                        model = mc.get("model") or mc.get("default")
                    except (json.JSONDecodeError, TypeError):
                        pass

                sessions.append(SessionInfo(
                    id=safe_get(row, "id", ""),
                    source=safe_get(row, "source", "unknown"),
                    title=safe_get(row, "title"),
                    started_at=started,
                    ended_at=ended,
                    message_count=safe_get(row, "message_count", 0),
                    tool_call_count=safe_get(row, "tool_call_count", 0),
                    input_tokens=safe_get(row, "input_tokens", 0),
                    output_tokens=safe_get(row, "output_tokens", 0),
                    cache_read_tokens=safe_get(row, "cache_read_tokens", 0),
                    cache_write_tokens=safe_get(row, "cache_write_tokens", 0),
                    reasoning_tokens=safe_get(row, "reasoning_tokens", 0),
                    estimated_cost_usd=safe_get(row, "estimated_cost_usd", 0.0),
                    model=model,
                    node=node.label,
                ))
            except Exception:
                continue

        # Daily stats
        cursor.execute("""
            SELECT date(started_at, 'unixepoch') as day,
                   COUNT(*) as sessions,
                   SUM(message_count) as msgs,
                   SUM(tool_call_count) as tools,
                   SUM(input_tokens + output_tokens) as tokens
            FROM sessions
            GROUP BY day
            ORDER BY day
        """)

        for row in cursor.fetchall():
            try:
                day = safe_get(row, "day", "")
                if not day:
                    continue  # NULL/non-numeric started_at yields no date bucket
                daily_stats.append(DailyStats(
                    date=day,
                    sessions=safe_get(row, "sessions", 0),
                    messages=safe_get(row, "msgs", 0),
                    tool_calls=safe_get(row, "tools", 0),
                    tokens=safe_get(row, "tokens", 0),
                ))
            except Exception:
                continue

        conn.close()
    except Exception as e:
        # Return what we have
        pass

    # Tool usage
    tool_usage = _extract_tool_usage(db_path)

    return SessionsState(
        sessions=sessions,
        daily_stats=daily_stats,
        tool_usage=tool_usage,
        node=node.label,
    )


def collect_sessions_multi(registry: NodeRegistry,
                           hermes_dir: str | None = None,
                           ) -> dict[str, SessionsState]:
    """Sessions from every enabled node, keyed by node label.

    Fetches run in parallel: one slow or dead node must not stall the HUD
    behind the others. Each node's failure is contained in its own state.
    """
    from concurrent.futures import ThreadPoolExecutor

    nodes = registry.all()
    if len(nodes) == 1:
        return {nodes[0].label: collect_sessions(hermes_dir, nodes[0])}

    with ThreadPoolExecutor(max_workers=min(len(nodes), 8)) as pool:
        states = pool.map(lambda n: collect_sessions(hermes_dir, n), nodes)
    return {n.label: s for n, s in zip(nodes, states)}
