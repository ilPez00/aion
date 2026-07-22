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

def vault_graph():
    try:
        sys.path.insert(0, os.path.join(ROOT, "src"))
        from aion.vault import VaultReader
    except Exception:
        return {"nodes": [], "edges": []}
    g = VaultReader(NOTES_DIR).graph()
    return {
        "nodes": [{"id": n["name"], "label": n["title"],
                   "r": 8 + min(14, n.get("degree", 0) * 2),
                   "kind": "note", "degree": n.get("degree", 0),
                   "backlinks": len(n.get("backlinks", [])),
                   "tags": n.get("tags", [])} for n in g["nodes"]],
        "edges": [{"s": e["from"], "t": e["to"]} for e in g["edges"]
                  if not e.get("dangling")],
    }

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

def _sse_event(data: str, event: str = "message") -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

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

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == "/" or p == "/index.html":
            fp = os.path.join(STATIC, "index.html")
            return self._send(200, "text/html", open(fp, "rb").read())
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
            fp = os.path.join(STATIC, os.path.basename(p))
            if os.path.exists(fp):
                return self._send(200, "application/octet-stream", open(fp, "rb").read())
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
        if p in ("/api/notes", "/api/vault"):
            return self._sendj(vault_graph())
        if p == "/api/notes/content":
            name = q.get("name", ["welcome"])[0]
            pp = os.path.join(NOTES_DIR, name + ".md")
            txt = open(pp).read() if os.path.exists(pp) else "# not found"
            return self._sendj({"name": name, "text": txt})
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
    try:
        while True:
            await ws.send(json.dumps({"type": "stats", **system_stats()}))
            await asyncio.sleep(1.0)
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
    async with websockets.serve(hub, "127.0.0.1", 8743):
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


if __name__ == "__main__":
    _load_env()
    # Default localhost-only: this HUD can run commands (PTY/L3), so LAN exposure
    # is opt-in. Set AION_WEB_HOST=0.0.0.0 to reach it from a phone over WiFi.
    host = os.environ.get("AION_WEB_HOST", "127.0.0.1")
    threading.Thread(target=run_ws, daemon=True).start()
    httpd = ThreadingHTTPServer((host, 8742), Handler)
    if host in ("0.0.0.0", "::"):
        lan = _lan_ip()
        print(f"AION web HUD (LAN): http://{lan}:8742  (WS :8743)")
        print("  ⚠ exposed to the whole WiFi — this HUD can run commands. "
              "Trust the network, or put HTTPS+auth in front.")
    else:
        print("AION web HUD: http://127.0.0.1:8742  (WS :8743)")
        print("  (localhost only — set AION_WEB_HOST=0.0.0.0 to reach from a phone)")
    print(f"  Sessions: {SESSIONS_DIR}")
    httpd.serve_forever()
