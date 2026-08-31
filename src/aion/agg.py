"""agg.py — cross-machine aggregation of agent sessions, memories & documents.

The RandoMesh control plane needs a single place to SEE what every agentic
harness (Hermes, OpenCode, aion, kanban) did on every machine. Rather than
ssh-ing into each box ad hoc — and re-parsing schemas every time — this module
collects each node's stores into ONE local SQLite FTS5 database on the cockpit
host, and exposes a small query API the CLI (`aion mesh agg ...`) and the HUD
(mesh workspace) both read.

Design (mirrors meshsrv/meshmon):
  - Pure logic + injectable transport, so everything is unit-testable without
    the network.
  - The COLLECTOR is a separate stdlib script (`scripts/mesh_agg_collect.py`)
    that runs ON the target node over SSH and emits one JSON doc. This module
    only moves bytes + parses; it never re-implements a store's schema locally.
  - Soft-fail per node: a dead/unreachable node contributes nothing, never aborts.
  - The DB is append/reconcile: each collect upserts by (kind, source, id) so a
    re-run refreshes rows instead of duplicating them.

Row model (one FTS5-queryable index over everything):
    items(id, kind, node, source, ref, title, body, ts)
        kind   = "memory" | "session" | "doc"
        source = "hermes" | "aion" | "opencode" | "kanban" | "skill" | "vault"
        ref    = unique id within (kind, source) for upsert
        node   = hostname the data came from
        title  = short human label
        body   = the searchable text (memory body / preview / note body)
        ts     = unix time (0 if unknown, e.g. opencode sessions)

aion identity: this is a READ/AGGREGATE surface — it collects and searches, it
does not mutate the source stores or take ownership of the nodes' sessions.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Transport: (method, target, cmd) -> (rc, stdout). Mirrors meshsrv/meshmon.
Transport = Callable[[str, str, str], tuple[int, str]]

# Remote collector that runs ON a node. Path under the aion checkout on the
# cockpit host; its *content* is piped to the node, so the node needs no aion
# install — just python3.
COLLECTOR_REL = "scripts/mesh_agg_collect.py"
_COLLECTOR_CACHE: Optional[str] = None


def _collector_source() -> str:
    """The collector script's text, cached after first read."""
    global _COLLECTOR_CACHE
    if _COLLECTOR_CACHE is None:
        from .paths import checkout_root
        root = checkout_root()
        if root is None:
            raise RuntimeError("aion checkout not found; can't locate mesh_agg_collect.py")
        _COLLECTOR_CACHE = (root / COLLECTOR_REL).read_text(encoding="utf-8")
    return _COLLECTOR_CACHE


def default_db_path() -> Path:
    """Aggregation DB, overridable via AION_AGG_DB (tests use a tmp file)."""
    env = os.environ.get("AION_AGG_DB", "").strip()
    if env:
        return Path(env)
    from .fleet import shared_path
    root = shared_path("agg")
    root.mkdir(parents=True, exist_ok=True)
    return root / "agg.db"


_KIND_TO_TABLE = {"memory": "memory", "session": "session", "doc": "doc"}


