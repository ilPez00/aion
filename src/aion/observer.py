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

# ---- agent-event detection (ported from tuicommander output_parser.rs, MIT) --
# Provider rate-limit signatures. A hit means the agent is throttled and will
# retry; the HUD flags it so the user knows the pause is external, not a hang.
_RATE = re.compile(
    r"(?i)(rate_limit_error|overloaded_error|temporarily limiting requests|"
    r"User Provided API Key Rate Limit Exceeded|RESOURCE_EXHAUSTED|"
    r"\b429\b.{0,20}Too Many Requests|HTTP/\d[\d.]*\s+429|HTTP\s+429|"
    r"tokens per minute.*limit|TPM limit|requests per minute.*limit|RPM limit)")
# Retry-After seconds, either header form or the OpenAI prose form.
_RETRY_AFTER = re.compile(
    r"(?i)Retry-After:\s*(\d+)|Retry after (\d+) seconds?")
# Claude Code weekly/session usage meter.
_USAGE = re.compile(
    r"(?i)You['’]ve used (\d{1,3})% of your (weekly|session) limit")
# Usage exhausted + optional reset time ("… · resets 8pm (Europe/Madrid)").
_EXHAUST = re.compile(r"(?i)out of (?:extra )?usage")
_RESETS = re.compile(r"(?:·|·)\s*resets\s+(.+?)\s*$", re.MULTILINE)
# Waiting-for-input signatures: Y/N, confirm verbs, numbered menus, cliclack.
_YN = re.compile(r"(?i)\(y/n\)|\[y/n\]|\(yes/no\)|\[y/N\]|\[Y/n\]")
_CONFIRM = re.compile(
    r"(?im)^\s*(?:do you want|proceed with|continue\?|should i|confirm|"
    r"apply this|allow this|overwrite|are you sure)\b")
_MENU = re.compile(r"(?im)^\s*(?:[❯›>]\s*)?(\d+)[.)]\s+\S")
_SELECT = re.compile(r"(?i)Enter to select|use arrow keys|\[Use arrows")
_CLICLACK = re.compile(r"(?im)^\s*◆\s+(.+\?)\s*$")

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
        # agent-event detection state (rate-limit / usage / waiting-for-input)
        self.alert_line: str = ""       # rendered alert (rate-limit / usage)
        self.alert_kind: str = ""       # "rate" | "usage" | "exhausted" | ""
        self.attention: bool = False    # agent is waiting for user input
        self.attention_line: str = ""   # the question / menu prompt text
        self.rate_reset_at: float = 0.0  # epoch when a captured retry-after ends
        self.usage_pct: int = 0         # last seen usage-limit percentage

    # ---- lifecycle -------------------------------------------------------
    def attach(self, app_name: str) -> None:
        self.app_name = app_name
        self.started = time.time()
        self.status_line = f"● {app_name} starting"
        self.ai_line = ""
        self._last_screen = ""
        self._clear_alerts()

    def detach(self) -> None:
        self.app_name = ""
        self.status_line = ""
        self.ai_line = ""
        self._clear_alerts()

    def _clear_alerts(self) -> None:
        self.alert_line = ""
        self.alert_kind = ""
        self.attention = False
        self.attention_line = ""
        self.rate_reset_at = 0.0
        self.usage_pct = 0

    @property
    def rate_countdown(self) -> int:
        """Seconds left on a captured retry-after, else 0."""
        if not self.rate_reset_at:
            return 0
        return max(0, int(self.rate_reset_at - time.time()))

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
        self._detect_events(screen_text)

    # ---- agent-event detection ------------------------------------------
    def _detect_events(self, screen: str) -> None:
        """Scrape the screen for rate-limit / usage / waiting-for-input signals.

        Only the last ~20 non-empty lines matter — these prompts sit at the
        bottom of the screen. Cheap: a handful of pre-compiled regex scans.
        """
        tail = "\n".join(
            ln for ln in screen.splitlines() if ln.strip())[-4000:]

        # rate limit — capture retry-after when the provider gives one
        if _RATE.search(tail):
            self.alert_kind = "rate"
            m = _RETRY_AFTER.search(tail)
            if m and not self.rate_countdown:
                secs = int(m.group(1) or m.group(2) or 0)
                if secs:
                    self.rate_reset_at = time.time() + secs
            cd = self.rate_countdown
            self.alert_line = (f"⏳ rate-limited · retry {cd}s"
                               if cd else "⏳ rate-limited · retrying")
        else:
            # clear only once the countdown has elapsed
            if self.alert_kind == "rate" and not self.rate_countdown:
                self.alert_kind = ""
                self.alert_line = ""
                self.rate_reset_at = 0.0

        # usage meter — Claude Code weekly/session %
        um = _USAGE.search(tail)
        if um:
            self.usage_pct = int(um.group(1))
            scope = um.group(2)
            self.alert_kind = "usage"
            self.alert_line = f"▓ {self.usage_pct}% of {scope} limit used"
        ex = _EXHAUST.search(tail)
        if ex:
            self.alert_kind = "exhausted"
            rm = _RESETS.search(tail)
            self.alert_line = ("⛔ usage exhausted"
                               + (f" · resets {rm.group(1)[:24]}" if rm else ""))

        # waiting for user input — Y/N, confirm verbs, menus, cliclack
        q = ""
        cm = _CLICLACK.search(tail)
        yn = _YN.search(tail)
        cf = _CONFIRM.search(tail)
        if cm:
            q = cm.group(1)
        elif yn or cf:
            # grab the line the match sits on
            src = yn or cf
            line_start = tail.rfind("\n", 0, src.start()) + 1
            line_end = tail.find("\n", src.start())
            q = tail[line_start:line_end if line_end >= 0 else None].strip()
        elif _MENU.search(tail) and _SELECT.search(tail):
            q = "menu · awaiting selection"
        if q:
            self.attention = True
            self.attention_line = f"⚠ input needed: {q[:60]}"
        else:
            self.attention = False
            self.attention_line = ""

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
