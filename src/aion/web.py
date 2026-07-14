"""
web.py — Aion DeepSearch module.

Lets the agent answer questions using the LIVE web. Pipeline:
  1. DuckDuckGo HTML scrape (timeout-safe, no API key) -> result list
  2. Groq LLM (OpenAI-compatible) with a web_search function tool: it may call
     the tool, we run it, then it synthesizes a cited answer. Groq occasionally
     emits a malformed tool call (400) -> we retry WITHOUT tools so the user
     still gets an answer instead of an error.

Secrets: GROQ_API_KEY is loaded from the shared .env files at import time of the
server; this module reads it from the environment at call time (never hard-coded).
"""
from __future__ import annotations

import os
import re
import urllib.parse


def _load_env() -> None:
    """Pull backend keys from the shared .env files (no secrets in code)."""
    try:
        from dotenv import load_dotenv  # type: ignore
        for f in ("/home/gio/.env", "/home/gio/.hermes/.env"):
            if os.path.exists(f):
                try:
                    load_dotenv(f)
                except Exception:
                    for line in open(f):
                        if line.startswith(("GROQ_API_KEY=",)):
                            k, v = line.strip().split("=", 1)
                            os.environ.setdefault(k, v)
    except Exception:
        for f in ("/home/gio/.env", "/home/gio/.hermes/.env"):
            if os.path.exists(f):
                for line in open(f):
                    if line.startswith(("GROQ_API_KEY=",)):
                        k, v = line.strip().split("=", 1)
                        os.environ.setdefault(k, v)


def web_search(q: str, n: int = 5) -> list[dict]:
    """DuckDuckGo HTML scrape. Returns list of {title,url,snippet}. Timeout-safe."""
    import requests
    try:
        html = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": q},
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            timeout=8,
        ).text
    except Exception as e:  # noqa: BLE001
        return [{"title": f"(search failed: {e})", "url": "", "snippet": ""}]
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    out: list[dict] = []
    for url, title, snip in blocks[:n]:
        url = urllib.parse.unquote(re.sub(r".*uddg=([^&]+).*", r"\1", url))
        title = re.sub(r"<[^>]+>", "", title).strip()
        snip = re.sub(r"<[^>]+>", "", snip).strip()
        if title:
            out.append({"title": title, "url": url, "snippet": snip})
    if not out:
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


def deepsearch_answer(prompt: str, model: str = "llama-3.3-70b-versatile") -> dict:
    """ReAct-style web answer. Returns {answer, sources, searched}."""
    import json as _json
    import requests
    _load_env()
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return {"answer": _fallback(prompt), "sources": [], "searched": False}
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
            r0 = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 600},
                timeout=25,
            ).json()
            return {"answer": r0["choices"][0]["message"]["content"].strip(),
                    "sources": [], "searched": False}
        msg = r1.json()["choices"][0]["message"]
        sources: list[dict] = []
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
            return {"answer": r2["choices"][0]["message"]["content"].strip(),
                    "sources": sources, "searched": bool(sources)}
        return {"answer": msg["content"].strip(), "sources": [], "searched": False}
    except Exception as e:  # noqa: BLE001
        return {"answer": f"[deepsearch error: {e}] " + _fallback(prompt),
                "sources": [], "searched": False}


def _fallback(prompt: str) -> str:
    p = prompt.lower()
    if "search" in p or "web" in p or "latest" in p or "who" in p or "what" in p:
        return ("I can't reach the web right now. Try again, or rephrase. "
                "(Aion web module needs network + GROQ_API_KEY.)")
    return ("Aion: I heard you, but the web module is offline. "
            "Route me to terminal / files / browser / editor / latex / notes.")
