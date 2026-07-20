"""aion glue over the vendored hermes-hud collectors.

Aggregates the Textual-free collectors (joeynyc/hermes-hud, MIT) into one
plain dict the `mind` workspace renders. Runs off the event loop (MindHarness
calls it via asyncio.to_thread) — every collector here does blocking file /
sqlite reads over ~/.hermes/.
"""
from __future__ import annotations

import re

from .collectors.config import collect_config
from .collectors.corrections import collect_corrections
from .collectors.memory import collect_memory
from .collectors.patterns import collect_patterns
from .collectors.sessions import collect_sessions


def _oneline(text: str) -> str:
    """Collapse whitespace and neutralize Textual markup brackets.

    Prompt/correction text is arbitrary user content; a stray '[' or ']' is
    parsed as a markup tag and raises MarkupError in the render. Swap the
    square brackets for round ones so the HUD stays readable and safe.
    """
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.replace("[", "(").replace("]", ")")


def collect_mind(hermes_dir: str | None = None) -> dict:
    """One blocking pass over ~/.hermes/. Returns a render-ready dict."""
    cfg = collect_config(hermes_dir)
    mem, user = collect_memory(hermes_dir, cfg.memory_char_limit, cfg.user_char_limit)
    sessions = collect_sessions(hermes_dir)
    patterns = collect_patterns(hermes_dir)
    corrections = collect_corrections(hermes_dir)

    return {
        "ok": True,
        "config": {
            "provider": cfg.provider,
            "model": cfg.model,
            "backend": cfg.backend,
        },
        "memory": {
            "entries": mem.entry_count,
            "chars": mem.total_chars,
            "max_chars": mem.max_chars,
            "pct": round(mem.capacity_pct, 1),
            "categories": mem.count_by_category(),
        },
        "user": {
            "entries": user.entry_count,
            "pct": round(user.capacity_pct, 1),
        },
        "sessions": {
            "total": sessions.total_sessions,
            "messages": sessions.total_messages,
            "tool_calls": sessions.total_tool_calls,
            "daily": [(d.date, d.messages) for d in sessions.daily_stats[-14:]],
            "top_tools": sorted(
                sessions.tool_usage.items(), key=lambda x: -x[1]
            )[:6],
        },
        "clusters": [
            {"label": c.label, "count": c.count, "avg_tools": round(c.avg_tool_calls, 1)}
            for c in patterns.clusters[:6]
        ],
        "skill_candidates": [
            {"pattern": _oneline(r.pattern), "count": r.count}
            for r in patterns.repeated_prompts
            if r.could_be_skill
        ][:5],
        "workflows": [
            {"seq": w.tool_sequence, "count": w.count}
            for w in patterns.tool_workflows[:4]
        ],
        "corrections": {
            "total": corrections.total,
            "by_severity": corrections.by_severity(),
            "recent": [
                {"summary": _oneline(c.summary), "severity": c.severity, "source": c.source}
                for c in corrections.corrections[:5]
            ],
        },
    }
