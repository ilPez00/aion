"""
gbrain.py — aion's MCP bridge to gbrain (memory brain backend).

Replaces the flat memory.json with a proper hybrid retrieval brain via gbrain's
MCP protocol. Two connection modes:

  stdio  — spawn gbrain serve as subprocess (local PGLite brain)
  http   — connect to remote gbrain by URL + bearer token

Graceful degradation: if gbrain is unreachable, falls back to MemoryStore.

gbrain MCP tools exposed as aion commands:
    note <text>       → gbrain capture
    mem <query>       → gbrain search | think
    think <question>  → gbrain think (synthesis + gap analysis)
    gbrain status     → gbrain get_brain_identity
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .core import load_config


def _gbrain_bin() -> str | None:
    return shutil.which("gbrain")


def _gbrain_token() -> str | None:
    return os.environ.get("GBRAIN_REMOTE_TOKEN") or os.environ.get("GBRAIN_TOKEN")


def _gbrain_remote_url() -> str | None:
    """If configured, use a remote gbrain instead of spawning local."""
    cfg = load_config()
    return cfg.get("gbrain", {}).get("remote_url") or os.environ.get("GBRAIN_REMOTE_URL")


# ---------------------------------------------------------------------------
# MCP client helpers (stdin/stdout JSON-RPC for local gbrain serve)
# ---------------------------------------------------------------------------

def _mcp_call(tool: str, args: dict | None = None,
              timeout: float = 30) -> dict | None:
    """Call a gbrain MCP tool via stdio subprocess (local mode).

    Spawns 'gbrain serve' in tool-call mode, sends one JSON-RPC request,
    reads one response, and shuts down.
    """
    bin_ = _gbrain_bin()
    if not bin_:
        return None
    remote = _gbrain_remote_url()
    if remote:
        return _http_mcp_call(remote, tool, args, timeout)
    try:
        req = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        })
        proc = subprocess.run(
            [bin_, "serve"],
            input=req,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GBRAIN_MCP_MODE": "json-rpc"},
        )
        if proc.returncode != 0:
            return None
        resp = json.loads(proc.stdout)
        result = resp.get("result", {})
        content = result.get("content", [])
        if content:
            text = content[0].get("text", "{}")
            return json.loads(text)
        return result
    except Exception:
        return None


def _http_mcp_call(url: str, tool: str, args: dict | None = None,
                   timeout: float = 30) -> dict | None:
    """Call gbrain MCP tool via HTTP POST (remote mode)."""
    import urllib.request as req
    import urllib.error
    mcp_url = url.rstrip("/") + "/mcp"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    }).encode()
    headers = {"Content-Type": "application/json"}
    token = _gbrain_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = req.Request(mcp_url, data=body, headers=headers, method="POST")
        with req.urlopen(r, timeout=timeout) as resp:
            data = json.loads(resp.read())
        result = data.get("result", {})
        content = result.get("content", [])
        if content:
            text = content[0].get("text", "{}")
            return json.loads(text)
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API (called by store.py)
# ---------------------------------------------------------------------------

def available() -> bool:
    """Check if gbrain is reachable (local binary or remote URL)."""
    if _gbrain_remote_url():
        return _http_mcp_call(_gbrain_remote_url(), "get_brain_identity") is not None
    bin_ = _gbrain_bin()
    if not bin_:
        return False
    try:
        result = subprocess.run(
            [bin_, "doctor", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def identity() -> dict:
    """Get brain identity and stats."""
    result = _mcp_call("get_brain_identity")
    if result:
        return result
    stats = _mcp_call("get_stats")
    if stats:
        return {"version": "unknown", **stats}
    return {"error": "gbrain unreachable"}


def capture(text: str) -> dict:
    """Remember a fact / thought. Replaces memory.py add()."""
    result = _mcp_call("put_page", {
        "slug": f"inbox/{int(time.time())}",
        "content": text,
    })
    if result:
        return {"ok": True, "slug": result.get("slug", "")}
    return {"ok": False, "error": "gbrain unreachable"}


def search(query: str, limit: int = 10) -> list[dict]:
    """Hybrid search (keyword + vector). Replaces memory.py search()."""
    result = _mcp_call("search", {"query": query, "limit": limit})
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return result.get("results", result.get("pages", []))


def recall(query: str, limit: int = 10) -> list[dict]:
    """Query hot memory facts."""
    result = _mcp_call("recall", {"grep": query, "limit": limit})
    if result is None:
        return []
    facts = result.get("facts", [])
    if isinstance(result, list):
        return result
    return facts


def think(question: str) -> dict:
    """Multi-hop synthesis with gap analysis.

    Returns answer with citations and explicit "what the brain doesn't know".
    """
    result = _mcp_call("think", {"question": question})
    if result is None:
        return {"answer": None, "error": "gbrain unreachable"}
    return result


def volunteer_context(window: str,
                      prior_context: str | None = None,
                      max_pages: int = 3) -> list[dict]:
    """Zero-LLM push-based context retrieval.

    Given recent conversation turns, returns relevant pages without
    an explicit query.
    """
    params: dict[str, Any] = {
        "window": window,
        "max_pages": max_pages,
        "min_confidence": 0.7,
    }
    if prior_context:
        params["prior_context"] = prior_context
    result = _mcp_call("volunteer_context", params)
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return result.get("pages", result.get("results", []))


def extract_facts(turn_text: str,
                  entity_hints: list[str] | None = None) -> dict:
    """Extract knowledge facts from a conversation turn into hot memory."""
    params: dict[str, Any] = {"turn_text": turn_text}
    if entity_hints:
        params["entity_hints"] = entity_hints
    result = _mcp_call("extract_facts", params)
    if result is None:
        return {"inserted": 0, "error": "gbrain unreachable"}
    return result


# ---------------------------------------------------------------------------
# Store integration: wraps MemoryStore with gbrain fallback
# ---------------------------------------------------------------------------

from .memory import MemoryStore as _FallbackStore


class BrainStore:
    """Drop-in replacement for MemoryStore with gbrain backend.

    Falls back to MemoryStore (memory.json) when gbrain is unreachable.
    """

    def __init__(self) -> None:
        self._fallback = _FallbackStore()
        self._gbrain_ok = False
        self._last_check = 0.0

    def _check_gbrain(self) -> bool:
        if time.time() - self._last_check < 30:
            return self._gbrain_ok
        self._last_check = time.time()
        self._gbrain_ok = available()
        return self._gbrain_ok

    def add(self, text: str) -> dict:
        if self._check_gbrain():
            result = capture(text)
            if result.get("ok"):
                return {"text": text, "source": "gbrain", "slug": result.get("slug", "")}
        return self._fallback.add(text)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        if self._check_gbrain():
            results = search(query, limit=limit)
            if results:
                return [
                    {
                        "text": r.get("title", r.get("chunk_text", r.get("text", ""))),
                        "slug": r.get("slug", ""),
                        "score": r.get("score", 0),
                        "source": "gbrain",
                    }
                    for r in results
                ]
        return [
            {"text": f["text"], "when": f.get("when", ""), "source": "memory"}
            for f in self._fallback.search(query)
        ]

    def recall(self, query: str, limit: int = 10) -> list[dict]:
        if self._check_gbrain():
            return recall(query, limit=limit)
        return []

    def think(self, question: str) -> dict | None:
        if self._check_gbrain():
            return think(question)
        return None

    @property
    def facts(self) -> list:
        """Compatible with MemoryStore.facts — used by tests."""
        return self._fallback.facts

    def items(self) -> list[dict]:
        """Compatible with MemoryStore.items() — used by vault workspace."""
        return self._fallback.items()

    def forget(self, index: int) -> bool:
        return self._fallback.forget(index)
