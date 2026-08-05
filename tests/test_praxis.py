"""The todo list, mirrored into praxis.

Two lists of the same intentions that did not know about each other. Praxis
has no todo model, so this is a mapping, not a sync of like for like: an added
todo becomes a goal node with no progress, and a checked one sets that node to
1.0 AND logs an action record — praxis models "what I mean to do" and "what I
did" separately, and a completion is both.

Two endpoints means two failure modes, and the rule these tests exist to hold
is that neither may reach the cockpit. A todo list that refuses a line because
a remote service is down is worse than one briefly out of step. The local file
is the source of truth; praxis is a mirror.

No network here. `PraxisClient` takes a transport, so the mapping, the
validation and every refusal are testable with no praxis running — which is
just as well, since the configured deployment currently 404s.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.praxis import (  # noqa: E402
    DOMAINS, PraxisClient, PraxisConfig, action_payload, config_from,
    goal_payload, split_domain)


def cfg(**kw) -> PraxisConfig:
    base = dict(url="http://x/api", key="k", user_id="u1")
    base.update(kw)
    return PraxisConfig(**base)


class Fake:
    """Records calls; answers with whatever it was told to."""

    def __init__(self, *replies):
        self.calls = []
        self.replies = list(replies) or [(200, {"id": "n1"})]

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


# ── configuration ───────────────────────────────────────────────────────────

def test_praxis_is_off_until_it_is_configured():
    """A cockpit that has never heard of praxis must behave exactly as it
    did."""
    assert PraxisConfig().enabled is False
    assert config_from({}, env={}).enabled is False


def test_a_url_without_a_user_is_still_off():
    assert PraxisConfig(url="http://x", key="k").enabled is False


def test_the_environment_wins_over_the_config_file():
    """A URL exported for one session should not need a config edit that
    outlives the session."""
    c = config_from({"praxis": {"url": "http://old"}},
                    env={"AION_PRAXIS_URL": "http://new/", "AION_PRAXIS_KEY": "k",
                         "AION_PRAXIS_USER": "u"})
    assert c.url == "http://new" and c.enabled


def test_a_misspelt_domain_falls_back_instead_of_400ing_every_todo():
    c = config_from({"praxis": {"default_domain": "lerning"}}, env={})
    assert c.default_domain == "Personal"


def test_a_domain_is_matched_case_insensitively():
    assert config_from({"praxis": {"default_domain": "career"}},
                       env={}).default_domain == "Career"


# ── the mapping ─────────────────────────────────────────────────────────────

def test_a_trailing_tag_sets_the_domain_and_leaves_the_name_clean():
    assert split_domain("read the paper #Learning", "Personal") == (
        "read the paper", "Learning")


def test_an_unrecognised_tag_stays_in_the_text():
    """"#urgent" is a note the user wrote, not a failed domain. Eating it
    loses information to no purpose."""
    assert split_domain("ship it #urgent", "Personal") == (
        "ship it #urgent", "Personal")


def test_a_todo_becomes_a_goal_with_a_domain_praxis_accepts():
    body, why = goal_payload("fix the ring decoder", cfg())
    assert not why
    assert body["name"] == "fix the ring decoder"
    assert body["domain"] in DOMAINS


def test_a_too_short_todo_is_refused_here_rather_than_by_the_server():
    """Praxis rejects names under three characters. Finding that out as a 400
    per todo is worse than declining once, locally, with the reason."""
    body, why = goal_payload("go", cfg())
    assert body == {} and "3+ characters" in why


def test_a_completion_logs_an_honest_duration():
    """Praxis wants a number and aion does not time a checkbox. Zero is true;
    a plausible-looking duration would be fiction in a record reviewed as
    fact."""
    body, why = action_payload("fix the ring decoder", cfg())
    assert not why and body["duration_min"] == 0
    assert body["action_text"] == "fix the ring decoder"


# ── adding ──────────────────────────────────────────────────────────────────

def test_adding_a_todo_creates_a_goal_node():
    c = PraxisClient(cfg(), transport=Fake((200, {"id": "n7"})))
    assert c.add_todo("fix the ring decoder")
    method, path, body = c._transport.calls[0]
    assert method == "POST" and path == "/goals/u1/node"
    assert body["name"] == "fix the ring decoder"


def test_the_node_id_is_remembered_so_completion_can_find_it():
    c = PraxisClient(cfg(), transport=Fake((200, {"id": "n7"})))
    c.add_todo("fix the ring decoder")
    assert c.links["fix the ring decoder"] == "n7"


def test_a_disabled_praxis_makes_no_call_at_all():
    fake = Fake()
    c = PraxisClient(PraxisConfig(), transport=fake)
    assert not c.add_todo("anything")
    assert fake.calls == []


# ── completing ──────────────────────────────────────────────────────────────

def test_completing_sets_progress_and_logs_the_action():
    """Both, because praxis models the intention and the event separately and
    a checked todo is both of them."""
    c = PraxisClient(cfg(), transport=Fake((200, {"id": "n7"}), (200, {}), (200, {})))
    c.add_todo("fix the ring decoder")
    assert c.complete_todo("fix the ring decoder")
    paths = [p for _, p, _ in c._transport.calls]
    assert paths[1] == "/goals/u1/node/n7/progress"
    assert paths[2] == "/actions"
    assert c._transport.calls[1][2] == {"progress": 1.0}


def test_the_action_is_logged_even_when_the_goal_update_fails():
    """Two statements about one event. Losing the record of what was done
    because a goal id went stale drops the half praxis cannot reconstruct."""
    # First call is the progress PATCH (404), second is the action POST (ok).
    c = PraxisClient(cfg(), transport=Fake((404, "gone"), (200, {})),
                     links={"fix it": "n7"})
    result = c.complete_todo("fix it")
    assert not result.ok and "goal" in result.reason
    assert "/actions" in [p for _, p, _ in c._transport.calls]


def test_an_unknown_todo_still_logs_what_was_done():
    """A todo added before praxis was configured has no node. That is a
    reason, not a lost completion."""
    c = PraxisClient(cfg(), transport=Fake((200, {})))
    result = c.complete_todo("something added long ago")
    assert not result.ok and "no praxis goal" in result.reason
    assert c._transport.calls[0][1] == "/actions"


# ── failure never escapes ───────────────────────────────────────────────────

def test_a_transport_that_raises_becomes_a_reason():
    def boom(*a):
        raise OSError("connection refused")
    c = PraxisClient(cfg(), transport=boom)
    result = c.add_todo("fix the ring decoder")
    assert not result.ok and "connection refused" in result.reason


def test_an_http_error_becomes_a_reason():
    c = PraxisClient(cfg(), transport=Fake((500, "boom")))
    assert not c.add_todo("fix the ring decoder")


def test_nothing_here_raises_whatever_the_service_does():
    """The property the cockpit depends on: the local list is the source of
    truth and a mirror failure is a log line, never an exception."""
    for reply in ((500, "x"), (404, ""), (200, "not a dict"), (0, None)):
        c = PraxisClient(cfg(), transport=Fake(reply))
        c.add_todo("fix the ring decoder")
        c.complete_todo("fix the ring decoder")


# ── through the store ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_adding_a_todo_does_not_touch_praxis_when_unconfigured(tmp_path,
                                                                    monkeypatch):
    """The default path. No config, no calls, no log noise."""
    from aion.core import Bus, load_config
    from aion.store import Store
    from aion.todos import TodoStore

    monkeypatch.setenv("AION_HOME", str(tmp_path))
    for var in ("AION_PRAXIS_URL", "AION_PRAXIS_KEY", "AION_PRAXIS_USER"):
        monkeypatch.delenv(var, raising=False)
    s = Store(cfg=load_config(), bus=Bus())
    s.todos = TodoStore(tmp_path / "todos.md")
    await s._run_command("todo fix the ring decoder", _interpreted=True)
    assert [i["text"] for i in s.todos.items()] == ["fix the ring decoder"]
    assert not any("praxis" in line for line in s.state.logs)


@pytest.mark.asyncio
async def test_a_praxis_failure_never_costs_the_local_todo(tmp_path, monkeypatch):
    """The rule this whole module is built around."""
    from aion.core import Bus, load_config
    from aion.store import Store
    from aion.todos import TodoStore

    monkeypatch.setenv("AION_HOME", str(tmp_path))
    s = Store(cfg=load_config(), bus=Bus())
    s.todos = TodoStore(tmp_path / "todos.md")
    s._praxis = PraxisClient(cfg(), transport=Fake((500, "praxis is down")))
    s._praxis_links_path = tmp_path / "links.json"
    await s._run_command("todo fix the ring decoder", _interpreted=True)
    assert [i["text"] for i in s.todos.items()] == ["fix the ring decoder"]
    assert any("praxis" in line for line in s.state.logs)


@pytest.mark.asyncio
async def test_checking_off_mirrors_the_right_todo(tmp_path, monkeypatch):
    """`items()` sinks completed todos, so reading the text after checking it
    off names a different one."""
    from aion.core import Bus, load_config
    from aion.store import Store
    from aion.todos import TodoStore

    monkeypatch.setenv("AION_HOME", str(tmp_path))
    s = Store(cfg=load_config(), bus=Bus())
    s.todos = TodoStore(tmp_path / "todos.md")
    s.todos.add("first thing")
    s.todos.add("second thing")
    s._praxis = PraxisClient(cfg(), transport=Fake((200, {})),
                             links={"first thing": "n1"})
    s._praxis_links_path = tmp_path / "links.json"
    await s._run_command("todo done 1", _interpreted=True)
    assert "/goals/u1/node/n1/progress" in [
        p for _, p, _ in s._praxis._transport.calls]
