"""
interpret.py — plain-language palette: say what you want, aion routes it.

Two layers, both feeding store._run_command with a canonical command:

  1. rules (free, instant): synonym + verb matching. "open my mail",
     "todo buy milk", "i use this for coding and writing", "watch this"
     all resolve without a model.
  2. llm_translate (blocking, run in an executor): when rules miss and the
     text reads like natural language, the cheap-tier LLM turns it into one
     canonical command, or NONE to leave it to the normal chat/spawn path.

Canonical commands stay available and unchanged — this layer only adds
forgiveness on top. Pure stdlib; no UI imports.
"""
from __future__ import annotations

import re

# ---- vocabulary ----------------------------------------------------------
APP_SYNONYMS: dict[str, str] = {
    # app id -> id itself plus everyday words for it
    "mail": "mail email posta inbox aerc neomutt",
    "edit": "edit editor write text micro nano vim note",
    "sheet": "sheet spreadsheet csv table visidata excel",
    "files": "files file filemanager folder browse yazi ranger",
    "git": "git repo lazygit commits",
    "rss": "rss news feed newsboat",
    "monitor": "monitor top btop htop processes cpu",
}
_WORD_TO_APP = {w: app for app, words in APP_SYNONYMS.items()
                for w in words.split()}

_LAUNCH_VERBS = ("open", "launch", "start", "run", "use", "show", "check")

SCOPE_KEYWORDS: dict[str, str] = {
    "dev": "dev code coding programming software repos development",
    "writing": "writing write notes thesis documents latex text",
    "media": "media photos pictures video videos music audio",
    "data": "data downloads datasets archive",
    "comms": "comms email chat messaging social",
    "finance": "finance money invoices accounting taxes",
}
_WORD_TO_SCOPE = {w: s for s, words in SCOPE_KEYWORDS.items()
                  for w in words.split()}

_TODO_ADD = re.compile(
    r"^(?:todo:?|remind me to|remember to|add (?:a )?(?:task|todo):?|task:?)\s+(.+)$")
_TODO_DONE = re.compile(r"^(?:done|finish(?:ed)?|complete[d]?|check)\s+#?(\d+)$")
_GOTO = re.compile(r"^(?:go ?to|goto|switch to)\s+(\w+)$")


def interpret(text: str) -> str | None:
    """Rule layer: return a canonical command, or None if no confident match.

    Never fires on text that already IS a canonical command — the store
    checks those first, so anything arriving here is free-form.
    """
    orig = text.strip()
    t = orig.lower()
    if not t:
        return None
    words = t.split()
    owords = orig.split()               # original casing for arguments

    # bare app name or synonym: "mail", "spreadsheet"
    if len(words) == 1 and words[0] in _WORD_TO_APP:
        return f"app {_WORD_TO_APP[words[0]]}"

    # todo forms (argument keeps the user's casing)
    m = _TODO_ADD.match(t)
    if m:
        return f"todo {orig[m.start(1):]}"
    m = _TODO_DONE.match(t)
    if m:
        return f"todo done {m.group(1)}"

    # "open/check <app-ish> [args]" — verb + a known synonym anywhere after it
    if words[0] in _LAUNCH_VERBS:
        pairs = [(lw, ow) for lw, ow in zip(words[1:], owords[1:])
                 if lw not in ("my", "the", "a", "an", "up")]
        for i, (lw, _) in enumerate(pairs):
            if lw in _WORD_TO_APP:
                args = " ".join(ow for _, ow in pairs[i + 1:])
                return f"app {_WORD_TO_APP[lw]}" + (f" {args}" if args else "")
    # "edit plan.md" — synonym leading with an argument
    if words[0] in _WORD_TO_APP and len(words) > 1:
        return f"app {_WORD_TO_APP[words[0]]} {' '.join(owords[1:])}"

    # apps listing
    if re.match(r"^(?:what |list |show )?(?:apps|programs)\??$", t):
        return "apps"

    # profile setup: "i use this (computer) for coding and writing"
    if re.match(r"^i use (?:this|it|the computer|my computer)", t) or \
            t.startswith("setup "):
        scopes = sorted({_WORD_TO_SCOPE[w] for w in re.findall(r"\w+", t)
                         if w in _WORD_TO_SCOPE})
        if scopes:
            return "setup " + " ".join(scopes)

    # data refresh
    if re.match(r"^(?:re)?scan(?: (?:my )?(?:disk|data|trackers))?$|^refresh data$", t):
        return "scan"

    # HITL approval (voice): answer a pending gate
    if re.match(r"^(?:approve|confirm|yes do it|go ahead|do it)$", t):
        return "hitl approve"
    if re.match(r"^(?:reject|deny|no don't|cancel that|stop)$", t):
        return "hitl reject"

    # observer
    if re.match(r"^(?:watch(?: this)?|observe(?: this)?|keep an eye on (?:this|it))$", t):
        return "observe ai"
    if re.match(r"^(?:stop watching|observe off|stop observing)$", t):
        return "observe off"

    # navigation: "go to vault"
    m = _GOTO.match(t)
    if m:
        return f"goto {m.group(1)}"

    if t in ("help", "?", "what can you do", "what can i do", "how does this work"):
        return "help"
    return None