def _connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            node TEXT, source TEXT, ref TEXT,
            title TEXT, body TEXT, ts REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_items_uniq
            ON items(kind, node, source, ref);
        CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
        CREATE INDEX IF NOT EXISTS idx_items_ts ON items(ts);
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
            title, body, content='items', content_rowid='id'
        );
        """
    )
    return con


# ── Native FTS5 reconciliation ────────────────────────────────────────────
# SQLite FTS5 external-content tables don't auto-update from base rows; we keep
# the FTS index in sync manually after each upsert batch by marking dirty ids.
def _sync_fts(con: sqlite3.Connection) -> None:
    # Drop + rebuild is simplest and cheap at this scale (thousands of rows).
    con.executescript(
        """
        INSERT INTO items_fts(items_fts)
        VALUES('delete-all');
        INSERT INTO items_fts(rowid, title, body)
            SELECT id, title, body FROM items;
        """
    )


def _upsert_item(con: sqlite3.Connection, kind: str, node: str,
                 source: str, ref: str, title: str, body: str, ts: float) -> None:
    con.execute(
        """
        INSERT INTO items(kind, node, source, ref, title, body, ts)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(kind, node, source, ref)
        DO UPDATE SET title=excluded.title, body=excluded.body, ts=excluded.ts
        """,
        (kind, node, source, ref, title, body, ts),
    )


# ── ingest one node's collector JSON ───────────────────────────────────────
def ingest_node_payload(db_path: str | Path, payload: dict) -> dict:
    """Reconcile one node's collector JSON into the DB. Returns row counts."""
    con = _connect(db_path)
    try:
        node = payload.get("host") or "unknown"
        memory_rows = sessions_rows = docs_rows = 0

        for i, m in enumerate(payload.get("memory", [])):
            _upsert_item(con, "memory", node, m.get("kind", "hermes"),
                         f"{node}:mem:{i}", m.get("preview", "")[:120],
                         m.get("body", "") or m.get("preview", ""),
                         float(m.get("ts", 0) or 0))
            memory_rows += 1

        for s in payload.get("sessions", []):
            _upsert_item(con, "session", node, s.get("harness", "?"),
                         f"{s.get('id', '')}", s.get("title", "")[:120],
                         s.get("preview", "") or s.get("title", ""),
                         float(s.get("created", 0) or 0))
            sessions_rows += 1

        for d in payload.get("docs", []):
            _upsert_item(con, "doc", node, d.get("kind", "vault"),
                         d.get("path", ""), d.get("title", "")[:120],
                         d.get("preview", "") or d.get("title", ""), 0.0)
            docs_rows += 1

        model_rows = 0
        for m in payload.get("models", []):
            path = m.get("path", "")
            size = m.get("size_bytes", 0) or 0
            # body holds searchable text: kind + size + vram hint + path tail
            body = (
                f"{m.get('kind', 'model')} {size / (1024**3):.2f}GB "
                f"vram_hint {m.get('vram_hint_gb', '?')}GB hint {m.get('hint', '')} "
                f"{path}"
            )
            _upsert_item(con, "model", node, m.get("kind", "gguf"), path,
                         Path(path).name[:120] if path else "model",
                         body, float(m.get("mtime", 0) or 0))
            model_rows += 1

        _sync_fts(con)
        con.commit()
        counts = payload.get("counts", {})
        return {
            "node": node,
            "ingested": {
                "memory": memory_rows, "sessions": sessions_rows,
                "docs": docs_rows, "models": model_rows,
                "total": memory_rows + sessions_rows + docs_rows + model_rows,
            },
            "reported": counts,
        }
    finally:
        con.close()


# ── transport: run the collector on a node over SSH ───────────────────────
def _ssh_transport(target: str, source: str) -> tuple[bool, str, str]:
    """Run the collector script on `target` via its python3. Returns
    (ok, stdout, stderr). Streams script text over stdin so the node only needs
    python3."""
    import subprocess
    try:
        p = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", target,
             "python3 -"],
            input=source, capture_output=True, text=True, timeout=120,
        )
        return p.returncode == 0, p.stdout, p.stderr
    except Exception:
        return False, "", "transport error"


def collect_node(db_path: str | Path, node: str, alias: str,
                 transport: Optional[Callable] = None) -> dict:
    """Collect ONE node and reconcile it into the DB. Never raises."""
    if transport is None:
        transport = _ssh_transport
    ok, out, err = transport(alias, _collector_source())
    if not ok or not out.strip():
        return {"node": node, "ok": False, "error": (err or "no output").strip()[-200:]}
    try:
        payload = json.loads(out)
    except ValueError:
        return {"node": node, "ok": False,
                "error": f"collector returned non-JSON: {out.strip()[:200]}"}
    result = ingest_node_payload(db_path, payload)
    result["ok"] = True
    return result


