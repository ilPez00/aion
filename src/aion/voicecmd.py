"""voicecmd.py — turn a spoken phrase into a HUD action.

Pure grammar: text in, an action dict out. No audio, no network, no UI — so
the whole vocabulary is testable, which matters more here than usual because
you cannot eyeball a speech interface the way you can a button.

Why not just reuse interpret.py
-------------------------------
`interpret.py` resolves free text into *cockpit commands* (`app mail`,
`todo buy milk`) for the TUI's store to run. The web HUD needs *HUD actions*
— switch module, rescan a directory, toggle the list view, answer a gate.
Those overlap but are not the same vocabulary, so this maps the HUD half and
delegates the rest to `interpret` rather than duplicating it.

The safety line: voice may deny, never approve
----------------------------------------------
Speech is an ambient, low-confidence channel. A microphone hears the room —
a podcast, a colleague, a video — and an approval gate releases a privileged
action (`rm -rf`, a force push). So:

  * "reject" / "deny" / "cancel that"  -> resolves the gate as DENIED
  * "approve" / "yes" / "go ahead"     -> REFUSED, with a reason

Denying moves toward the state the engine already defaults to, so a
misheard rejection costs a re-run. Approving is the one direction where a
mishearing is unrecoverable, and it stays a deliberate button press. This is
deliberately stricter than the TUI, where the gate is answered by a physical
device you are holding.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Modules the HUD can switch to, with the words people actually say.
MODULE_WORDS: dict[str, str] = {
    "desk": "desk desktop home todos memory",
    "files": "files file filesystem folders directory disk",
    "agents": "agents agent tasks fleet work processes",
    "repos": "repos repo repositories git worktrees",
    "vault": "vault notes obsidian wiki",
    "system": "system telemetry stats monitor cpu",
    "board": "board boards kanban cards",
    "term": "term terminal shell console",
    "agent": "chat assistant llm",
    "latex": "latex tex document pdf",
    "settings": "settings config configuration providers preferences",
}
_WORD_TO_MODULE = {w: m for m, words in MODULE_WORDS.items()
                   for w in words.split()}

_GOTO_VERBS = ("go to", "goto", "show", "open", "switch to", "take me to",
               "bring up", "display")

# Below this, the phrase is shown but not executed. Web Speech reports a
# confidence per result; a low score usually means the room was noisy, and
# acting on a guess is worse than asking again.
MIN_CONFIDENCE = 0.55

_SCAN = re.compile(r"^(?:scan|browse|explore|open (?:the )?(?:folder|directory|dir))\s+(.+)$")
_FILTER = re.compile(r"^(?:filter|highlight|narrow(?: to)?)\s+(.+)$")
_SEARCH = re.compile(r"^(?:search(?: for)?|find|look for)\s+(.+)$")
_ISOLATE = re.compile(r"^(?:isolate|focus(?: on)?|only)\s+(.+)$")

APPROVE_WORDS = ("approve", "approved", "accept", "allow", "yes go ahead",
                 "go ahead", "confirm", "permit", "authorise", "authorize")
REJECT_WORDS = ("reject", "deny", "denied", "refuse", "cancel that", "no",
                "stop", "abort", "block")


@dataclass
class Action:
    """What the HUD should do. `ok=False` means say why, do nothing."""
    action: str = "none"
    ok: bool = True
    args: dict = field(default_factory=dict)
    say: str = ""              # what the HUD should tell the user
    transcript: str = ""
    confidence: float = 1.0

    def as_dict(self) -> dict:
        return {"action": self.action, "ok": self.ok, "args": self.args,
                "say": self.say, "transcript": self.transcript,
                "confidence": round(self.confidence, 3)}


def _clean(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[.!?,;]+$", "", t)
    # wake words are stripped, not required — the mic is already opt-in
    t = re.sub(r"^(?:hey |ok |okay )?(?:aion|ayon|ion|iron)\b[,:]?\s*", "", t)
    return t.strip()


def _module_for(phrase: str) -> str | None:
    for word in re.split(r"\W+", phrase):
        if word in _WORD_TO_MODULE:
            return _WORD_TO_MODULE[word]
    return None


def parse(text: str, confidence: float = 1.0) -> Action:
    """Map a spoken phrase to a HUD action."""
    raw = (text or "").strip()
    t = _clean(raw)
    if not t:
        return Action(action="none", ok=False, say="nothing heard",
                      transcript=raw, confidence=confidence)

    if confidence < MIN_CONFIDENCE:
        return Action(action="unsure", ok=False, transcript=raw,
                      confidence=confidence,
                      say=f"not sure I heard that — “{raw}”?")

    def done(action, **args):
        return Action(action=action, args=args, transcript=raw,
                      confidence=confidence)

    # ── gates: deny is allowed, approve is not ───────────────────────────
    if any(t == w or t.startswith(w + " ") for w in REJECT_WORDS):
        return Action(action="gate", args={"decision": "reject"},
                      transcript=raw, confidence=confidence,
                      say="rejecting the pending approval")
    if any(t == w or t.startswith(w + " ") for w in APPROVE_WORDS):
        return Action(
            action="gate", ok=False, args={"decision": "approve"},
            transcript=raw, confidence=confidence,
            say="approval has to be a button press, not a voice command — "
                "the gate is highlighted")

    # ── view controls ────────────────────────────────────────────────────
    if t in ("list", "list view", "show list", "show the list", "table"):
        return done("view", mode="list")
    if t in ("graph", "graph view", "show graph", "show the graph", "map"):
        return done("view", mode="graph")
    if t in ("fit", "fit the graph", "zoom out", "reset view", "reset zoom"):
        return done("fit")
    if t in ("refresh", "rescan", "reload", "scan again", "update"):
        return done("refresh")
    if t in ("clear", "clear filter", "show everything", "reset filter",
             "clear the filter"):
        return done("filter", query="")
    if t in ("back", "go back", "previous"):
        return done("back")
    if t in ("up", "go up", "parent", "up a level", "parent folder"):
        return done("up")
    if t in ("help", "what can i say", "commands"):
        return done("help")

    # ── navigation ───────────────────────────────────────────────────────
    for verb in _GOTO_VERBS:
        if t.startswith(verb + " "):
            rest = t[len(verb) + 1:].strip()
            mod = _module_for(rest)
            if mod:
                return done("goto", module=mod)
            # "open <folder>" that is not a module is a scan, not a failure
            return done("scan", dir=rest)
    mod = _module_for(t) if len(t.split()) <= 2 else None
    if mod:
        return done("goto", module=mod)

    # ── graph + directory verbs ──────────────────────────────────────────
    m = _SCAN.match(t)
    if m:
        return done("scan", dir=m.group(1).strip())
    m = _ISOLATE.match(t)
    if m:
        return done("isolate", query=m.group(1).strip())
    m = _FILTER.match(t)
    if m:
        return done("filter", query=m.group(1).strip())
    m = _SEARCH.match(t)
    if m:
        return done("search", query=m.group(1).strip())

    # ── delegate to the cockpit's plain-language interpreter ─────────────
    # Todos, app launches and scope setup already have a vocabulary; there is
    # no reason for this module to grow a second one.
    try:
        from .interpret import interpret
        cmd = interpret(raw)
    except Exception:  # noqa: BLE001
        cmd = None
    if cmd:
        return Action(action="command", args={"command": cmd}, transcript=raw,
                      confidence=confidence, say=f"→ {cmd}")

    # ── otherwise: it was speech for the agent, not for the HUD ──────────
    return Action(action="chat", args={"text": raw}, transcript=raw,
                  confidence=confidence)


def vocabulary() -> list[dict]:
    """What the user can say — powers the spoken help and the cheat sheet."""
    return [
        {"say": "go to files / show agents / open vault", "does": "switch module"},
        {"say": "scan ~/dev", "does": "graph that directory"},
        {"say": "filter parser", "does": "dim everything that does not match"},
        {"say": "search for lexer", "does": "open the palette on that query"},
        {"say": "isolate <hub>", "does": "show one cluster only"},
        {"say": "list / graph", "does": "swap representation"},
        {"say": "fit / refresh / clear / back / up", "does": "view controls"},
        {"say": "todo buy milk", "does": "add a todo"},
        {"say": "reject", "does": "deny a pending approval gate"},
        {"say": "approve", "does": "refused — approval is a button, by design"},
    ]
