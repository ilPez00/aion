#!/usr/bin/env python3
"""
AION — AI-OS prototype (web+Python layer; Tauri-ready).
3 layers:
  LAYER 1 AI   : Agent (command router + pluggable LLM)  -> agent_reply()
  LAYER 2 SHELL: HUD served as static/index.html         -> stdlib http.server + WS
  LAYER 3 TOOL : PTY host (micro/latexmk), file/notes graph, browser, latex
No fastapi/pydantic (broken on this host) — stdlib http.server + websockets.
"""
import os, time, json, re, threading, select, pty, subprocess, shlex, asyncio
import psutil, pyte
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import websockets

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
NOTES_DIR = os.path.join(ROOT, "notes")
os.makedirs(NOTES_DIR, exist_ok=True)
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

# ───────────────────────── LAYER 1: AGENT (router + pluggable LLM) ─────────
def _load_env():
    """Pull backend keys from the shared .env files (no secrets in code)."""
    import dotenv  # optional
    for f in ("/home/gio/.env", "/home/gio/.hermes/.env"):
        if os.path.exists(f):
            try:
                dotenv.load_dotenv(f)
            except Exception:
                for line in open(f):
                    if line.startswith(("GROQ_API_KEY=", "DEEPSEEK_API_KEY=")):
                        k, v = line.strip().split("=", 1)
                        os.environ.setdefault(k, v)

def llm(prompt: str) -> str:
    """
    Pluggable LLM hook. Routes real prompts to Groq (OpenAI-compatible) when
    AION_LLM=groq (or GROQ_API_KEY present); otherwise the deterministic router.
    """
    backend = os.environ.get("AION_LLM", "").lower()
    if backend in ("", "stub") and not os.environ.get("GROQ_API_KEY"):
        return _router(prompt)
    try:
        return _groq(prompt)
    except Exception as e:
        return f"[LLM error: {e}] " + _router(prompt)

def _router(prompt: str) -> str:
    p = prompt.lower()
    if "open" in p and "file" in p:
        return "Opening the organic Files visualizer. Say a path or pick a node."
    if "note" in p:
        return "Notes module ready — vault is local-first markdown with a live graph."
    if "search" in p or "browser" in p:
        return "Browser module armed. Speak or type a query; it drives the search field."
    if "latex" in p or "tex" in p:
        return "LaTeX module ready. Write source, hit Compile, preview the PDF in-HUD."
    if "terminal" in p or "shell" in p:
        return "Spawning a real PTY. Any TUI (micro, etc.) runs natively here."
    return ("Aion agent: command received. Route to terminal / files / browser / editor / "
            "latex / notes / agent. (LLM slot is a stub — wire AION_LLM to go live.)")

def _groq(prompt: str, model: str = "llama-3.3-70b-versatile") -> str:
    import requests
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("no GROQ_API_KEY")
    sys = ("You are Aion, an AI-first operating-system assistant living inside a sci-fi HUD. "
           "You control modules: terminal, files, browser, editor, latex, notes, agent. "
           "Keep replies short, calm, useful. Offer to route the user to a module.")
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "system", "content": sys},
             {"role": "user", "content": prompt}], "temperature": 0.4, "max_tokens": 220},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

# ───────────────────────── DEEPSEARCH (real web tool the agent can call) ────
def web_search(q: str, n: int = 5):
    """DuckDuckGo HTML scrape. Returns list of {title,url,snippet}. Timeout-safe."""
    import requests, re
    import urllib.parse
    try:
        html = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": q},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            timeout=8,
        ).text
    except Exception as e:
        return [{"title": f"(search failed: {e})", "url": "", "snippet": ""}]
    # DDG HTML lite result blocks
    blocks = re.findall(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
                        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    out = []
    for url, title, snip in blocks[:n]:
        url = urllib.parse.unquote(re.sub(r".*uddg=([^&]+).*", r"\1", url))
        title = re.sub(r"<[^>]+>", "", title).strip()
        snip = re.sub(r"<[^>]+>", "", snip).strip()
        if title:
            out.append({"title": title, "url": url, "snippet": snip})
    if not out:  # fallback: any result link
        for m in re.findall(r'class="result__a" href="([^"]+)">(.*?)</a>', html, re.S)[:n]:
            url = urllib.parse.unquote(re.sub(r".*uddg=([^&]+).*", r"\1", m[0]))
            out.append({"title": re.sub(r"<[^>]+>", "", m[1]).strip(), "url": url, "snippet": ""})
    return out

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the live web for current facts, news, versions, or anything "
                       "the user asks that needs up-to-date information. Returns titles, URLs, snippets.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "The search query to run on the web."}},
            "required": ["query"],
        },
    },
}

