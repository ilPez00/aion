#!/usr/bin/env python3
"""mesh_agg_collect.py — per-node aggregation collector (stdlib only).

Run ON a mesh node (over SSH via `python3 - < this file`, or directly). It
reads every "agentic session / memory / document" store aion's control plane
wants to aggregate and prints ONE JSON document to stdout:

    {
      "host": str,
      "generated_at": float,
      "memory":  [{"kind":"hermes","section":i,"body":str,"preview":str}, ...],
      "sessions":[{"harness":"hermes|opencode|aion","id":str,"title":str,
                   "href":str,"state":str,"created":float,"preview":str}, ...],
      "docs":    [{"kind":"vault|note","path":str,"title":str,"preview":str},...],
      "counts":  {"memory":int,"sessions":int,"docs":int}
    }

Never raise: a missing/invalid store contributes nothing, never fails the run.
This is intentionally read-only — it never writes a byte to the node.

Intended to be invoked by the controlling aion cockpit over SSH. Kept fully
self-contained (no `import aion`) so it can run on any fleet node that merely
has python3, regardless of whether aion is installed there.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path.home()


def _preview(text: str, n: int = 140) -> str:
    text = (text or "").strip()
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ── memory ────────────────────────────────────────────────────────────────
def collect_memory() -> list[dict]:
    out: list[dict] = []
    # Hermes MEMORY.md (section-separated) — the durable cross-session memory.
    mem = HOME / ".hermes" / "memories" / "MEMORY.md"
    if mem.exists():
        try:
            parts = mem.read_text().split("\n§\n")
            out.extend(
                {"kind": "hermes", "section": i, "body": p.strip(),
                 "preview": _preview(p)}
                for i, p in enumerate(parts) if p.strip()
            )
        except OSError:
            pass
    # aion shared memory.json (freeform name→fact).
    aion_mem = HOME / ".aion" / "shared" / "memory.json"
    if aion_mem.exists():
        try:
            data = json.loads(aion_mem.read_text())
            if isinstance(data, dict):
                out.extend(
                    {"kind": "aion", "section": i, "body": f"{k}: {v}",
                     "preview": _preview(f"{k}: {v}")}
                    for i, (k, v) in enumerate(data.items())
                    if isinstance(v, (str, int, float, bool))
                )
        except (OSError, ValueError):
            pass
    return out


# ── sessions ──────────────────────────────────────────────────────────────
def collect_hermes_sessions() -> list[dict]:
    """Hermes request dumps + aion source of truth (shared session.json)."""
    out: list[dict] = []
    sdir = HOME / ".hermes" / "sessions"
    if sdir.is_dir():
        for p in sorted(sdir.glob("*.json"), key=lambda f: f.stat().st_mtime,
                        reverse=True)[:300]:
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                sid = str(data.get("session_id", p.stem))
                title = (data.get("plain_text")
                         or str(data.get("purpose", "")) or data.get("model", "")
                         or p.stem)[:120]
                preview = _preview(json.dumps(data.get("qualitative_synthetic_checked") or "", ensure_ascii=False)
                                   or " ".join(str(data.get(k, "")) for k in ("model", "platform", "reason"))[:140])
                out.append({
                    "harness": "hermes", "id": sid, "title": title,
                    "href": str(p), "state": "dump", "created": p.stat().st_mtime,
                    "preview": preview or title,
                })
    # aion session.json (instance state) — source of truth for running tasks.
    for sess in (HOME / ".aion" / "instances").glob("*/session.json") if (HOME / ".aion" / "instances").exists() else []:
        try:
            data = json.loads(sess.read_text())
            tasks = data.get("tasks", []) if isinstance(data, dict) else []
            for t in tasks if isinstance(tasks, list) else []:
                if isinstance(t, dict):
                    out.append({
                        "harness": "aion", "id": str(t.get("id", sess.stem)),
                        "title": str(t.get("label", ""))[:120],
                        "href": str(sess), "state": str(t.get("state", "")),
                        "created": float(t.get("created", 0) or 0),
                        "preview": _preview(" ".join(t.get("log", [])[-2:])),
                    })
        except (OSError, ValueError):
            pass
    return out


def collect_opencode_sessions() -> list[dict]:
    """OpenCode sessions + message previews from its SQLite store."""
    out: list[dict] = []
    db = HOME / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        cur = con.cursor()
        # sessions table — column names differ slightly across versions; probe.
        sess_cols = [r[1] for r in cur.execute(
            "PRAGMA table_info(session)").fetchall()]
        sname = "title" if "title" in sess_cols else "id"
        rows = cur.execute(
            f"SELECT id, {sname} FROM session ORDER BY time_updated DESC LIMIT 150"
        ).fetchall() if "time_updated" in sess_cols else \
            cur.execute(f"SELECT id, {sname} FROM session LIMIT 150").fetchall()
        for sid, title in rows:
            out.append({
                "harness": "opencode", "id": str(sid),
                "title": (str(title or "")[:120]),
                "href": f"opencode://{sid}", "state": "session",
                "created": 0.0, "preview": "",
            })
        con.close()
    except Exception:
        pass
    return out


# ── documents / notes ─────────────────────────────────────────────────────
def collect_docs() -> list[dict]:
    out: list[dict] = []
    # aion vault notes (shared across instances).
    vault = HOME / ".aion" / "shared" / "vault"
    if vault.is_dir():
        for p in sorted(vault.glob("**/*.md"))[:200]:
            try:
                body = p.read_text()
            except OSError:
                continue
            out.append({"kind": "vault", "path": str(p.relative_to(HOME)),
                        "title": p.stem, "preview": _preview(body)})
    # Hermes skills tree (documents: SKILL.md files).
    skills = HOME / ".hermes" / "skills"
    if skills.is_dir():
        for p in sorted(skills.glob("**/SKILL.md"))[:150]:
            try:
                body = p.read_text()
            except OSError:
                continue
            out.append({"kind": "skill", "path": str(p.relative_to(HOME)),
                        "title": p.parent.name, "preview": _preview(body)})
    return out


# ── kanban (tasks as documents of record) ─────────────────────────────────
def collect_kanban() -> list[dict]:
    out: list[dict] = []
    db = HOME / ".hermes" / "kanban.db"
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        cur = con.cursor()
        cols = [r[1] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()]
        if "title" in cols and "status" in cols:
            rows = cur.execute(
                "SELECT id, title, status FROM tasks ORDER BY rowid DESC LIMIT 100"
            ).fetchall()
            for tid, title, status in rows:
                out.append({"harness": "kanban", "id": f"kanban-{tid}",
                            "title": str(title)[:120], "href": str(db),
                            "state": str(status), "created": 0.0,
                            "preview": _preview(str(title))})
        con.close()
    except Exception:
        pass
    return out


def main() -> int:
    memory = collect_memory()
    sessions = (collect_hermes_sessions() + collect_opencode_sessions()
                + collect_kanban())
    docs = collect_docs()
    print(json.dumps({
        "host": os.uname().nodename,
        "generated_at": time.time(),
        "memory": memory,
        "sessions": sessions,
        "docs": docs,
        "counts": {"memory": len(memory), "sessions": len(sessions),
                   "docs": len(docs)},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())