#!/usr/bin/env python3
"""
aion_web.py — web HUD with session persistence, SSE streaming, Web Speech voice.

Patterns adopted from hermes-webui:
  - Session: one JSON file per conversation in ~/.aion/webui/sessions/
  - SSE: streaming agent responses via chunked HTTP (server-sent events)
  - Voice: Web Speech API on frontend, server-side TTS endpoint
  - File preview: workspace browser with syntax highlighting

Layers:
  L1 Agent   : command router + pluggable LLM
  L2 HUD     : static/index.html served via stdlib http.server
  L3 Tool    : PTY host, file browse, notes graph, latex
"""
import asyncio
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
AION_DIR = os.path.expanduser("~/.aion")

# ---------------------------------------------------------------------------
# Auth. This HUD reads and writes the filesystem, runs latexmk, and drives the
# agent -- on a LAN that is somebody else's shell, not a dashboard. Loopback
# stays frictionless; any non-loopback bind demands the same shared secret the
# fleet transport already uses (~/.aion/token, see aion.fleet).
#
# Three ways to present it, in the order a phone actually needs them:
#   1. ?token=... on the first URL you open (what you type/QR once)
#   2. the aion_token cookie that first request sets (every later fetch)
#   3. X-Aion-Token header (scripts, curl)
# ---------------------------------------------------------------------------
def _clampenv(name, default, lo, hi):
    """Int from the environment, clamped. Junk falls back to the default."""
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


# The PTY websocket. Derived from the HTTP port so two HUDs on one box do not
# collide; always loopback-bound regardless of AION_WEB_HOST.
WS_PORT = _clampenv("AION_WS_PORT", _clampenv("AION_WEB_PORT", 8742, 1024, 65534) + 1,
                    1024, 65535)

TOKEN_COOKIE = "aion_token"
TOKEN_HEADER_NAME = "X-Aion-Token"
TOKEN = ""            # filled in main(); empty means auth disabled
AUTH_REQUIRED = False  # True only when bound off-loopback
WEBUI_DIR = os.path.join(AION_DIR, "webui")
SESSIONS_DIR = os.path.join(WEBUI_DIR, "sessions")
# share notes dir with the TUI vault (consistent across web and TUI)
NOTES_DIR = os.path.join(os.path.dirname(ROOT), "notes")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(NOTES_DIR, exist_ok=True)

# seed default notes
for fn, txt in {
    "welcome.md": "# Welcome to Aion\n\nThis is your local-first [[vault]]. "
                  "Notes are plain markdown, like Obsidian.\nLink ideas with [[double brackets]].\n\n"
                  "See also [[system]] and [[agent]].\n",
    "system.md": "# System\n\nThe HUD shows CPU/RAM/disk/net and (if present) GPU.\nConnects to [[welcome]].\n",
    "agent.md": "# Agent\n\nVoice or text commands route through the AI layer.\nBack to [[welcome]].\n",
}.items():
    p = os.path.join(NOTES_DIR, fn)
    if not os.path.exists(p):
        open(p, "w").write(txt)

