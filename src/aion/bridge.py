"""bridge.py — everything the TUI cockpit shows, readable from the web HUD.

The cockpit and the web HUD are separate processes. The cockpit's workspaces
(Desktop, Tasks, Vault, Settings, …) read live objects in memory; the HUD
cannot. What it *can* read is the same shared state those objects persist to
`~/.aion/shared/` — todos, memory facts, boards, agent entities — plus the
things that are derived rather than stored (installed apps, operational modes,
which provider keys are present).

This module is that bridge. One import surface, everything fails soft: a
missing or corrupt store yields an empty section with an `error`, never an
exception, because one broken file must not blank the whole HUD.

What genuinely cannot cross the boundary
----------------------------------------
Some cockpit state is inherently in-process and is NOT faked here:

  * spawning a task on a harness — needs the running cockpit, so the web HUD
    routes it instead (`routing.py` → the cockpit's `/run`)
  * HITL approval gates — `GateBook` lives in the cockpit's memory
  * the live operational mode — set on a running cockpit

Those are reported as capabilities the HUD can ask the cockpit to perform,
rather than pretended to be local state.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Provider env vars worth reporting on the Settings surface. Only presence is
# ever exposed — never the value, since this HUD is LAN-reachable.
PROVIDER_KEYS = (
    ("GROQ_API_KEY", "Groq"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("DEEPSEEK_API_KEY", "DeepSeek"),
    ("OPENROUTER_API_KEY", "OpenRouter"),
    ("GEMINI_API_KEY", "Gemini"),
)


def _jsonable(value: Any) -> Any:
    """Coerce a value into something `json.dumps` accepts.

    Dataclasses in this codebase legitimately hold `Path` objects (SkillInfo
    keeps the file it was loaded from). Serialising the HTTP response is the
    wrong place to discover that, so it is flattened here where the shape is
    known.
    """
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _soft(fn, default):
    """Run `fn`, returning `default` plus an error string on any failure."""
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001
        return default, f"{type(e).__name__}: {str(e)[:160]}"


# ── todos ────────────────────────────────────────────────────────────────
def todos() -> dict:
    def read():
        from .todos import TodoStore
        return TodoStore().items()
    items, err = _soft(read, [])
    return {"items": items, "error": err,
            "open": sum(1 for i in items if not i.get("done"))}


# The underlying stores index from 1 (they were written for `todo done 3`
# typed at a prompt) and return False rather than raising when the index is
# out of range. Everything above this line is 0-based, because the browser
# enumerates a list. The conversion happens here, once, at the boundary —
# and a rejected index is surfaced instead of silently doing nothing.
def _one_based(index: int) -> int:
    return int(index) + 1


def todo_add(text: str) -> dict:
    from .todos import TodoStore
    TodoStore().add(text)
    return todos()


def todo_done(index: int) -> dict:
    from .todos import TodoStore
    ok = TodoStore().done(_one_based(index))
    out = todos()
    if not ok:
        out["error"] = f"no todo at position {index}"
    return out


def todo_remove(index: int) -> dict:
    from .todos import TodoStore
    ok = TodoStore().rm(_one_based(index))
    out = todos()
    if not ok:
        out["error"] = f"no todo at position {index}"
    return out


# ── memory ───────────────────────────────────────────────────────────────
def memory(query: str = "") -> dict:
    """Facts in the SAME order `MemoryStore.forget` indexes into.

    `store.facts` is insertion order; `store.search()` is newest-first, and
    `forget()` resolves its index against the latter. Returning `facts` here
    meant the position the UI showed and the position the store deleted were
    different lists — so forgetting the top fact removed the oldest one.
    """
    def read():
        from .memory import MemoryStore
        st = MemoryStore()
        st.query = query
        return list(st.search(query))
    facts, err = _soft(read, [])
    return {"facts": facts, "query": query, "error": err, "count": len(facts)}


def memory_add(text: str) -> dict:
    from .memory import MemoryStore
    MemoryStore().add(text)
    return memory()


def memory_forget(index: int, query: str = "") -> dict:
    from .memory import MemoryStore
    st = MemoryStore()
    st.query = query              # forget() indexes into the filtered view
    ok = st.forget(_one_based(index))
    out = memory(query)
    if not ok:
        out["error"] = f"no fact at position {index}"
    return out


# ── boards (Tasks workspace kanban) ──────────────────────────────────────
def boards() -> dict:
    def read():
        from .board import BoardStore
        st = BoardStore()
        return [b.as_dict() for b in st.list_all()]
    got, err = _soft(read, [])
    return {"boards": got, "error": err}


# ── agent entities (Agent workspace cards) ───────────────────────────────
def agent_entities() -> dict:
    def read():
        from .agents import AgentStore
        return [a.as_dict() for a in AgentStore().list_all()]
    got, err = _soft(read, [])
    return {"agents": got, "error": err}


# ── apps launcher (Desktop workspace) ────────────────────────────────────
def apps() -> dict:
    def read():
        from . import apps as appmod
        return appmod.availability()
    got, err = _soft(read, [])
    return {"apps": got, "error": err,
            "installed": sum(1 for a in got if a.get("available"))}


# ── operational modes ────────────────────────────────────────────────────
def modes() -> dict:
    def read():
        from . import modes as modemod
        out = []
        for m in modemod.list_modes():
            out.append({
                "id": getattr(m, "id", getattr(m, "name", "?")),
                "name": getattr(m, "name", ""),
                "description": getattr(m, "description", ""),
            })
        return out
    got, err = _soft(read, [])
    return {"modes": got, "error": err}


# ── profile / trackers (Desktop DATA panel) ──────────────────────────────
def profile() -> dict:
    def read():
        from . import profile as prof
        return prof.load() or {}
    got, err = _soft(read, {})
    return {"profile": got, "error": err}


# ── settings surface ─────────────────────────────────────────────────────
def providers() -> list[dict]:
    """Which model providers are configured. Presence only, never the key."""
    return [{"env": env, "name": name, "present": bool(os.environ.get(env))}
            for env, name in PROVIDER_KEYS]


def skills() -> dict:
    def read():
        from .hermes.skills import SkillLoader
        return [_jsonable(sk) for sk in SkillLoader().list_all()]
    got, err = _soft(read, [])
    return {"skills": got, "error": err}


def settings() -> dict:
    prov = providers()
    sk = skills()
    return {
        "providers": prov,
        "configured": sum(1 for p in prov if p["present"]),
        "skills": sk["skills"],
        "skills_error": sk["error"],
        "paths": paths(),
    }


def paths() -> dict:
    """Where aion keeps things — the first question when something is missing."""
    from .fleet import AION_HOME, instance_root, shared_root
    def s(p):
        try:
            return str(p)
        except Exception:  # noqa: BLE001
            return "?"
    return {
        "home": s(AION_HOME),
        "shared": s(shared_root()),
        "instance": s(instance_root()),
        "vault": os.environ.get("AION_VAULT", ""),
        "fs_root": os.environ.get("AION_FS_ROOT", os.path.expanduser("~")),
    }


# ── the whole desktop, in one call ───────────────────────────────────────
def desktop() -> dict:
    """Everything the cockpit's Desktop workspace shows.

    One request rather than six: the HUD renders this as a single surface, and
    six round trips to paint one screen is how a dashboard feels slow.
    """
    return {
        "todos": todos(),
        "memory": memory(),
        "apps": apps(),
        "modes": modes(),
        "profile": profile(),
        "agents": agent_entities(),
        "boards": boards(),
    }


def search(query: str, limit: int = 20) -> list[dict]:
    """Palette hits across todos, memory facts, boards and apps."""
    q = (query or "").strip().lower()
    if not q:
        return []
    hits: list[dict] = []
    for i, t in enumerate(todos()["items"]):
        if q in t.get("text", "").lower():
            hits.append({"type": "todo", "label": t["text"],
                         "sub": "done" if t.get("done") else "open",
                         "module": "desk", "node": f"todo{i}"})
    for i, f in enumerate(memory()["facts"]):
        if q in str(f.get("text", "")).lower():
            hits.append({"type": "fact", "label": str(f["text"])[:80],
                         "sub": "memory", "module": "desk", "node": f"fact{i}"})
    for b in boards()["boards"]:
        for c in b.get("cards", []):
            if q in str(c.get("title", "")).lower():
                hits.append({"type": "card", "label": c["title"],
                             "sub": f"{b.get('title', '')} · {c.get('column', '')}",
                             "module": "board", "node": f"c{c.get('id')}"})
    for a in apps()["apps"]:
        if q in str(a.get("id", "")).lower() or q in str(a.get("name", "")).lower():
            hits.append({"type": "app", "label": a.get("name") or a.get("id"),
                         "sub": "installed" if a.get("available") else "not installed",
                         "module": "desk", "node": f"app{a.get('id')}"})
    return hits[:limit]