def deepsearch_answer(prompt: str, model: str = "llama-3.3-70b-versatile"):
    """ReAct-style: let the LLM decide to search, run it, then synthesize with sources.
    Groq occasionally emits a malformed tool call (400). Degrade gracefully: retry
    without tools so we never return an error to the user."""
    import requests, json as _json
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return {"answer": _router(prompt), "sources": [], "searched": False}
    sys = ("You are Aion, an AI-first OS assistant. You may call web_search to get current "
           "facts. When you have enough info, answer concisely and cite sources by title.")
    messages = [{"role": "system", "content": sys}, {"role": "user", "content": prompt}]
    try:
        r1 = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "tools": [SEARCH_TOOL],
                  "tool_choice": "auto", "temperature": 0.3, "max_tokens": 600},
            timeout=25,
        )
        if r1.status_code != 200:
            # Groq tool-call quirk -> fall back to a clean answer (no tools)
            r0 = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 600},
                timeout=25,
            ).json()
            return {"answer": r0["choices"][0]["message"]["content"].strip(),
                    "sources": [], "searched": False}
        msg = r1.json()["choices"][0]["message"]
        sources = []
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc["function"]["name"] == "web_search":
                    q = _json.loads(tc["function"]["arguments"]).get("query", prompt)
                    res = web_search(q)
                    sources = res
                    messages.append(msg)
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": _json.dumps(res)})
            r2 = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 600},
                timeout=25,
            ).json()
            answer = r2["choices"][0]["message"]["content"].strip()
            return {"answer": answer, "sources": sources, "searched": bool(sources)}
        return {"answer": msg["content"].strip(), "sources": [], "searched": False}
    except Exception as e:
        return {"answer": f"[deepsearch error: {e}] " + _router(prompt), "sources": [], "searched": False}

def agent_reply(text):
    backend = os.environ.get("AION_LLM", "").lower()
    if backend in ("", "stub") and not os.environ.get("GROQ_API_KEY"):
        return _router(text)
    try:
        return llm(text)
    except Exception as e:
        return f"[LLM error: {e}] " + _router(text)
# ───────────────────────── LAYER 2: SYSTEM MONITOR ─────────────────────────
def gpu_load():
    """Best-effort GPU probe (mirrors the TUI's SystemReader)."""
    import subprocess
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
    return None  # graceful N/A (e.g. no nvidia GPU)


def system_stats():
    net = psutil.net_io_counters()
    # per-core CPU + disk list (richer than the original single numbers)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    disks = []
    for mp in ("/",):  # keep the top bar simple; full list via /api/system
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
    """Real-life stats for the web HUD (pluggable Google/Apple/JSON)."""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(ROOT, "src"))
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
    """Obsidian-style graph for the web HUD (backlinks + degree + tags)."""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(ROOT, "src"))
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

