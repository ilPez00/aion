"""Agent loop — tool-calling for the inline LLM chat (Cycle 10).

Pure + testable: parse tool calls from an LLM reply, execute them against a
small ToolEnv interface, and format observations. No I/O here; the store
supplies the real ToolEnv (harnesses, memory, rerun, compare).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# LLM emits tool calls as:  <tool name>args</tool>
_TOOL_RE = re.compile(r"<tool\s+([a-z_]+)>([^<]*)</tool>", re.IGNORECASE)


@dataclass
class ToolEnv:
    """What the agent can do. The store fills these with real callables."""
    run: object = None          # run(harness: str, prompt: str) -> str
    rerun: object = None        # rerun() -> str
    compare: object = None      # compare(prompt: str) -> str
    mem: object = None          # mem(query: str) -> str
    note: object = None         # note(fact: str) -> str
    state: object = None        # state() -> str (snapshot of cockpit)

    def has(self, name: str) -> bool:
        return getattr(self, name, None) is not None


def parse_tools(text: str) -> list[tuple[str, str]]:
    """Extract (name, args) tool calls from an LLM reply."""
    return [(m.group(1).lower(), m.group(2).strip()) for m in _TOOL_RE.finditer(text)]


def strip_tools(text: str) -> str:
    """Remove tool tags, leaving the natural-language reply."""
    import re as _re
    cleaned = _TOOL_RE.sub("", text)
    return _re.sub(r"\s{2,}", " ", cleaned).strip()


def execute(text: str, env: ToolEnv) -> tuple[str, str]:
    """Given an LLM reply, execute any tool calls.

    Returns (natural_reply, observations) where observations is a concatenated
    tool-result log (empty if no tools were called).
    """
    calls = parse_tools(text)
    if not calls:
        return strip_tools(text), ""
    parts: list[str] = []
    for name, args in calls:
        if not env.has(name):
            parts.append(f"[tool {name}: unknown]")
            continue
        try:
            if name == "run":
                sp = args.split(" ", 1)
                hid = sp[0]
                prompt = sp[1] if len(sp) > 1 else ""
                out = env.run(hid, prompt)
            elif name == "rerun":
                out = env.rerun()
            elif name == "compare":
                out = env.compare(args)
            elif name == "mem":
                out = env.mem(args)
            elif name == "note":
                out = env.note(args)
            elif name == "state":
                out = env.state()
            else:
                out = f"[tool {name}: unsupported]"
        except Exception as e:  # noqa: BLE001
            out = f"[tool {name} error: {e}]"
        parts.append(f"[{name}] {out}")
    return strip_tools(text), "\n".join(parts)