# ---------------------------------------------------------------------------
# Session store (one JSON file per conversation)
# ---------------------------------------------------------------------------
def _session_path(sid: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{sid}.json")

def _new_session_id() -> str:
    return uuid.uuid4().hex[:12]

def _load_session(sid: str) -> dict:
    p = _session_path(sid)
    if os.path.exists(p):
        try:
            return json.loads(open(p).read())
        except Exception:
            pass
    return {"id": sid, "title": "New Chat", "messages": [], "created": time.time(), "updated": time.time()}

def _save_session(sess: dict) -> None:
    sess["updated"] = time.time()
    p = _session_path(sess["id"])
    tmp = p + ".tmp." + secrets.token_hex(4)
    open(tmp, "w").write(json.dumps(sess, indent=2))
    os.replace(tmp, p)

def _list_sessions() -> list[dict]:
    sessions = []
    for fn in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        if fn.endswith(".json"):
            try:
                data = json.loads(open(os.path.join(SESSIONS_DIR, fn)).read())
                sessions.append({
                    "id": data.get("id", fn[:-5]),
                    "title": data.get("title", "Chat"),
                    "created": data.get("created", 0),
                    "updated": data.get("updated", 0),
                    "msg_count": len(data.get("messages", [])) // 2,
                })
            except Exception as e:
                sys.stderr.write(f"web: corrupt session {fn}: {e}\n")
    return sessions[:50]

# ---------------------------------------------------------------------------
# Agent / LLM
# ---------------------------------------------------------------------------
def _load_env():
    home = os.path.expanduser("~")
    try:
        from dotenv import load_dotenv
        for f in (f"{home}/.env", f"{home}/.hermes/.env"):
            if os.path.exists(f):
                try:
                    load_dotenv(f)
                except Exception:
                    for line in open(f):
                        if line.startswith(("GROQ_API_KEY=", "DEEPSEEK_API_KEY=")):
                            k, v = line.strip().split("=", 1)
                            os.environ.setdefault(k, v)
    except Exception:
        for f in (f"{home}/.env", f"{home}/.hermes/.env"):
            if os.path.exists(f):
                for line in open(f):
                    if line.startswith(("GROQ_API_KEY=",)):
                        k, v = line.strip().split("=", 1)
                        os.environ.setdefault(k, v)

def llm_chunk(prompt: str) -> list[str]:
    """Yield text chunks from LLM (simulated for now; real streaming would yield tokens)."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        yield _router(prompt)
        return
    try:
        import requests
        sys_msg = ("You are Aion, an AI-first OS assistant. "
                    "Keep replies short, calm, useful. You have access to: terminal, files, browser, "
                    "editor, latex, notes, agent modules.")
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": prompt},
            ], "temperature": 0.3, "max_tokens": 400, "stream": True},
            timeout=25, stream=True,
        )
        for line in r.iter_lines():
            if line:
                decoded = line.decode("utf-8", errors="replace")
                if decoded.startswith("data: ") and decoded != "data: [DONE]":
                    try:
                        chunk = json.loads(decoded[6:])
                        delta = chunk["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except Exception:
                        pass
    except Exception:
        yield _router(prompt)

def _router(prompt: str) -> str:
    p = prompt.lower()
    if "open" in p and "file" in p:
        return "Opening the organic Files visualizer. Say a path or pick a node."
    if "note" in p:
        return "Notes module ready — vault is local-first markdown with a live graph."
    if "search" in p or "web" in p:
        return "Browser module armed. Speak or type a query; it drives the search field."
    if "latex" in p or "tex" in p:
        return "LaTeX module ready. Write source, hit Compile, preview the PDF in-HUD."
    if "terminal" in p or "shell" in p:
        return "Spawning a real PTY. Any TUI (micro, etc.) runs natively here."
    return ("Aion agent: command received. Route to terminal / files / browser / editor / "
            "latex / notes / agent.")

def deepsearch_answer(prompt: str) -> dict:
    """Web search + synthesize."""
    from aion import web
    return web.deepsearch_answer(prompt)

# ---------------------------------------------------------------------------
# System monitor
# ---------------------------------------------------------------------------
def gpu_load():
    try:
        proc = subprocess.run(
            "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
            "--format=csv,noheader,nounits",
            shell=True, capture_output=True, text=True, timeout=2)
        lines = (proc.stdout or "").strip().splitlines()[:1]
        if lines:
            parts = [p.strip() for p in lines[0].split(",")]
            if len(parts) == 3:
                return {"util": int(parts[0]), "mem_mb": int(parts[1]),
                        "mem_total_mb": int(parts[2])}
    except Exception:
        pass
    return None

def system_stats():
    try:
        import psutil
    except ImportError:
        return {"error": "psutil not installed", "time": time.strftime("%H:%M:%S")}
    net = psutil.net_io_counters()
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    disks = []
    for mp in ("/",):
        try:
            du = psutil.disk_usage(mp)
            disks.append({"mount": mp, "pct": round(du.used / du.total * 100, 1)})
        except Exception:
            pass
    return {
        "time": time.strftime("%H:%M:%S"),
        "date": time.strftime("%a %d %b %Y"),
        "cpu": psutil.cpu_percent(interval=None),
        "per_core": [round(x, 1) for x in per_core],
        "mem": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage("/").percent,
        "disks": disks,
        "net_up": net.bytes_sent,
        "net_down": net.bytes_recv,
        "gpu": gpu_load(),
    }

def health_summary():
    try:
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from aion.health import HealthReader
    except Exception:
        return {"ok": False, "error": "health module unavailable"}
    src = os.environ.get("AION_HEALTH_SOURCE", "json")
    path = os.environ.get("AION_HEALTH_PATH",
                          os.path.expanduser("~/.aion/health.json"))
    try:
        return HealthReader(source=src, path=path).summary()
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}

# ---------------------------------------------------------------------------
# Graph file manager
#
# The engine lives in `aion.fsgraph` (pure, unit-tested); this layer only maps
# it onto HTTP and onto the errors a browser can act on. Every path argument is
# sandboxed *inside* the engine, so no route here has to remember to do it.
# ---------------------------------------------------------------------------
def _fsgraph_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import fsgraph
    return fsgraph


def _int(q, key, default, lo, hi):
    """Clamped int from a query string. Junk falls back to the default."""
    try:
        return max(lo, min(hi, int(q.get(key, [default])[0])))
    except (TypeError, ValueError):
        return default


def fs_limit():
    return _clampenv("AION_FS_MAX_FILES", 600, 1, 2000)


def fs_default_dir():
    """Where the graph FM opens. Defaults to the repo, not $HOME — a first
    scan of a whole home directory is slow and reads as noise; the repo you
    launched from is the thing you almost certainly meant."""
    return Path(os.environ.get("AION_FS_DIR", os.path.dirname(ROOT)))


def fs_roots():
    """Bookmarks for the location bar, so nobody has to type an absolute path."""
    fg = _fsgraph_mod()
    root = fg.scan_root()
    home = Path(os.path.expanduser("~"))
    cand = [("repo", fs_default_dir()), ("home", home),
            ("notes", Path(NOTES_DIR)), ("dev", home / "dev"),
            ("documents", home / "Documents")]
    out = []
    for name, p in cand:
        try:
            rp = p.resolve()
        except OSError:
            continue
        inside = rp == root or root in rp.parents
        if inside and rp.is_dir() and not any(o["path"] == str(rp) for o in out):
            out.append({"name": name, "path": str(rp)})
    return {"root": str(root), "roots": out}


def fs_listing(fg, q):
    """Flat adjacency list for the same scan the graph renders.

    Same payload, different projection: file -> its hub -> its nearest
    neighbour. This is the keyboard/screen-reader path through the data.
    """
    g = fg.graph(q.get("dir", [str(fs_default_dir())])[0],
                 depth=_int(q, "depth", 3, 1, 8),
                 max_files=_int(q, "limit", fs_limit(), 1, 2000),
                 include_hidden=(q.get("hidden", ["0"])[0] == "1"))
    hubs = {t["id"]: t["name"] for t in g["themes"]}
    best_hub, best_sib = {}, {}
    for e in sorted(g["edges"], key=lambda e: -e["score"]):
        best_hub.setdefault(e["file_id"], (hubs.get(e["theme_id"], "?"), e["score"]))
    for e in sorted(g["file_edges"], key=lambda e: -e["score"]):
        best_sib.setdefault(e["source"], (e["target"], e["score"]))
        best_sib.setdefault(e["target"], (e["source"], e["score"]))
    titles = {f["id"]: f["title"] for f in g["files"]}
    rows = []
    for f in g["files"]:
        hub, hs = best_hub.get(f["id"], ("unclustered", 0.0))
        sib = best_sib.get(f["id"])
        rows.append({**f, "hub": hub, "hub_score": hs,
                     "nearest": titles.get(sib[0]) if sib else None,
                     "nearest_score": sib[1] if sib else 0.0})
    rows.sort(key=lambda r: (r["hub"], -r["hub_score"], r["title"]))
    return {"root": g["root"], "truncated": g["truncated"], "rows": rows}


# ---------------------------------------------------------------------------
# Process graph + unified search
# ---------------------------------------------------------------------------
def _procgraph_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import procgraph
    return procgraph


def proc_snapshot(include_finished=True):
    try:
        return _procgraph_mod().snapshot(include_finished=include_finished)
    except Exception as e:  # noqa: BLE001
        # A HUD that renders "process graph unavailable" beats one that 500s.
        return {"instances": [], "harnesses": [], "tasks": [], "swarm": [],
                "summary": {}, "error": str(e)[:200]}


# Static jump targets. These are the things you cannot discover by scanning
# something -- the modules themselves and the handful of verbs the HUD has.
_COMMANDS = [
    ("Files — graph file manager", "files", "Scan a directory as an organic graph"),
    ("Vault — notes graph", "vault", "Wikilinks, backlinks, tags"),
    ("System — telemetry constellation", "system", "CPU / RAM / disk / GPU / per-core"),
    ("Agents — process graph", "agents", "Fleet, harnesses, tasks, swarm"),
    ("Agent — chat", "agent", "Talk to the LLM"),
    ("LaTeX — compile", "latex", "Edit, compile, preview"),
    ("Repos — git worktrees", "repos", "Repositories, worktrees, branches"),
    ("Desk — todos, memory, apps", "desk", "The cockpit's Desktop workspace"),
    ("Board — kanban", "board", "Task boards and cards"),
    ("Term — real terminal", "term", "A live PTY in the browser"),
    ("Settings — providers, skills, paths", "settings", "What is configured"),
]


def search_everything(query, scan_dir=None, limit=60):
    """One query across every corpus the HUD can show.

    Ordered by how specific the match is, not by corpus: an exact task label
    beats a fuzzy file-content hit. Each result carries `{module, node}` so
    the palette can jump straight to it, and results are capped per corpus so
    one huge directory cannot crowd out the notes.
    """
    q = (query or "").strip().lower()
    if not q:
        return {"query": query, "results": []}
    out = []

    for label, module, sub in _COMMANDS:
        if q in label.lower() or q in module:
            out.append({"type": "module", "label": label, "sub": sub,
                        "module": module, "node": None, "rank": 0})

    # process graph (harnesses, tasks, instances, swarm)
    try:
        pgm = _procgraph_mod()
        for h in pgm.search(query, limit=20):
            out.append({**h, "rank": 1})
    except Exception:  # noqa: BLE001
        pass

    # cockpit surfaces: todos, memory facts, board cards, apps
    try:
        for h in _bridge_mod().search(query, limit=15):
            out.append({**h, "rank": 1})
    except Exception:  # noqa: BLE001
        pass

    # repos + worktrees
    try:
        wt = _worktrees_mod()
        snap = worktree_snapshot(probe=False)
        for h in wt.search(query, snap, limit=15):
            out.append({**h, "rank": 1})
    except Exception:  # noqa: BLE001
        pass

    # notes
    try:
        for n in vault_graph()["nodes"]:
            if q in n["label"].lower() or q in n["id"].lower():
                out.append({"type": "note", "label": n["label"],
                            "sub": f"{n.get('degree', 0)} links",
                            "module": "vault", "node": n["id"], "rank": 1})
    except Exception:  # noqa: BLE001
        pass

    # files in the directory currently on screen -- name matches first, then
    # content. Content search re-reads heads, so it is capped tightly.
    try:
        fg = _fsgraph_mod()
        base = fg.resolve_in_root(scan_dir or str(fs_default_dir()))
        paths = fg.walk(base, depth=3, max_files=fs_limit())
        named, inside = [], []
        for fp in paths:
            if q in fp.name.lower():
                named.append(fp)
            elif len(inside) < 12 and q in fg._read_head(fp).lower():
                inside.append(fp)
        for fp in named[:25]:
            out.append({"type": "file", "label": fp.name, "sub": str(fp),
                        "module": "files", "node": str(fp), "rank": 1})
        for fp in inside:
            out.append({"type": "file", "label": fp.name, "sub": f"contains · {fp}",
                        "module": "files", "node": str(fp), "rank": 2})
    except Exception:  # noqa: BLE001
        pass

    # exact-prefix matches float to the top within their rank band
    out.sort(key=lambda r: (r["rank"], not r["label"].lower().startswith(q),
                            len(r["label"])))
    return {"query": query, "results": out[:limit]}


# ---------------------------------------------------------------------------
# Cross-instance task routing
#
# Sending a task to another instance is remote code execution: aion's /run
# endpoint executes a prompt on the target. Three rules follow, and all three
# are enforced here rather than in the UI:
#
#   1. The target is resolved from real discovery (fleet.discover_local), never
#      from a host/port in the request. The caller may name an instance id; it
#      may not name a machine.
#   2. Dispatch is fail-closed. Without an explicit `confirm: true` the route
#      endpoint returns a PLAN and sends nothing, so a stray POST cannot run
#      anything anywhere.
#   3. The plan is always computed, even when pinned, so dropping work on a box
#      that died two seconds ago is refused with a reason instead of failing
#      somewhere down in the transport.
# ---------------------------------------------------------------------------
def _routing_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import routing
    return routing


def _fleet_peers():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import fleet
    return fleet.discover_local(include_self=True)


# ---------------------------------------------------------------------------
# SSH peers
#
# The tunnel pool is process-wide and lazy: nothing dials anything until a
# request asks about peers. `~/.aion/peers.json` is the only source of hosts —
# no endpoint here accepts a hostname, so the LAN-reachable HUD cannot be used
# to make this machine open an ssh connection anywhere its owner did not
# configure.
# ---------------------------------------------------------------------------
_SSH_POOL = None
_SSH_CACHE: dict = {"at": 0.0, "rows": []}
_SSH_TTL_S = 5.0


def _sshlink_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import sshlink
    return sshlink


def ssh_rows(refresh: bool = False) -> list[dict]:
    """Configured peers, each with its tunnel state and a polled /status.

    Cached briefly: `route/plan` runs on every drag-hover in the HUD, and each
    uncached call is one TCP connect per peer.
    """
    global _SSH_POOL
    now = time.time()
    if not refresh and now - _SSH_CACHE["at"] < _SSH_TTL_S:
        return _SSH_CACHE["rows"]

    sshlink = _sshlink_mod()
    peers = sshlink.load_peers()
    if not peers:
        _SSH_CACHE.update(at=now, rows=[])
        return []

    if _SSH_POOL is None:
        _SSH_POOL = sshlink.TunnelPool()
    # `wait` is short on purpose: this runs inside an HTTP handler, and a peer
    # that needs eight seconds to come up can come up by the next poll instead
    # of holding the HUD's request open.
    _SSH_POOL.ensure_all(peers, wait=3.0)
    rows = sshlink.describe(peers, _SSH_POOL)

    for row in rows:
        row["status"] = _poll_peer(row) if row["up"] else {}
    _SSH_CACHE.update(at=now, rows=rows)
    return rows


def _poll_peer(row: dict) -> dict:
    """GET /status through the tunnel. {} means the far end is not answering.

    An ssh forward outlives the remote aion process, so the local port
    accepting proves only that ssh is alive. This is the liveness check.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion.remotes import RemoteClient, RemoteNode
    node = RemoteNode(id=row["id"], host="127.0.0.1", port=row["local_port"])
    try:
        return asyncio.run(RemoteClient(timeout=3.0).fetch_status(node)) or {}
    except Exception:  # noqa: BLE001
        return {}


def peers_snapshot() -> dict:
    """What the HUD draws for the fleet's off-machine half."""
    try:
        rows = ssh_rows()
    except Exception as e:  # noqa: BLE001
        return {"peers": [], "error": f"{type(e).__name__}: {str(e)[:160]}"}
    # Copy before flattening. `rows` are the cached objects shared with
    # route_plan(), and popping "status" out of them in place made every peer
    # look dead to the router the moment the HUD drew the peer list.
    out = []
    for row in rows:
        status = row.get("status") or {}
        flat = {k: v for k, v in row.items() if k != "status"}
        flat.update(
            running_count=int(status.get("running_count", 0) or 0),
            active_harness=status.get("active_harness", ""),
            hostname=status.get("hostname", ""),
            reachable=bool(status),
        )
        out.append(flat)
    return {"peers": out, "count": len(out),
            "up": sum(1 for r in out if r["reachable"])}


def _with_peers(snap: dict) -> dict:
    """Fold SSH peers into a process-graph snapshot as extra instances.

    They carry `remote: True` so the HUD can draw them differently — a box you
    can reach is not the same thing as a box you are running on, and the graph
    should not pretend otherwise.
    """
    try:
        rows = ssh_rows()
    except Exception:  # noqa: BLE001
        return snap
    if not rows:
        return snap
    peers = []
    for row in rows:
        status = row.get("status") or {}
        peers.append({
            "id": row["id"],
            "hostname": status.get("hostname", "") or row["target"],
            "pid": 0,
            "port": row["local_port"],
            "alive": bool(status),
            "active_harness": status.get("active_harness", ""),
            "running_count": int(status.get("running_count", 0) or 0),
            "started_at": 0.0,
            "updated_at": time.time() if status else 0.0,
            "age_s": 0.0,
            "remote": True,
            "target": row["target"],
            "error": row.get("error", ""),
        })
    out = dict(snap)
    out["instances"] = list(snap.get("instances", [])) + peers
    summary = dict(snap.get("summary", {}))
    summary["instances"] = len(out["instances"])
    summary["live_instances"] = sum(1 for i in out["instances"] if i.get("alive"))
    summary["remote_instances"] = len(peers)
    out["summary"] = summary
    return out


def _self_cpu() -> float:
    try:
        import psutil
        return float(psutil.cpu_percent(interval=None)) / 100.0
    except Exception:  # noqa: BLE001
        return 0.0


def route_plan(harness: str = "", target_id: str | None = None) -> dict:
    """Where would this task go? Pure — dispatches nothing."""
    routing = _routing_mod()
    known = [h["id"] for h in proc_snapshot().get("harnesses", [])]
    cands = routing.candidates_from_fleet(
        _fleet_peers(), harnesses=known, now_cpu=_self_cpu())
    # SSH peers are candidates on exactly the same terms: rule 1 still holds,
    # because the id is resolved against peers.json, and the host it maps to is
    # always 127.0.0.1 — the local end of a tunnel this machine opened.
    try:
        cands += routing.candidates_from_ssh(ssh_rows())
    except Exception:  # noqa: BLE001
        pass  # a broken peer file must not make local routing unavailable
    return routing.plan(cands, harness=harness, target_id=target_id).as_dict()


def route_dispatch(prompt: str, harness: str = "",
                   target_id: str | None = None, confirm: bool = False) -> dict:
    """Plan, then (only with confirm) actually send the task."""
    if not prompt.strip():
        return {"ok": False, "reason": "no prompt", "dispatched": False}
    plan = route_plan(harness=harness, target_id=target_id)
    plan["dispatched"] = False
    if not plan["ok"]:
        return plan
    if not confirm:
        plan["reason"] += " — preview only, resend with confirm to dispatch"
        return plan

    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion.remotes import RemoteClient, RemoteNode
    t = plan["target"]
    node = RemoteNode(id=t["id"], host=t["host"], port=t["port"])
    try:
        result = asyncio.run(RemoteClient().run_task(node, prompt, harness))
    except Exception as e:  # noqa: BLE001
        plan["error"] = f"dispatch failed: {type(e).__name__}: {str(e)[:160]}"
        return plan
    if result is None:
        # RemoteClient swallows transport errors into None; say so plainly
        # rather than reporting a success the fleet never acknowledged.
        plan["error"] = (f"{t['id']} at {t['host']}:{t['port']} did not accept "
                         "the task (unreachable, or token mismatch)")
        return plan
    plan["dispatched"] = True
    plan["result"] = result
    return plan


# ---------------------------------------------------------------------------
# Agent control
#
# The HUD does not run harnesses. It asks a cockpit to, over the same
# authenticated transport routing uses, and that cockpit applies its own HITL
# gates. Keeping execution on one side of the wire is what makes an approval
# gate mean anything while this page is open on a phone.
#
# Same three rules as routing apply, for the same reason (/run and rerun are
# remote code execution):
#   1. the instance is resolved from discovery, never from a host in the body
#   2. spawning needs an explicit confirm; control actions act on work that
#      already exists and do not
#   3. the target instance validates the action against the task's real state
# ---------------------------------------------------------------------------
def _instance_node(instance: str):
    """Resolve an instance id to a RemoteNode, or (None, reason).

    Local peers first, then SSH peers with a live tunnel. A caller may name an
    id; it may never name a machine.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion.remotes import RemoteNode

    for peer in _fleet_peers():
        if peer.id == instance:
            return RemoteNode(id=peer.id, host=peer.host, port=peer.port), ""
    try:
        for row in ssh_rows():
            if row["id"] == instance:
                if not row["up"]:
                    return None, f"{instance}: tunnel down ({row.get('error') or 'not connected'})"
                return RemoteNode(id=instance, host="127.0.0.1",
                                  port=row["local_port"]), ""
    except Exception:  # noqa: BLE001
        pass
    return None, f"no instance named {instance!r}"


def _ask_instance(instance: str, coro_factory) -> dict:
    """Send one command to an instance and report honestly what came back."""
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion.remotes import RemoteClient

    node, why = _instance_node(instance)
    if node is None:
        return {"ok": False, "reason": why}
    try:
        result = asyncio.run(coro_factory(RemoteClient(timeout=8.0), node))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:160]}"}
    if result is None:
        # RemoteClient swallows transport errors into None. Saying "done"
        # here would report a success nothing acknowledged.
        return {"ok": False, "reason": (
            f"{instance} did not answer (no cockpit running on it, or token "
            f"mismatch). Agent control needs a live cockpit: run ./aion.sh")}
    return result


def agent_control(instance: str, task_id: str, action: str) -> dict:
    """pause / resume / cancel / rerun a task on an instance."""
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import agentctl

    if action not in agentctl.ACTIONS:
        return {"ok": False, "reason": f"unknown action {action!r}"}
    if not task_id:
        return {"ok": False, "reason": "no task"}
    return _ask_instance(instance, lambda c, n: c.control_task(n, task_id, action))


def agent_spawn(instance: str, harness: str, prompt: str,
                confirm: bool = False) -> dict:
    """Start a task on a harness. Fail-closed: no confirm, nothing runs."""
    if not (prompt or "").strip():
        return {"ok": False, "reason": "no prompt"}
    if not harness:
        return {"ok": False, "reason": "no harness"}
    if confirm is not True:
        return {"ok": False, "dispatched": False,
                "reason": f"would run on {harness} at {instance} — "
                          "resend with confirm to start it"}
    out = _ask_instance(instance, lambda c, n: c.run_task(n, prompt, harness))
    out.setdefault("ok", True)
    return out


# ---------------------------------------------------------------------------
# Settings
#
# Every value the HUD can change is declared in aion.settings, and validated
# there rather than here. This layer only decides WHAT may be addressed:
# section writes and per-harness writes, nothing else. In particular there is
# no generic "write this key to the config file" endpoint, because that is
# indistinguishable from arbitrary file write once the HUD is on a LAN.
# ---------------------------------------------------------------------------
def _settings_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import settings
    return settings


def settings_snapshot() -> dict:
    try:
        return _settings_mod().snapshot()
    except Exception as e:  # noqa: BLE001
        return {"schema": [], "values": {}, "harnesses": [], "providers": [],
                "skills": [], "error": f"{type(e).__name__}: {str(e)[:200]}"}


def settings_write(section: str, values: dict) -> dict:
    if not isinstance(values, dict):
        return {"ok": False, "rejected": {"_body": "values must be an object"}}
    try:
        return _settings_mod().write(str(section), values).as_dict()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "section": section,
                "rejected": {"_write": f"{type(e).__name__}: {str(e)[:160]}"}}


def settings_harness(harness_id: str, values: dict) -> dict:
    if not isinstance(values, dict):
        return {"ok": False, "rejected": {"_body": "values must be an object"}}
    try:
        return _settings_mod().set_harness(str(harness_id), values)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "id": harness_id,
                "rejected": {"_write": f"{type(e).__name__}: {str(e)[:160]}"}}


def _bridge_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import bridge
    return bridge


def _bridge(fn, *a, **kw):
    """Call a bridge function, degrading to an error payload not a 500."""
    try:
        return fn(_bridge_mod(), *a, **kw)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ---------------------------------------------------------------------------
# Human-in-the-loop gates
#
# A gate blocks a task until a human answers, and `GateBook.wait()` is
# fail-closed — so a gate nobody SEES is a gate that will be denied. The
# cockpit publishes its pending set to gates.json; this reads that for
# display and answers over the authenticated fleet transport.
#
# The published file is never a control channel: writing approved=true into
# it releases nothing. Only the cockpit's in-memory GateBook can do that, and
# only via POST /gate on its token-guarded listener.
# ---------------------------------------------------------------------------
def gates_pending():
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
        from aion import hitl
        return {"gates": hitl.read_all_pending()}
    except Exception as e:  # noqa: BLE001
        return {"gates": [], "error": f"{type(e).__name__}: {str(e)[:180]}"}


def gate_answer(gate_id: str, approved: bool, instance: str = "") -> dict:
    """Answer a gate on the cockpit that raised it."""
    if not gate_id:
        return {"ok": False, "error": "no gate id"}
    peers = {p.id: p for p in _fleet_peers()}
    if instance and instance not in peers:
        return {"ok": False,
                "error": f"instance {instance!r} is not running — the gate "
                         "cannot be answered until its cockpit is back"}
    targets = [peers[instance]] if instance else list(peers.values())
    if not targets:
        return {"ok": False, "error": "no live cockpit to answer on"}

    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion.remotes import RemoteClient, RemoteNode
    client = RemoteClient()
    errors = []
    for peer in targets:
        node = RemoteNode(id=peer.id, host="127.0.0.1", port=peer.port)
        try:
            res = asyncio.run(client.resolve_gate(node, gate_id, approved))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{peer.id}: {type(e).__name__}")
            continue
        if res and res.get("ok"):
            return {"ok": True, "instance": peer.id, **res}
        if res and res.get("error"):
            errors.append(f"{peer.id}: {res['error']}")
        else:
            errors.append(f"{peer.id}: no response")
    # Never report success we did not get: the gate stays pending, and
    # pending means denied once the harness times out.
    return {"ok": False, "error": "; ".join(errors) or "not answered"}


def voice_interpret(text: str, confidence: float = 1.0,
                    context: dict | None = None, allow_llm: bool = True) -> dict:
    """Spoken phrase -> HUD action. Executes nothing itself.

    Rules resolve the common phrasings instantly; anything else goes to the
    model, whose reply is validated against a fixed schema before it can
    become an action (see voicecmd._validate).
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
        from aion import voicecmd
        act = voicecmd.understand(text, confidence, context=context,
                                  allow_llm=allow_llm)
        return act.as_dict()
    except Exception as e:  # noqa: BLE001
        return {"action": "none", "ok": False, "args": {},
                "say": f"{type(e).__name__}: {str(e)[:160]}",
                "transcript": text, "confidence": confidence, "source": "error"}


def voice_vocabulary() -> dict:
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
        from aion import voicecmd
        return {"vocabulary": voicecmd.vocabulary(),
                "min_confidence": voicecmd.MIN_CONFIDENCE}
    except Exception as e:  # noqa: BLE001
        return {"vocabulary": [], "error": str(e)[:160]}


def _worktrees_mod():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion import worktrees
    return worktrees


def worktree_snapshot(probe=True):
    """Repos + worktrees under the fs sandbox. Degrades, never 500s."""
    try:
        fg = _fsgraph_mod()
        wt = _worktrees_mod()
        root = os.environ.get("AION_REPO_ROOT") or str(fs_default_dir().parent)
        base = fg.resolve_in_root(root)      # never scan outside the sandbox
        tasks = proc_snapshot().get("tasks", [])
        return wt.graph(base, depth=_clampenv("AION_REPO_DEPTH", 3, 1, 6),
                        probe=probe, tasks=tasks)
    except Exception as e:  # noqa: BLE001
        return {"root": "", "repos": [], "summary": {}, "error": str(e)[:200]}


def vault_root() -> str:
    """Where the notes actually live.

    The TUI already asks for this on first run and records the answer in the
    `vault_setup_done` flag file, but the web HUD ignored it and always read
    the repo's own `notes/` — so the Vault module showed four seeded demo
    notes instead of the user's real Obsidian vault. Same resolution order as
    the cockpit, so both halves of aion agree on what "the vault" means:

        AION_VAULT  >  the recorded setup answer  >  repo notes/
    """
    env = os.environ.get("AION_VAULT", "").strip()
    if env:
        return os.path.expanduser(env)
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
        from aion.fleet import shared_path
        recorded = shared_path("vault_setup_done").read_text().strip()
        if recorded and os.path.isdir(os.path.expanduser(recorded)):
            return os.path.expanduser(recorded)
    except Exception:  # noqa: BLE001
        pass
    return NOTES_DIR


def _vault_reader():
    sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
    from aion.vault import VaultReader
    return VaultReader(vault_root())


def vault_graph():
    try:
        reader = _vault_reader()
        g = reader.graph()
    except Exception as e:  # noqa: BLE001
        return {"nodes": [], "edges": [], "root": vault_root(), "error": str(e)[:200]}
    return {
        "root": vault_root(),
        "nodes": [{"id": n["name"], "label": n["title"],
                   "r": 8 + min(14, n.get("degree", 0) * 2),
                   "kind": "note", "degree": n.get("degree", 0),
                   "backlinks": len(n.get("backlinks", [])),
                   # relative path, so a nested Obsidian vault can be opened
                   # without guessing where the file is
                   "path": n.get("path", ""),
                   "tags": n.get("tags", [])} for n in g["nodes"]],
        "edges": [{"s": e["from"], "t": e["to"]} for e in g["edges"]
                  if not e.get("dangling")],
    }


def vault_note(name: str) -> dict:
    """Read one note by its vault name.

    The path is taken from the reader's own index and never built from the
    caller's string. The previous version did `join(NOTES_DIR, name + ".md")`,
    so `?name=../../../../etc/passwd` escaped the vault outright — and this
    endpoint is reachable from the LAN.
    """
    reader = _vault_reader()
    notes = reader.read()
    note = notes.get(name)
    if note is None:
        return {"name": name, "text": "# not found", "found": False}
    target = (reader.root / note.path).resolve()
    root = reader.root.resolve()
    if root != target and root not in target.parents:
        # cannot happen via the index, but the check is cheap and this is the
        # exact spot where a future refactor would reintroduce the escape
        return {"name": name, "text": "# refused", "found": False}
    return {"name": name, "path": note.path, "found": True,
            "text": target.read_text(encoding="utf-8", errors="replace")}

# ---------------------------------------------------------------------------
# PTY host (terminal + editor)
# ---------------------------------------------------------------------------
class PTYHost:
    def __init__(self, cols=100, rows=30, cmd="bash"):
        self.cols, self.rows = cols, rows
        import pyte, pty, fcntl, termios
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.master, self.slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", cmd],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            preexec_fn=lambda: (os.setsid(),
                                fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)),
            env={**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)},
        )
        os.close(self.slave)
        self.lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        import select
        while self.alive:
            try:
                r, _, _ = select.select([self.master], [], [], 0.05)
            except OSError:
                break
            if self.master in r:
                try:
                    data = os.read(self.master, 4096)
                except OSError:
                    break
                if not data:
                    break
                with self.lock:
                    self.stream.feed(data)

    def write(self, data: str):
        try:
            os.write(self.master, data.encode("utf-8", "replace"))
        except OSError:
            pass

    def resize(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.screen.resize(rows, cols)
        import fcntl, termios, struct
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            lines = [self.screen.display[i]
                     for i in range(min(self.screen.cursor.y + 1, self.rows))]
            while len(lines) < self.rows:
                lines.append(" " * self.cols)
            return {"lines": lines[:self.rows],
                    "cursor": [self.screen.cursor.x, self.screen.cursor.y]}

    def close(self):
        self.alive = False
        try:
            self.proc.terminate()
        except Exception:
            pass

HOSTS = {}

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_CTYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


def _ctype(path: str) -> str:
    return _CTYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _sse_event(data: str, event: str = "message") -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ---- auth ------------------------------------------------------------
    def _presented_token(self, q):
        """Token from query, cookie, or header -- in that precedence."""
        got = (q.get("token") or [""])[0]
        if got:
            return got, True          # came from the URL -> worth a cookie
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            k, _, v = part.strip().partition("=")
            if k == TOKEN_COOKIE and v:
                return v, False
        return self.headers.get(TOKEN_HEADER_NAME, ""), False

    def _authorized(self, q):
        """(ok, set_cookie). Constant-time compare; loopback is exempt."""
        if not AUTH_REQUIRED or not TOKEN:
            return True, False
        got, from_url = self._presented_token(q)
        ok = hmac.compare_digest(got, TOKEN)
        return ok, (ok and from_url)

    def _deny(self):
        # 401 with no hint about the token -- an unauthenticated caller learns
        # only that this port wants credentials.
        self._send(401, "text/plain",
                   b"401 unauthorized: append ?token=<~/.aion/token> to the URL\n")

    def _send(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _sendj(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _fs(self, fn):
        """Run a graph-FM call, mapping engine errors onto honest HTTP codes.

        A sandbox rejection is a 403 and says so; anything else is a 500 with
        the reason. Returning 200 + `{"error": ...}` would let the frontend
        render a broken graph as an empty one.
        """
        fg = _fsgraph_mod()
        try:
            return self._sendj(fn(fg))
        except fg.FsGraphError as e:
            code = 403 if "outside scan root" in str(e) else 400
            return self._sendj({"error": str(e)}, code)
        except OSError as e:
            return self._sendj({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        ok, set_cookie = self._authorized(q)
        if not ok:
            return self._deny()
        if p == "/" or p == "/index.html":
            fp = os.path.join(STATIC, "index.html")
            # SameSite=Strict: the cookie is never attached to a cross-site
            # request, so a hostile page on the same WiFi cannot CSRF the HUD.
            extra = ({"Set-Cookie": f"{TOKEN_COOKIE}={TOKEN}; Path=/; "
                                    "SameSite=Strict; Max-Age=31536000"}
                     if set_cookie else None)
            return self._send(200, "text/html", open(fp, "rb").read(), extra)
        # ---- PWA assets (root-scoped so the service worker controls "/") ----
        if p in ("/manifest.webmanifest", "/sw.js", "/icon.svg"):
            fp = os.path.join(STATIC, os.path.basename(p))
            if not os.path.exists(fp):
                return self._send(404, "text/plain", b"no")
            ctype = {
                "/manifest.webmanifest": "application/manifest+json",
                "/sw.js": "text/javascript",
                "/icon.svg": "image/svg+xml",
            }[p]
            # SW must be allowed to claim the root scope
            extra = {"Service-Worker-Allowed": "/"} if p == "/sw.js" else None
            return self._send(200, ctype, open(fp, "rb").read(), headers=extra)
        if p.startswith("/static/"):
            # Content-Type is load-bearing, not cosmetic: a stylesheet served
            # as octet-stream is silently dropped in standards mode, and a
            # script served as one is refused outright when nosniff is on.
            fp = os.path.join(STATIC, os.path.basename(p))
            if os.path.exists(fp):
                return self._send(200, _ctype(fp), open(fp, "rb").read(),
                                  {"X-Content-Type-Options": "nosniff"})
            return self._send(404, "text/plain", b"no")
        # ---- API: sessions ----
        if p == "/api/sessions":
            return self._sendj({"sessions": _list_sessions()})
        if p == "/api/session":
            sid = q.get("id", [None])[0]
            if sid:
                return self._sendj(_load_session(sid))
            return self._sendj({"error": "no session id"}, 400)
        if p == "/api/session/new":
            sid = _new_session_id()
            sess = _load_session(sid)
            _save_session(sess)
            return self._sendj({"id": sid})
        # ---- SE - streaming agent response ----
        if p == "/api/agent/stream":
            sid = q.get("session", [None])[0]
            text = q.get("text", [""])[0]
            if not text:
                return self._send(400, "text/plain", b"no text")
            if not sid:
                sid = _new_session_id()
            sess = _load_session(sid)
            if not sess.get("messages"):
                sess["title"] = text[:60]
            sess["messages"].append({"role": "user", "content": text, "ts": time.time()})
            _save_session(sess)
            # SSE response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            full = ""
            for chunk in llm_chunk(text):
                if not chunk:
                    continue
                full += chunk
                try:
                    self.wfile.write(_sse_event(json.dumps({"token": chunk}), "token"))
                    self.wfile.flush()
                except BrokenPipeError:
                    break
            # final event with complete message
            sess["messages"].append({"role": "assistant", "content": full, "ts": time.time()})
            _save_session(sess)
            try:
                self.wfile.write(_sse_event(json.dumps({"done": True, "session": sid}), "done"))
                self.wfile.flush()
            except BrokenPipeError:
                pass
            return
        if p == "/api/files":
            path = q.get("path", ["/home/gio"])[0]
            nodes, edges = [], []
            try:
                entries = sorted(os.listdir(path))
            except Exception:
                entries = []
            root = os.path.basename(path.rstrip("/")) or path
            nodes.append({"id": root, "label": root, "r": 14, "kind": "dir"})
            for e in entries[:40]:
                if e.startswith("."):
                    continue
                full = os.path.join(path, e)
                isdir = os.path.isdir(full)
                nodes.append({"id": e, "label": e, "r": 9 if isdir else 6,
                              "kind": "dir" if isdir else "file"})
                edges.append({"s": root, "t": e})
            return self._sendj({"nodes": nodes, "edges": edges, "path": path})
        # ---- graph file manager (aion.fsgraph, physis-compatible payload) ----
        if p == "/api/fs/roots":
            return self._sendj(fs_roots())
        if p == "/api/fs/graph":
            return self._fs(lambda fg: fg.graph(
                q.get("dir", [str(fs_default_dir())])[0],
                depth=_int(q, "depth", 3, 1, 8),
                max_files=_int(q, "limit", fs_limit(), 1, 2000),
                k=_int(q, "k", 0, 0, 24) or None,
                include_hidden=(q.get("hidden", ["0"])[0] == "1"),
            ))
        if p == "/api/fs/file":
            return self._fs(lambda fg: fg.preview(q.get("path", [""])[0]))
        # ---- agent / process graph ----
        if p == "/api/agents":
            snap = proc_snapshot(
                include_finished=(q.get("finished", ["1"])[0] == "1"))
            # SSH peers join the graph as instances. Deliberately merged HERE
            # and not inside procgraph.snapshot(): that function is pure and
            # disk-only, it is what the live-push watcher fingerprints, and
            # putting a network round trip inside it would make an idle HUD
            # dial every peer four times a second.
            return self._sendj(_with_peers(snap))
        # ---- the cockpit's own surfaces, read from shared state ----
        if p == "/api/voice/vocabulary":
            return self._sendj(voice_vocabulary())
        if p == "/api/gates":
            return self._sendj(gates_pending())
        if p == "/api/desktop":
            return self._sendj(_bridge(lambda b: b.desktop()))
        if p == "/api/todos":
            return self._sendj(_bridge(lambda b: b.todos()))
        if p == "/api/memory":
            return self._sendj(_bridge(
                lambda b: b.memory(q.get("q", [""])[0])))
        if p == "/api/board":
            return self._sendj(_bridge(lambda b: b.boards()))
        if p == "/api/settings":
            # The read-only surfaces (providers, skills, paths) plus the whole
            # editable schema and its current values, in one request.
            snap = settings_snapshot()
            snap.update(_bridge(lambda b: b.settings()))
            return self._sendj(snap)
        if p == "/api/apps":
            return self._sendj(_bridge(lambda b: b.apps()))
        if p == "/api/peers":
            return self._sendj(peers_snapshot())
        if p == "/api/route/plan":
            return self._sendj(route_plan(
                harness=q.get("harness", [""])[0],
                target_id=(q.get("target", [""])[0] or None)))
        if p == "/api/worktrees":
            return self._sendj(worktree_snapshot(
                probe=(q.get("probe", ["1"])[0] == "1")))
        # ---- unified search (the command palette) ----
        if p == "/api/search/all":
            return self._sendj(search_everything(
                (q.get("q", [""])[0]), (q.get("dir", [""])[0] or None)))
        if p == "/api/fs/listing":
            # Accessible twin of the graph: a flat, sortable adjacency list.
            # Network graphs grade D for accessibility, so the list is a peer
            # view, not a consolation prize (see static/hud.js `renderList`).
            return self._fs(lambda fg: fs_listing(fg, q))
        if p in ("/api/notes", "/api/vault"):
            return self._sendj(vault_graph())
        if p == "/api/notes/content":
            try:
                return self._sendj(vault_note(q.get("name", ["welcome"])[0]))
            except OSError as e:
                return self._sendj({"error": str(e)[:200]}, 500)
        if p == "/api/health":
            return self._sendj(health_summary())
        if p == "/api/system":
            return self._sendj(system_stats())
        if p == "/api/tts-capability":
            cap = bool(shutil.which("edge-tts")) or bool(os.environ.get("OPENAI_API_KEY"))
            return self._sendj({"available": cap})
        if p == "/build.pdf":
            pp = os.path.join(ROOT, "build.pdf")
            if os.path.exists(pp):
                return self._send(200, "application/pdf", open(pp, "rb").read())
            return self._send(404, "text/plain", b"no pdf")
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        u = urlparse(self.path)
        ok, _ = self._authorized(parse_qs(u.query))
        if not ok:
            return self._deny()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        p = u.path
        if p == "/api/agent":
            text = (body.get("text") or "").strip()
            sid = body.get("session") or _new_session_id()
            sess = _load_session(sid)
            sess["messages"].append({"role": "user", "content": text, "ts": time.time()})
            _save_session(sess)
            # non-streaming
            reply = "".join(llm_chunk(text))
            sess["messages"].append({"role": "assistant", "content": reply, "ts": time.time()})
            _save_session(sess)
            return self._sendj({"reply": reply, "session": sid})
        if p == "/api/search":
            qq = body.get("query", "")
            return self._sendj({"results": web_search(qq), "query": qq})
        # ---- mutating the cockpit's shared stores ----
        # Todos and memory are the user's own notes; the web HUD writes them
        # directly rather than round-tripping through a cockpit that may not
        # be running. Anything that EXECUTES still goes through routing.
        if p == "/api/todos":
            act, val = body.get("action", ""), body.get("value")
            if act == "add" and isinstance(val, str) and val.strip():
                return self._sendj(_bridge(lambda b: b.todo_add(val.strip())))
            if act == "done" and isinstance(val, int):
                return self._sendj(_bridge(lambda b: b.todo_done(val)))
            if act == "rm" and isinstance(val, int):
                return self._sendj(_bridge(lambda b: b.todo_remove(val)))
            return self._sendj({"error": f"bad todo action {act!r}"}, 400)
        if p == "/api/memory":
            act, val = body.get("action", ""), body.get("value")
            if act == "add" and isinstance(val, str) and val.strip():
                return self._sendj(_bridge(lambda b: b.memory_add(val.strip())))
            if act == "forget" and isinstance(val, int):
                return self._sendj(_bridge(lambda b: b.memory_forget(val)))
            return self._sendj({"error": f"bad memory action {act!r}"}, 400)
        if p == "/api/voice":
            try:
                conf = float(body.get("confidence", 1.0))
            except (TypeError, ValueError):
                conf = 0.0        # unparseable confidence is NOT confidence
            ctx = body.get("context") if isinstance(body.get("context"), dict) else None
            return self._sendj(voice_interpret(
                str(body.get("text", "")), conf, context=ctx,
                allow_llm=body.get("llm") is not False))
        if p == "/api/gate":
            # `approved` must be the boolean true; anything else is a reject.
            return self._sendj(gate_answer(
                str(body.get("gate_id", "")),
                body.get("approved") is True,
                str(body.get("instance", "") or "")))
        if p == "/api/settings":
            return self._sendj(settings_write(
                str(body.get("section", "")), body.get("values") or {}))
        if p == "/api/settings/harness":
            return self._sendj(settings_harness(
                str(body.get("id", "")), body.get("values") or {}))
        if p == "/api/agents/control":
            return self._sendj(agent_control(
                str(body.get("instance", "")),
                str(body.get("task_id", "")),
                str(body.get("action", ""))))
        if p == "/api/agents/spawn":
            # Fail-closed, same as /api/route: no `confirm` means preview only.
            return self._sendj(agent_spawn(
                str(body.get("instance", "")),
                str(body.get("harness", "")),
                str(body.get("prompt", "")),
                confirm=body.get("confirm") is True))
        if p == "/api/route":
            # Fail-closed: no `confirm` means plan only, dispatch nothing.
            return self._sendj(route_dispatch(
                prompt=str(body.get("prompt", "")),
                harness=str(body.get("harness", "")),
                target_id=(body.get("target") or None),
                confirm=body.get("confirm") is True))
        if p == "/api/open":
            # Hand a path to the user's editor. The path is sandboxed here;
            # the editor comes from an allowlist inside aion.opener, never
            # from the request — this endpoint is LAN-reachable.
            sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "src"))
            from aion import opener
            fg = _fsgraph_mod()
            try:
                target = fg.resolve_in_root(body.get("path", ""))
                return self._sendj(opener.launch(
                    str(target), preferred=None,
                    line=body.get("line") if isinstance(body.get("line"), int) else None))
            except fg.FsGraphError as e:
                return self._sendj({"error": str(e)}, 403)
            except opener.OpenError as e:
                return self._sendj({"error": str(e)}, 400)
        if p == "/api/fs/move":
            return self._fs(lambda fg: fg.move(body.get("from", ""),
                                               body.get("to", "")))
        if p == "/api/notes/save":
            name = (body.get("name") or "note").replace("/", "")
            open(os.path.join(NOTES_DIR, name + ".md"), "w").write(body.get("text", ""))
            return self._sendj({"ok": True})
        if p == "/api/latex":
            tex = os.path.join(ROOT, "build.tex")
            open(tex, "w").write(body.get("src", ""))
            try:
                r = subprocess.run(
                    ["latexmk", "-pdf", "-interaction=nonstopmode", "build.tex"],
                    cwd=ROOT, capture_output=True, text=True, timeout=60)
                ok = os.path.exists(os.path.join(ROOT, "build.pdf"))
                return self._sendj({"ok": ok, "pdf": "/build.pdf" if ok else None,
                                    "log": (r.stdout + r.stderr)[-2000:]})
            except Exception as e:
                return self._sendj({"ok": False, "pdf": None, "log": str(e)})
        if p == "/api/tts":
            engine = body.get("engine", "edge")
            text = body.get("text", "")
            if not text:
                return self._sendj({"error": "no text"}, 400)
            if engine == "edge" and shutil.which("edge-tts"):
                import edge_tts
                voice = body.get("voice", "en-GB-SoniaNeural")
                rate = body.get("rate", "+0%")
                out = os.path.join(WEBUI_DIR, "tts_out.mp3")
                async def _tts():
                    communicate = edge_tts.Communicate(text, voice, rate=rate)
                    await communicate.save(out)
                asyncio.run(_tts())
                if os.path.exists(out):
                    with open(out, "rb") as f:
                        data = f.read()
                    self._send(200, "audio/mpeg", data)
                    return
                return self._sendj({"error": "tts failed"}, 500)
            return self._sendj({"error": "no tts engine"}, 503)
        return self._send(404, "text/plain", b"not found")

# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------
def ws_path(ws):
    return getattr(ws, "path", None) or getattr(getattr(ws, "request", None), "path", "")

async def term_ws(ws):
    host = PTYHost(cols=100, rows=30)
    HOSTS[id(ws)] = host
    try:
        async def sender():
            while True:
                await ws.send(json.dumps({"type": "screen", **host.snapshot()}))
                await asyncio.sleep(0.08)
        async def receiver():
            async for msg in ws:
                d = json.loads(msg)
                if d.get("type") == "input":
                    host.write(d.get("data", ""))
                elif d.get("type") == "resize":
                    host.resize(d.get("cols", 100), d.get("rows", 30))
        await asyncio.gather(sender(), receiver())
    finally:
        host.close()
        HOSTS.pop(id(ws), None)

async def events_ws(ws):
    """Live channel: system stats on a tick, fleet changes as they happen.

    The fleet half is a watch, not a poll of the expensive thing: it stats a
    few checkpoint files at 4Hz and only parses/diffs when the signature
    moves. An idle fleet therefore costs a handful of stat() calls per second
    and sends nothing, so a HUD left open all day is free.

    Every send is wrapped: one slow or dead client must not take down the
    loop for the others, and a fleet read error must not kill the stats feed.
    """
    pg = _procgraph_mod()
    prev_snap = None
    prev_fp = None
    prev_gates = None
    stats_at = 0.0
    try:
        while True:
            now = time.time()
            if now - stats_at >= 1.0:
                stats_at = now
                await ws.send(json.dumps({"type": "stats", **system_stats()}))
            try:
                # Gates are the one thing that must never wait for a poll:
                # the harness is blocked and fail-closed the whole time.
                gates = gates_pending().get("gates", [])
                gate_sig = json.dumps(sorted(g["id"] for g in gates))
                if gate_sig != prev_gates:
                    prev_gates = gate_sig
                    await ws.send(json.dumps({"type": "gates", "gates": gates}))
                fp = pg.fingerprint()
                if fp != prev_fp:
                    prev_fp = fp
                    snap = pg.snapshot()
                    payload = pg.diff(prev_snap, snap)
                    prev_snap = snap
                    # A first-connect diff is "full"; suppress the empty
                    # updates that follow an unrelated file touch.
                    if payload["full"] or payload["changed"] or payload["removed"]:
                        await ws.send(json.dumps({"type": "agents", **payload}))
            except Exception as e:  # noqa: BLE001
                await ws.send(json.dumps({"type": "agents_error",
                                          "error": str(e)[:200]}))
            await asyncio.sleep(0.25)
    except Exception:
        pass

async def editor_ws(ws):
    host = None
    try:
        async def sender():
            while True:
                if host:
                    await ws.send(json.dumps({"type": "screen", **host.snapshot()}))
                await asyncio.sleep(0.08)
        async def receiver():
            nonlocal host
            async for msg in ws:
                d = json.loads(msg)
                if d.get("type") == "open":
                    fn = d.get("file", "/home/gio/aion/notes/welcome.md")
                    host = PTYHost(cols=100, rows=30, cmd=f"micro {shlex.quote(fn)}")
                    HOSTS[id(ws)] = host
                elif d.get("type") == "input" and host:
                    host.write(d.get("data", ""))
                elif d.get("type") == "resize" and host:
                    host.resize(d.get("cols", 100), d.get("rows", 30))
                elif d.get("type") == "close" and host:
                    host.close()
                    HOSTS.pop(id(ws), None)
                    host = None
        await asyncio.gather(sender(), receiver())
    finally:
        if host:
            host.close()
            HOSTS.pop(id(ws), None)

async def hub(ws):
    path = ws_path(ws)
    if path == "/ws/term":
        await term_ws(ws)
    elif path == "/ws/events":
        await events_ws(ws)
    elif path == "/ws/editor":
        await editor_ws(ws)
    else:
        await ws.close()

async def run_ws_async():
    import websockets
    async with websockets.serve(hub, "127.0.0.1", WS_PORT):
        await asyncio.Future()

def run_ws():
    try:
        asyncio.run(run_ws_async())
    except ImportError:
        sys.stderr.write("web: websockets not installed, WS disabled\n")

# ---------------------------------------------------------------------------
# Web search (from aion.web)
# ---------------------------------------------------------------------------
def web_search(q: str, n: int = 5):
    import requests as rq
    try:
        html = rq.post(
            "https://html.duckduckgo.com/html/",
            data={"q": q},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            timeout=8,
        ).text
    except Exception as e:
        return [{"title": f"(search failed: {e})", "url": "", "snippet": ""}]
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    out = []
    for url, title, snip in blocks[:n]:
        import urllib.parse
        url = urllib.parse.unquote(re.sub(r".*uddg=([^&]+).*", r"\1", url))
        title = re.sub(r"<[^>]+>", "", title).strip()
        snip = re.sub(r"<[^>]+>", "", snip).strip()
        if title:
            out.append({"title": title, "url": url, "snippet": snip})
    if not out:
        for m in re.findall(r'class="result__a" href="([^"]+)">(.*?)</a>', html, re.S)[:n]:
            import urllib.parse
            url = urllib.parse.unquote(re.sub(r".*uddg=([^&]+).*", r"\1", m[0]))
            out.append({"title": re.sub(r"<[^>]+>", "", m[1]).strip(), "url": url, "snippet": ""})
    return out

def _lan_ip() -> str:
    """Best-guess primary LAN IP (no traffic actually sent)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "")


if __name__ == "__main__":
    _load_env()
    # Default localhost-only: this HUD reads and writes the filesystem, runs
    # latexmk, and drives the agent. LAN exposure is opt-in via AION_WEB_HOST,
    # and off loopback it is token-gated -- a warning is not a control.
    host = os.environ.get("AION_WEB_HOST", "127.0.0.1")
    AUTH_REQUIRED = not _loopback(host)
    if AUTH_REQUIRED:
        try:
            from aion.fleet import load_or_create_token
            TOKEN = load_or_create_token()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(
                f"web: refusing to bind {host} without a token ({e}).\n"
                "     Bind loopback, or fix ~/.aion/token so auth can load.\n")
            sys.exit(1)
    port = _clampenv("AION_WEB_PORT", 8742, 1024, 65535)
    threading.Thread(target=run_ws, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    if not _loopback(host):
        lan = _lan_ip()
        print(f"AION web HUD (LAN): http://{lan}:{port}/?token={TOKEN}")
        print("  token-gated (shared fleet secret, ~/.aion/token). Open the URL")
        print("  above once per device — it sets a SameSite=Strict cookie.")
        print("  Still plain HTTP: the token crosses the WiFi in clear. Put it")
        print("  behind tailscale serve / a TLS proxy on an untrusted network.")
        print(f"  PTY (:{WS_PORT}) stays bound to loopback and is NOT reachable here.")
    else:
        print(f"AION web HUD: http://127.0.0.1:{port}  (WS :{WS_PORT})")
        print("  (localhost only — set AION_WEB_HOST=0.0.0.0 to reach from a phone)")
    print(f"  Graph FM root: {os.environ.get('AION_FS_ROOT', '~')}"
          f"  ·  opens on: {fs_default_dir()}")
    print(f"  Sessions: {SESSIONS_DIR}")
    httpd.serve_forever()
