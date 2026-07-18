"""
observer.py — the observant AI HUD for launched programs.

While a program runs in the Term workspace, the Observer watches its pyte
screen and keeps a one-line status the right rail renders under OBSERVER.

Two layers:
  - heuristic (always on, free): screen-change activity, error/warning
    pattern counts scraped from the visible screen.
  - AI one-liner (opt-in, `observe ai`): every AI_INTERVAL_S the trimmed
    screen text goes to the cheap-tier LLM for a "what is happening /
    anything wrong" single sentence. Failures degrade silently back to
    the heuristic line. `observe off` disables the AI layer.

No Textual imports — app.py calls tick() from its timer and renders
.status_line / .ai_line.
"""
from __future__ import annotations

import re
import time

_ERR = re.compile(r"\b(error|failed|fatal|traceback|exception|panic|denied)\b",
                  re.IGNORECASE)
_WARN = re.compile(r"\b(warn|warning|deprecated)\b", re.IGNORECASE)

AI_INTERVAL_S = 20
_AI_PROMPT = (
    "You are a silent HUD observer watching a terminal program over the "
    "user's shoulder. In ONE short sentence (max 12 words): what is the "
    "program doing, and flag anything that looks wrong. Screen:\n\n")


class Observer:
    def __init__(self):
        self.app_name: str = ""
        self.started: float = 0.0
        self.ai_enabled: bool = False
        self.status_line: str = ""
        self.ai_line: str = ""
        self._last_screen: str = ""
        self._last_change: float = 0.0
        self._last_ai: float = 0.0
        self._ai_busy: bool = False

    # ---- lifecycle -------------------------------------------------------
    def attach(self, app_name: str) -> None:
        self.app_name = app_name
        self.started = time.time()
        self.status_line = f"● {app_name} starting"
        self.ai_line = ""
        self._last_screen = ""

    def detach(self) -> None:
        self.app_name = ""
        self.status_line = ""
        self.ai_line = ""

    @property
    def active(self) -> bool:
        return bool(self.app_name)

    # ---- per-tick heuristics (cheap, no allocs beyond the scan) ----------
    def tick(self, screen_text: str) -> None:
        if not self.active:
            return
        now = time.time()
        if screen_text != self._last_screen:
            self._last_screen = screen_text
            self._last_change = now
        idle_s = int(now - self._last_change)
        up_m = int((now - self.started) / 60)
        errs = len(_ERR.findall(screen_text))
        warns = len(_WARN.findall(screen_text))
        state = "idle" if idle_s > 30 else "active"
        bits = [f"● {self.app_name}", state, f"{up_m}′"]
        if errs:
            bits.append(f"{errs} err")
        if warns:
            bits.append(f"{warns} warn")
        self.status_line = " · ".join(bits)

    # ---- optional AI layer ----------------------------------------------
    def want_ai_pass(self) -> bool:
        """True when it's time to fire an LLM observation (caller runs it
        in an executor and reports back via set_ai_result)."""
        return (self.active and self.ai_enabled and not self._ai_busy
                and time.time() - self._last_ai >= AI_INTERVAL_S
                and bool(self._last_screen.strip()))

    def begin_ai_pass(self) -> str:
        """Mark the pass started; returns the prompt to send."""
        self._ai_busy = True
        self._last_ai = time.time()
        # trim: last 30 non-empty lines, 90 cols — enough context, few tokens
        lines = [ln[:90] for ln in self._last_screen.splitlines() if ln.strip()]
        return _AI_PROMPT + "\n".join(lines[-30:])

    def set_ai_result(self, text: str) -> None:
        self._ai_busy = False
        text = (text or "").strip().splitlines()[0] if text else ""
        if text and not text.lower().startswith(("error", "http", "[", "⚠")):
            self.ai_line = f"◉ {text[:70]}"

    def ai_failed(self) -> None:
        self._ai_busy = False
