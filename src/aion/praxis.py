"""praxis.py — the todo list, mirrored into praxis.

`~/.aion/todos.md` and praxis are two lists of the same intentions that do not
know about each other. Praxis has no todo model, so this is a mapping rather
than a sync of like for like:

    todo added       -> a goal node, progress 0, under an "aion" parent
    todo checked     -> that node's progress set to 1.0, AND an action record
                        logged, because praxis models "what I mean to do" and
                        "what I did" as different things and a completion is
                        both

Two endpoints means two failure modes, and neither may reach the cockpit. A
todo list that refuses to accept a line because a remote service is down is
worse than one that is briefly out of step: the local file stays the source of
truth, every call soft-fails with a reason, and the mirror catches up later.

Pure by construction. Nothing here opens a socket — `PraxisClient` takes a
`transport(method, path, body) -> (status, data)` callable, so the mapping,
the validation and the refusals are all testable with no network, no praxis
and no clock. The real transport is a dozen lines of `urllib` at the bottom.

OFF unless configured. No URL or no key means `enabled` is False and every
call returns a refusal without attempting anything, so a cockpit that has
never heard of praxis behaves exactly as it did.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

__all__ = ["PraxisConfig", "Result", "PraxisClient", "config_from",
           "DOMAINS", "MODES", "split_domain", "goal_payload", "action_payload"]

# Praxis validates these server-side; sending anything else is a 400 the user
# only finds out about from a log line, so they are checked here first.
DOMAINS = ("Fitness", "Career", "Learning", "Relationships", "Finance",
           "Creative", "Health", "Spiritual", "Business", "Personal")
ACTION_DOMAINS = ("FABRICATE", "STUDY", "CONSTRUCT", "BOND", "HEAL")
MODES = ("LIFT", "WALK", "WORK", "LEARN", "CODE", "CREATE", "REST")

# A goal name shorter than this is rejected by praxis. "buy milk" passes;
# "go" does not, and finding that out as a 400 is worse than declining here.
MIN_NAME = 3
MAX_NAME = 200

_TAG = re.compile(r"\s#([A-Za-z]+)\s*$")


@dataclass(frozen=True)
class PraxisConfig:
    """Where praxis is and who we are to it."""

    url: str = ""
    key: str = ""
    user_id: str = ""
    # Praxis requires a domain per goal and "buy milk" does not imply one, so
    # a default is unavoidable. `Personal` rather than a guess: a wrong
    # confident domain is harder to notice than a boring one.
    default_domain: str = "Personal"
    # Where aion's todos hang in the goal tree. One parent, so praxis users
    # can see at a glance which goals came from a machine.
    parent: str = "aion"
    # What a completed todo is logged as. Not inferable from the text either.
    action_domain: str = "FABRICATE"
    action_mode: str = "WORK"

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key and self.user_id)


@dataclass
class Result:
    """What happened, in a form the caller never has to catch."""

    ok: bool
    reason: str = ""
    data: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def config_from(cfg: dict | None, env: dict | None = None) -> PraxisConfig:
    """Read config then environment. Anything unparseable is OFF.

    Environment wins: a URL exported for one session should not require
    editing a config file that outlives it.
    """
    import os

    env = os.environ if env is None else env
    raw = (cfg or {}).get("praxis")
    raw = raw if isinstance(raw, dict) else {}
    try:
        return PraxisConfig(
            url=str(env.get("AION_PRAXIS_URL") or raw.get("url") or "").rstrip("/"),
            key=str(env.get("AION_PRAXIS_KEY") or raw.get("key") or ""),
            user_id=str(env.get("AION_PRAXIS_USER") or raw.get("user_id") or ""),
            default_domain=_domain(raw.get("default_domain"), DOMAINS, "Personal"),
            parent=str(raw.get("parent") or "aion"),
            action_domain=_domain(raw.get("action_domain"), ACTION_DOMAINS,
                                  "FABRICATE"),
            action_mode=_domain(raw.get("action_mode"), MODES, "WORK"))
    except (TypeError, ValueError):
        return PraxisConfig()


def _domain(value, allowed: tuple[str, ...], fallback: str) -> str:
    """Case-insensitive match against what praxis accepts, or the fallback.

    A misspelt domain in a config file would otherwise become a 400 per todo,
    discovered one log line at a time.
    """
    want = str(value or "").strip().lower()
    for name in allowed:
        if name.lower() == want:
            return name
    return fallback


def split_domain(text: str, default: str) -> tuple[str, str]:
    """`"read the paper #Learning"` -> `("read the paper", "Learning")`.

    A trailing tag is the only way a one-line todo can carry a domain, and it
    stays out of the goal name. An unrecognised tag is LEFT IN the text rather
    than silently eaten: "#urgent" is a note the user wrote, not a failed
    domain, and swallowing it loses information to no purpose.
    """
    m = _TAG.search(text or "")
    if not m:
        return (text or "").strip(), default
    matched = _domain(m.group(1), DOMAINS, "")
    if not matched:
        return (text or "").strip(), default
    return text[:m.start()].strip(), matched


def goal_payload(text: str, cfg: PraxisConfig) -> tuple[dict, str]:
    """The body for a new goal node, and why it cannot be sent (empty if fine)."""
    name, domain = split_domain(text, cfg.default_domain)
    if len(name) < MIN_NAME:
        return {}, f"praxis needs {MIN_NAME}+ characters, got {name!r}"
    return {"name": name[:MAX_NAME], "domain": domain,
            "description": f"from aion todo: {name[:MAX_NAME]}"}, ""


def action_payload(text: str, cfg: PraxisConfig,
                   duration_min: int = 0) -> tuple[dict, str]:
    """The body for the completion record that accompanies a checked todo."""
    name, _ = split_domain(text, cfg.default_domain)
    if not name:
        return {}, "praxis needs some text to log an action"
    return {"action_text": name[:MAX_NAME], "domain": cfg.action_domain,
            "mode": cfg.action_mode,
            # Praxis requires a number and aion does not time a checkbox.
            # Zero is honest; inventing a plausible duration would put
            # fiction into a record the user reviews as fact.
            "duration_min": max(0, int(duration_min or 0)),
            "scope": "PERSONAL", "trigger": "SELF"}, ""


class PraxisClient:
    """Mirror aion todos into praxis. Never raises at the caller."""

    def __init__(self, cfg: PraxisConfig, transport=None, links=None) -> None:
        self.cfg = cfg
        self._transport = transport or _http_transport(cfg)
        # todo text -> praxis node id, so checking one off can find the goal
        # it created. Injected so tests need no disk.
        self.links: dict[str, str] = {} if links is None else links

    # -- the two writes -------------------------------------------------
    def add_todo(self, text: str) -> Result:
        """A new todo becomes a goal node with no progress."""
        if not self.cfg.enabled:
            return Result(False, "praxis not configured")
        body, why = goal_payload(text, self.cfg)
        if why:
            return Result(False, why)
        ok, data = self._call("POST", f"/goals/{self.cfg.user_id}/node", body)
        if not ok:
            return Result(False, f"praxis: {data}")
        node_id = str(data.get("id") or data.get("node_id") or "")
        if node_id:
            self.links[_key(text)] = node_id
        return Result(True, "", {"node_id": node_id})

    def complete_todo(self, text: str, duration_min: int = 0) -> Result:
        """A checked todo is BOTH a finished goal and a thing that happened.

        The action record is logged even when the progress update fails, and
        vice versa. They are two statements about one event, and losing the
        record of what was done because a goal id went stale would drop the
        half that praxis cannot reconstruct.
        """
        if not self.cfg.enabled:
            return Result(False, "praxis not configured")
        problems = []
        node_id = self.links.get(_key(text))
        if node_id:
            ok, data = self._call(
                "PATCH", f"/goals/{self.cfg.user_id}/node/{node_id}/progress",
                {"progress": 1.0})
            if not ok:
                problems.append(f"goal: {data}")
        else:
            problems.append("no praxis goal known for this todo")

        body, why = action_payload(text, self.cfg, duration_min)
        if why:
            problems.append(why)
        else:
            ok, data = self._call("POST", "/actions", body)
            if not ok:
                problems.append(f"action: {data}")
        return Result(not problems, "; ".join(problems))

    # -- transport ------------------------------------------------------
    def _call(self, method: str, path: str, body: dict) -> tuple[bool, object]:
        """One request. Any failure is a value, never an exception.

        A todo list that refuses a line because a remote service is down is
        worse than one briefly out of step, so nothing here escapes.
        """
        try:
            status, data = self._transport(method, path, body)
        except Exception as e:                       # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        if not 200 <= int(status) < 300:
            return False, f"HTTP {status}: {str(data)[:120]}"
        return True, data if isinstance(data, dict) else {}


def _key(text: str) -> str:
    """Todos are matched by their text; praxis ids are not in the markdown."""
    name, _ = split_domain(text or "", "")
    return " ".join(name.lower().split())


def _http_transport(cfg: PraxisConfig):
    """The real one. Kept to the bottom so everything above stays pure."""
    def send(method: str, path: str, body: dict):
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"{cfg.url}{path}", method=method,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg.key}"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:200]
    return send