# ---- LLM fallback (blocking; caller wraps in executor) -------------------
# The vocabulary in this prompt IS a contract with `store._run_command`: every
# name here is one the model will be told works. Both halves were internally
# consistent and disagreed with each other — the goto list named `memory`,
# `sys`, `hermes`, `skills`, `projects` and `swarm`, none of which are
# workspaces, and omitted `runs` and `net`, which are. A model obeying the
# prompt exactly produced "goto: no workspace 'sys'".
#
# So the lists are DERIVED rather than typed. A hardcoded vocabulary is only
# correct on the day it is written, and nothing about being wrong later is
# visible from either side.
_LLM_PROMPT_TMPL = """You translate a user's plain-language request into ONE aion palette command, or NONE.

Commands:
  app <{apps}> [args]   launch a program
  apps                    list programs
  todo <text> | todo done <n> | todo rm <n>
  setup <scopes from: dev writing media data comms finance>
  scan                    refresh disk trackers
  observe ai | observe off
  goto <{workspaces}>
  run <harness> <prompt>  run an AI task

Reply with exactly the command and nothing else. If the request is
conversation or doesn't map cleanly, reply NONE.

Request: """


def workspace_ids() -> list[str]:
    """The workspaces `goto` can actually reach, from the live config.

    Falls back to the three shipped by default rather than to nothing: a
    prompt with an empty list invites the model to invent targets, which is
    the failure this function exists to remove.
    """
    try:
        from .core import load_config
        ids = [str(w.get("id") or "")
               for w in (load_config().get("workspaces") or [])]
        return [i for i in ids if i]
    except Exception:                      # noqa: BLE001 — a prompt, not a gate
        return ["models", "tasks", "agent"]


def build_prompt() -> str:
    """The translator prompt, with its vocabulary read off the real thing."""
    return _LLM_PROMPT_TMPL.format(
        apps="|".join(APP_SYNONYMS),
        workspaces="|".join(workspace_ids()) or "models|tasks|agent")


def llm_translate(text: str, timeout: int = 10) -> str | None:
    """Ask the cheap-tier LLM chain to canonicalize. None on miss/failure."""
    try:
        from .llm import ChatSession, chat_send
        reply = chat_send(ChatSession(), build_prompt() + text, timeout=timeout)
    except Exception:
        return None
    if not reply:
        return None
    line = reply.strip().splitlines()[0].strip().strip("`")
    if not line or line.upper() == "NONE" or line.startswith("⚠"):
        return None
    first = line.split()[0]
    if first in ("app", "apps", "todo", "setup", "scan", "observe",
                 "goto", "run", "help"):
        return line
    return None
