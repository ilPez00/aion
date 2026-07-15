"""
llm.py — inline LLM chat client for aion's Agent workspace.

Routes messages through the free-coding-models (FCM) local proxy at
http://localhost:19280/v1 so no API key is needed. Falls back to Groq
if FCM is unreachable and GROQ_API_KEY is set.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any


FCM_URL = "http://localhost:19280/v1/chat/completions"
DEFAULT_MODEL = "fcm"  # routes through the FCM router

SYSTEM_PROMPT = (
    "You are aion, an AI cockpit assistant living inside a multi-workspace TUI. "
    "You have access to: models (harnesses), tasks, memory (persistent notes), "
    "vault (obsidian-style notes graph), system stats, projects, and hermes agent data. "
    "Keep replies concise and useful. You can run tasks by saying 'run <harness> <prompt>'."
)


@dataclass
class ChatMessage:
    role: str          # "user" | "assistant" | "system"
    content: str
    ts: float = field(default_factory=time.time)


@dataclass
class ChatSession:
    messages: list[ChatMessage] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    pending: bool = False        # true while waiting for a response

    def add(self, role: str, content: str) -> ChatMessage:
        m = ChatMessage(role=role, content=content)
        self.messages.append(m)
        # cap context window to last 40 messages
        if len(self.messages) > 40:
            # keep system prompt + last 39
            self.messages = self.messages[-39:]
        return m

    def as_api_messages(self) -> list[dict]:
        out = []
        # inject system prompt as first message (overriding any old one)
        out.append({"role": "system", "content": SYSTEM_PROMPT})
        for m in self.messages:
            if m.role == "system":
                continue  # skip old system prompts
            out.append({"role": m.role, "content": m.content})
        return out


def _load_env() -> None:
    """Ensure FCM/groq env vars are loaded."""
    try:
        from dotenv import load_dotenv
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


def chat_send(session: ChatSession, message: str, timeout: int = 30) -> str:
    """Send a message to the LLM, get a reply. Blocks (runs in thread)."""
    session.add("user", message)
    session.pending = True
    try:
        api_msgs = session.as_api_messages()
        # Try FCM local proxy first
        reply = _fcm_chat(api_msgs, timeout=timeout)
        if reply is None:
            # Fall back to Groq
            reply = _groq_chat(api_msgs, timeout=timeout)
        if reply is None:
            reply = "⚠️ LLM unavailable (FCM proxy + Groq both unreachable)."
        session.add("assistant", reply)
        return reply
    finally:
        session.pending = False


def _fcm_chat(messages: list[dict], timeout: int = 30) -> str | None:
    """Try FCM local proxy. Returns text or None."""
    import urllib.request
    import urllib.error
    try:
        data = json.dumps({
            "model": "fcm",
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 800,
        }).encode()
        req = urllib.request.Request(
            FCM_URL,
            data=data,
            headers={
                "Authorization": "Bearer fcm-local",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            return body["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        return None


def _groq_chat(messages: list[dict], timeout: int = 30) -> str | None:
    """Fallback to Groq API if key is available."""
    _load_env()
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None
    import urllib.request
    import urllib.error
    try:
        data = json.dumps({
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 800,
        }).encode()
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            return body["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        return None


def format_conversation(session: ChatSession) -> list[dict]:
    """Return display-ready chat items for the agent workspace."""
    out: list[dict] = []
    for i, m in enumerate(session.messages):
        if m.role == "system":
            continue
        out.append({
            "id": f"msg_{i}",
            "role": m.role,
            "content": m.content[:400],
            "ts": m.ts,
        })
    if session.pending:
        out.append({
            "id": "pending",
            "role": "assistant",
            "content": "⏳ thinking...",
            "pending": True,
        })
    if not out:
        out.append({
            "id": "empty",
            "role": "system",
            "content": "💬 Type a message in the command palette to chat with the AI. Or: 'run demo hello', 'tier cheap', 'mem <query>', 'theme matrix'.",
            "pending": False,
        })
    return out


def chat_send_multi(prompt: str, providers: list[str], timeout: int = 30) -> dict[str, str]:
    """Side-by-side model comparison. Returns {provider: reply_or_warning}.

    Provider keys map to backends: 'fcm' -> local FCM proxy, 'groq' -> Groq API.
    Any other key currently falls back to FCM (single backend today). Replies are
    capped at 400 chars so the side-by-side UI stays readable.
    """
    api_msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    out: dict[str, str] = {}
    for prov in providers:
        if prov == "groq":
            reply = _groq_chat(api_msgs, timeout=timeout)
        else:  # default: fcm (and any unknown key)
            reply = _fcm_chat(api_msgs, timeout=timeout)
            if reply is None and prov == "fcm":
                # fcm is the local-only proxy; don't double-count groq unless asked
                pass
        if reply is None:
            out[prov] = f"⚠️ {prov} unavailable"
        else:
            out[prov] = reply[:400]
    return out
