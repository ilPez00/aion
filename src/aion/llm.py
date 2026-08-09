"""
llm.py — inline LLM chat client for aion's Agent workspace.

Backend chain: FCM local proxy -> Groq -> OpenRouter.
OmniRoute is available for explicit invocation via `compare` or direct call;
it is no longer part of the automatic fallback chain.
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
    "You are aion, a splitscreen HUD and application desktop shell. "
    "You manage processes, telemetry, tasks, and workspaces, rather than being a conversational assistant. "
    "You have access to: models (harnesses), tasks, memory (persistent notes), "
    "vault (obsidian-style notes graph), system stats, projects, and hermes agent data. "
    "Keep replies concise, objective, and useful.\n\n"
    "You can ACT by emitting tool calls inside your reply using this exact format:\n"
    "  <tool run>demo hello world</tool>      -> run a harness with a prompt\n"
    "  <tool rerun></tool>                    -> rerun the last failed task\n"
    "  <tool compare>which model is better?</tool> -> side-by-side model compare\n"
    "  <tool mem>status query</tool>          -> search persistent memory\n"
    "  <tool note>a useful fact</tool>        -> save a note\n"
    "  <tool think>a question</tool>          -> reason over memory (gbrain)\n"
    "  <tool state></tool>                    -> snapshot of current cockpit state\n"
    "  <tool vault>path::content</tool>       -> write a vault note (path like 'ideas/x')\n"
    "  <tool swarm>goal</tool>                -> plan a multi-agent swarm for a goal\n"
    "  <tool hermes>title::body</tool>        -> create a task on the Hermes kanban board\n"
    "For vault/hermes, separate the path/title from content/body with '::'. "
    "After a tool call, you will receive its result and can act on it. "
    "Use tools when the user asks you to DO something (run, compare, recall), "
    "not just talk. End with a short natural-language summary for the user."
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
    """Send a message to the LLM, get a reply. Blocks (runs in thread).

    Backend fallback chain: FCM local proxy -> Groq -> OpenRouter.
    OmniRoute is excluded from the automatic chain; use `compare` with
    provider 'omniroute' for explicit side-by-side invocation.
    The first backend that returns a real reply wins; if all fail, returns a
    '⚠️' diagnostic string naming which providers were tried.
    """
    session.add("user", message)
    session.pending = True
    try:
        api_msgs = session.as_api_messages()
        tried: list[str] = []
        for name, fn in (("FCM", _fcm_chat),
                         ("Groq", _groq_chat), ("OpenRouter", _openrouter_chat)):
            reply = fn(api_msgs, timeout=timeout)
            if _is_ok(reply):
                session.add("assistant", reply)  # type: ignore[arg-type]
                return reply
            tried.append(name)
        session.add("assistant",
                    f"⚠️ LLM unavailable (tried {', '.join(tried)}).")
        return f"⚠️ LLM unavailable (tried {', '.join(tried)})."
    finally:
        session.pending = False


def _omniroute_chat(messages: list[dict], timeout: int = 30) -> str | None:
    """Try the local OmniRoute router (agy → Claude Code Pro → free stack).

    Env-driven (freerouting contract): OMNIROUTE_BASE_URL / FREEROUTING_BASE_URL,
    OMNIROUTE_API_KEY / FREEROUTING_API_KEY, OMNIROUTE_MODEL / FREEROUTING_MODEL
    (default agy-claude-first). Returns None when unconfigured so the chain falls
    through to FCM/Groq/OpenRouter unchanged.
    """
    import urllib.request
    import urllib.error
    base = (os.environ.get("OMNIROUTE_BASE_URL")
            or os.environ.get("FREEROUTING_BASE_URL")
            or "http://localhost:20128/v1").rstrip("/")
    key = (os.environ.get("OMNIROUTE_API_KEY")
           or os.environ.get("FREEROUTING_API_KEY") or "")
    model = (os.environ.get("OMNIROUTE_MODEL")
             or os.environ.get("FREEROUTING_MODEL") or "agy-claude-first")
    if not key:
        return None  # not configured → skip to next backend
    try:
        data = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 800,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            base + "/chat/completions",
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
    except urllib.error.HTTPError as e:
        return f"⚠️ OmniRoute HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"⚠️ OmniRoute unreachable: {e.reason if hasattr(e, 'reason') else e}"
    except (ValueError, KeyError, IndexError) as e:
        return f"⚠️ OmniRoute bad response: {e}"


def _fcm_chat(messages: list[dict], timeout: int = 30) -> str | None:
    """Try FCM local proxy. Returns text, a '⚠️ ...' error string, or None."""
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
    except urllib.error.HTTPError as e:
        return f"⚠️ FCM HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"⚠️ FCM unreachable: {e.reason if hasattr(e, 'reason') else e}"
    except (ValueError, KeyError, IndexError) as e:
        return f"⚠️ FCM bad response: {e}"


def _groq_chat(messages: list[dict], timeout: int = 30) -> str | None:
    """Fallback to Groq API if key is available.

    Returns the reply, or a human-readable error string prefixed with '⚠️'
    (so the UI can show WHY a provider failed instead of a silent blank).
    """
    _load_env()
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return "⚠️ GROQ_API_KEY not set"
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
    except urllib.error.HTTPError as e:
        # surface the real status so a blocked/expired key is visible
        return f"⚠️ Groq HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"⚠️ Groq unreachable: {e.reason if hasattr(e, 'reason') else e}"
    except (ValueError, KeyError, IndexError) as e:
        return f"⚠️ Groq bad response: {e}"


def _oa_compatible(messages: list[dict], *, url: str, key: str, model: str,
                   auth_scheme: str = "Bearer", timeout: int = 30) -> str | None:
    """Generic OpenAI-compatible chat completion. Returns text or '⚠️ ...'."""
    import urllib.request
    import urllib.error
    try:
        data = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 800,
        }).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"{auth_scheme} {key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            return body["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        return f"⚠️ HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"⚠️ unreachable: {e.reason if hasattr(e, 'reason') else e}"
    except (ValueError, KeyError, IndexError) as e:
        return f"⚠️ bad response: {e}"


def _openrouter_chat(messages: list[dict], timeout: int = 30) -> str | None:
    """Third-tier fallback: OpenRouter (many models, one key)."""
    _load_env()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return "⚠️ OPENROUTER_API_KEY not set"
    return _oa_compatible(
        messages,
        url="https://openrouter.ai/api/v1/chat/completions",
        key=key,
        model="openai/gpt-4o-mini",  # cheap, reliable default
        timeout=timeout,
    )


def _is_ok(reply: str | None) -> bool:
    """A reply counts as success only if it's non-empty and not a '⚠️' warning."""
    return bool(reply) and not (isinstance(reply, str) and reply.startswith("⚠️"))


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
            "content": "💬 Agent with tools: ask it to DO things — 'run demo hello', 'compare <q>', 'mem <query>', 'rerun', 'note <fact>'. The agent calls cockpit tools and reports back.",
            "pending": False,
        })
    return out


