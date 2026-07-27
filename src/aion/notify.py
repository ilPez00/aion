"""notify.py — push aion events outward (Slack, or any webhook).

What this is for
----------------
An autonomous fleet is only useful if it can get your attention. The events
worth interrupting a human for are narrow and specific:

    a task FAILED · a loop STALLED · an approval gate is WAITING

Everything else (progress, routine completions) belongs on the HUD, not in
your notifications. `INTERESTING` encodes that, and defaults to those three.

Opt-in by construction
----------------------
Nothing is ever sent unless a webhook URL is configured. There is no default
endpoint, no telemetry, and `enabled()` is false on a fresh install — so
merely importing or wiring this module cannot leak what your agents are
doing. A misconfigured notifier degrades to silence plus a log line; it never
raises into a harness loop.

Delivery is Slack's incoming-webhook shape (`{"text": ...}`), which is also
what Discord, Mattermost and most self-hosted receivers accept, so
`AION_WEBHOOK_URL` works for more than Slack despite the name of the file.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TIMEOUT = 6.0
# Events worth a notification. Anything else is HUD-only noise.
INTERESTING = ("failed", "stalled", "interrupted", "gate")
# Never send the same thing twice inside this window — a flapping harness
# must not turn into a hundred messages.
DEDUPE_S = 300.0


def webhook_url() -> str:
    """Configured endpoint, or empty. `AION_SLACK_WEBHOOK` is an alias."""
    return (os.environ.get("AION_WEBHOOK_URL")
            or os.environ.get("AION_SLACK_WEBHOOK")
            or "").strip()


def enabled() -> bool:
    return bool(webhook_url())


def _redact(url: str) -> str:
    """Webhook URLs are credentials — never log one in full."""
    if len(url) < 24:
        return "<webhook>"
    return f"{url[:22]}…{url[-4:]}"


@dataclass
class Notifier:
    """Rate-limited, deduplicating webhook sender.

    Stateful only in memory: a restart may repeat one message, which is a far
    better failure than a persistent dedupe cache that silently swallows the
    alert you actually needed.
    """
    url: str = field(default_factory=webhook_url)
    dedupe_s: float = DEDUPE_S
    min_interval_s: float = 1.0
    _last_sent: dict[str, float] = field(default_factory=dict, repr=False)
    _last_call: float = field(default=0.0, repr=False)
    sent: int = 0
    suppressed: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def should_send(self, kind: str, key: str, now: float | None = None) -> bool:
        """Is this event interesting, new, and not too soon after the last?"""
        if not self.enabled:
            return False
        if kind.lower() not in INTERESTING:
            return False
        now = time.time() if now is None else now
        last = self._last_sent.get(key)
        if last is not None and now - last < self.dedupe_s:
            return False
        return True

    def send(self, text: str, *, kind: str = "gate", key: str | None = None,
             now: float | None = None) -> bool:
        """Post `text`. Returns whether it actually went out.

        Never raises: this is called from harness loops and an unreachable
        Slack must not take a running agent down with it.
        """
        now = time.time() if now is None else now
        key = key or f"{kind}:{text}"
        if not self.should_send(kind, key, now):
            self.suppressed += 1
            return False
        # crude spacing so a burst cannot hammer the endpoint
        wait = self.min_interval_s - (now - self._last_call)
        if wait > 0:
            time.sleep(min(wait, self.min_interval_s))
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                ok = 200 <= r.status < 300
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"[notify] send failed via {_redact(self.url)}: "
                  f"{type(e).__name__}: {str(e)[:120]}")
            return False
        if ok:
            self._last_sent[key] = now
            self._last_call = now
            self.sent += 1
        return ok

    # ── event helpers ────────────────────────────────────────────────────
    def task_event(self, task: dict, now: float | None = None) -> bool:
        """Notify about a task, if its state warrants it."""
        state = str(task.get("state", "")).lower()
        label = task.get("label") or task.get("id", "?")
        inst = task.get("instance", "")
        where = f" on {inst}" if inst else ""
        icon = {"failed": ":x:", "stalled": ":warning:",
                "interrupted": ":pause_button:"}.get(state, ":bell:")
        return self.send(
            f"{icon} aion: task *{label}* is {state}{where}",
            kind=state, key=f"{inst}:{task.get('id')}:{state}", now=now)

    def gate_event(self, prompt: str, harness: str = "",
                   now: float | None = None) -> bool:
        """Notify that a human-in-the-loop approval is waiting.

        This is the one event that genuinely blocks the fleet: nothing
        proceeds until somebody answers, and HITL is fail-closed, so an
        unnoticed gate is indistinguishable from a denial.
        """
        who = f" [{harness}]" if harness else ""
        return self.send(
            f":lock: aion{who}: approval needed — {prompt[:200]}",
            kind="gate", key=f"gate:{harness}:{prompt[:80]}", now=now)


_default: Notifier | None = None


def default() -> Notifier:
    """Process-wide notifier, built from the environment on first use."""
    global _default
    if _default is None:
        _default = Notifier()
    return _default


def reset() -> None:
    """Drop the cached notifier — used by tests and after an env change."""
    global _default
    _default = None