# ───────────────────────── LAYER 3: PTY HOST (Terminal module) ─────────────
class PTYHost:
    def __init__(self, cols=100, rows=30, cmd="bash"):
        self.cols, self.rows = cols, rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.master, self.slave = pty.openpty()
        import fcntl, termios
        # make the slave the controlling terminal of the child session
        func = getattr(os, "setsid", None)
        self.proc = subprocess.Popen(
            shlex.split(cmd),
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
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            lines = [self.screen.display[i] for i in range(min(self.screen.cursor.y + 1, self.rows))]
            while len(lines) < self.rows:
                lines.append(" " * self.cols)
            return {"lines": lines[:self.rows], "cursor": [self.screen.cursor.x, self.screen.cursor.y]}

    def close(self):
        self.alive = False
        try:
            self.proc.terminate()
        except Exception:
            pass

HOSTS = {}

# ───────────────────────── HTTP HANDLER ────────────────────────────────────
def jresp(code, obj):
    body = json.dumps(obj).encode()
    return code, "application/json", body

class H(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == "/" or p == "/index.html":
            return self._send(200, "text/html", open(os.path.join(STATIC, "index.html"), "rb").read())
        if p.startswith("/static/"):
            fp = os.path.join(STATIC, os.path.basename(p))
            if os.path.exists(fp):
                return self._send(200, "application/octet-stream", open(fp, "rb").read())
            return self._send(404, "text/plain", b"no")
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
                nodes.append({"id": e, "label": e, "r": 9 if isdir else 6, "kind": "dir" if isdir else "file"})
                edges.append({"s": root, "t": e})
            return self._sendj({"nodes": nodes, "edges": edges, "path": path})
        if p == "/api/notes" or p == "/api/vault":
            g = vault_graph()
            return self._sendj(g)
        if p == "/api/notes/content":
            name = q.get("name", ["welcome"])[0]
            pp = os.path.join(NOTES_DIR, name + ".md")
            txt = open(pp).read() if os.path.exists(pp) else "# not found"
            return self._sendj({"name": name, "text": txt})
        if p == "/api/health":
            return self._sendj(health_summary())
        if p == "/api/system":
            return self._sendj(system_stats())
        if p == "/build.pdf":
            pp = os.path.join(ROOT, "build.pdf")
            if os.path.exists(pp):
                return self._send(200, "application/pdf", open(pp, "rb").read())
            return self._send(404, "text/plain", b"no pdf")
        return self._send(404, "text/plain", b"not found")

    def _sendj(self, obj):
        return self._send(200, "application/json", json.dumps(obj).encode())

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
            res = deepsearch_answer((body.get("text") or "").strip())
            return self._sendj({"reply": res["answer"], "sources": res["sources"],
                                "searched": res["searched"]})
        if p == "/api/search":
            q = body.get("query", "")
            return self._sendj({"results": web_search(q), "query": q})
        if p == "/api/notes/save":
            name = (body.get("name") or "note").replace("/", "")
            open(os.path.join(NOTES_DIR, name + ".md"), "w").write(body.get("text", ""))
            return self._sendj({"ok": True})
        if p == "/api/latex":
            tex = os.path.join(ROOT, "build.tex")
            open(tex, "w").write(body.get("src", ""))
            try:
                r = subprocess.run(["latexmk", "-pdf", "-interaction=nonstopmode", "build.tex"],
                                   cwd=ROOT, capture_output=True, text=True, timeout=60)
                ok = os.path.exists(os.path.join(ROOT, "build.pdf"))
                return self._sendj({"ok": ok, "pdf": "/build.pdf" if ok else None,
                                    "log": (r.stdout + r.stderr)[-2000:]})
            except Exception as e:
                return self._sendj({"ok": False, "pdf": None, "log": str(e)})
        return self._send(404, "text/plain", b"not found")

# ───────────────────────── WEBSOCKETS (term + events) ──────────────────────
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

async def hub(ws):
    path = ws_path(ws)
    if path == "/ws/term":
        await term_ws(ws)
    elif path == "/ws/events":
        await events_ws(ws)
    elif path == "/ws/editor":
        await editor_ws(ws)
    elif path == "/ws/files":
        await files_ws(ws)
    else:
        await ws.close()

async def editor_ws(ws):
    """Live PTY hosting `micro` (or any TUI) for the Editor module."""
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
                    host.close(); HOSTS.pop(id(ws), None); host = None
        await asyncio.gather(sender(), receiver())
    finally:
        if host:
            host.close(); HOSTS.pop(id(ws), None)

async def files_ws(ws):
    """Live PTY hosting the minimal TUI file manager (filetui.py)."""
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
                    fn = d.get("path", "/home/gio")
                    host = PTYHost(cols=100, rows=30,
                                   cmd=f"python3 {shlex.quote(os.path.join(ROOT, 'filetui.py'))} {shlex.quote(fn)}")
                    HOSTS[id(ws)] = host
                elif d.get("type") == "input" and host:
                    host.write(d.get("data", ""))
                elif d.get("type") == "close" and host:
                    host.close(); HOSTS.pop(id(ws), None); host = None
        await asyncio.gather(sender(), receiver())
    finally:
        if host:
            host.close(); HOSTS.pop(id(ws), None)

async def run_ws_async():
    async with websockets.serve(hub, "127.0.0.1", 8743):
        await asyncio.Future()  # run forever

def run_ws():
    asyncio.run(run_ws_async())

if __name__ == "__main__":
    _load_env()
    threading.Thread(target=run_ws, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", 8742), H)
    print("AION up: http://127.0.0.1:8742  (WS :8743)")
    httpd.serve_forever()