def chat_send_multi(prompt: str, providers: list[str], timeout: int = 30) -> dict[str, str]:
    """Side-by-side model comparison. Returns {provider: reply_or_warning}.

    Provider keys map to backends: 'fcm' -> local FCM proxy, 'groq' -> Groq API,
    'openrouter' -> OpenRouter. Replies are capped at 400 chars so the
    side-by-side UI stays readable. A dead backend yields a '⚠️ ...' string
    instead of crashing.
    """
    api_msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    out: dict[str, str] = {}
    for prov in providers:
        if prov == "omniroute":
            reply = _omniroute_chat(api_msgs, timeout=timeout)
        elif prov == "groq":
            reply = _groq_chat(api_msgs, timeout=timeout)
        elif prov == "openrouter":
            reply = _openrouter_chat(api_msgs, timeout=timeout)
        else:  # fcm (and any unknown key)
            reply = _fcm_chat(api_msgs, timeout=timeout)
        if _is_ok(reply):
            out[prov] = reply[:400]  # type: ignore[index]
        else:
            out[prov] = reply or f"⚠️ {prov} unavailable"
    return out


def agent_run(session: ChatSession, env, max_steps: int = 3, timeout: int = 30) -> str:
    """Agent loop: chat, let the LLM emit tool calls, execute them, re-prompt.

    Flow:
      1. send user message -> get reply
      2. if reply has <tool ...> tags, execute them via `env`, append the
         observations as a tool-result turn, and ask the LLM to continue
      3. repeat up to max_steps, then return the final natural-language reply

    `env` is an aion.agent.ToolEnv with real callables. Falls back to a plain
    chat reply if no tools are emitted (so it still works as a normal chat).
    """
    from .agent import execute

    # initial turn
    reply = chat_send(session, _last_user(session), timeout=timeout)
    steps = 0
    while steps < max_steps:
        natural, obs = execute(reply, env)
        if not obs:
            # no tool calls -> final answer
            return natural or reply
        # record the tool exchange in the session so context carries forward
        session.add("assistant", reply)
        session.add("user", f"[tool results]\n{obs}\n\nContinue using the results above.")
        reply = chat_send(session, _last_user(session), timeout=timeout)
        steps += 1
    # return whatever the last reply was (tools stripped)
    natural, _ = execute(reply, env)
    return natural or reply


def _last_user(session: ChatSession) -> str:
    for m in reversed(session.messages):
        if m.role == "user":
            return m.content
    return ""