# ── fleet-level collect ────────────────────────────────────────────────────
def collect_all(db_path: str | Path | None = None,
                transport: Optional[Callable] = None,
                nodes: dict[str, str] | None = None) -> dict:
    """Collect every named node into the DB. `nodes` maps name->ssh alias;
    defaults to meshmon's node table. Returns per-node results + DB totals."""
    db_path = db_path or default_db_path()
    if nodes is None:
        from .meshmon import NODES
        nodes = NODES
    results: dict[str, dict] = {}
    for name, alias in nodes.items():
        try:
            results[name] = collect_node(db_path, name, alias, transport)
        except Exception as e:
            results[name] = {"node": name, "ok": False, "error": str(e)}
    return {"db": str(db_path), "nodes": nodes, "results": results,
            "summary": status(db_path)}


# ── query API ──────────────────────────────────────────────────────────────
def _rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def status(db_path: str | Path | None = None) -> dict:
    db_path = Path(db_path or default_db_path())
    if not db_path.exists():
        return {"db": str(db_path), "exists": False, "items": 0,
                "by_kind": {}, "by_node": {}, "last_collect": None}
    con = _connect(db_path)
    try:
        items = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        by_kind = dict(con.execute(
            "SELECT kind, COUNT(*) FROM items GROUP BY kind").fetchall())
        by_node = dict(con.execute(
            "SELECT node, COUNT(*) FROM items GROUP BY node").fetchall())
        last = con.execute("SELECT MAX(ts) FROM items WHERE kind='session'").fetchone()[0]
        # model inventory summary (the "what's where, VRAM fit" picture)
        m_total = m_gb = 0
        m_by_host = []
        for row in con.execute(
                "SELECT node, title, body, ref FROM items WHERE kind='model' "
                "ORDER BY ts DESC"):
            node, title, body, ref = row
            size_gb, gpu = 0.0, False
            try:
                parts = body.split()
                if len(parts) > 1 and parts[1].endswith("GB"):
                    size_gb = float(parts[1][:-2])
                gpu = "vram_hint" in body
            except (ValueError, IndexError):
                pass
            m_total += 1
            m_gb += size_gb
            if len(m_by_host) < 20:
                m_by_host.append({"node": node or "?", "name": title or "model",
                                  "gb": round(size_gb, 1), "gpu": gpu,
                                  "hint": "gpu" if gpu else "cpu", "path": ref or ""})
        return {"db": str(db_path), "exists": True, "items": items,
                "by_kind": by_kind, "by_node": by_node, "last_collect": last,
                "model_total": m_total, "model_gb": round(m_gb, 1),
                "model_by_host": m_by_host}
    finally:
        con.close()


def search(query: str, db_path: str | Path | None = None,
           limit: int = 40, kind: str | None = None) -> list[dict]:
    db_path = db_path or default_db_path()
    if not Path(db_path).exists():
        return []
    con = _connect(db_path)
    try:
        params: list[Any] = [query]
        extra = ""
        if kind:
            extra = " AND kind = ?"
            params.append(kind)
        return _rows(
            con,
            f"""
            SELECT kind, node, source, items.title, items.body, ts,
                   snippet(items_fts, 1, '[', ']', ' … ', 8) AS hl
            FROM items_fts
            JOIN items ON items.id = items_fts.rowid
            WHERE items_fts MATCH ?
            """ + extra + """ ORDER BY bm25(items_fts) LIMIT ?""",
            tuple(params + [limit]),
        )
    finally:
        con.close()


def recent(db_path: str | Path | None = None, limit: int = 30,
           kind: str | None = None) -> list[dict]:
    db_path = db_path or default_db_path()
    if not Path(db_path).exists():
        return []
    con = _connect(db_path)
    try:
        extra, params = (" WHERE kind = ?", [kind]) if kind else ("", [])
        return _rows(
            con,
            f"SELECT kind, node, source, title, body, ts FROM items"
            f"{extra} ORDER BY ts DESC, id DESC LIMIT ?",
            tuple(params + [limit]),
        )
    finally:
        con.close()