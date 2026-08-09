"""Is this machine running the same aion as the rest of the fleet?

pansa sat a commit behind for a day and nothing said so. air was three weeks
behind, on a branch nobody remembered checking out, while answering `/status`
and accepting work the whole time. The only way to find out was to ssh into
each box and run `git rev-parse` by hand, which means nobody did.

The security-relevant line in this module is that peers are a DIAGNOSIS
source, never an update source. Code comes from origin. Treating a peer as
somewhere to fetch code from means one compromised node owns every other node
— a much worse failure than being a few commits behind.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aion.selfupdate import (  # noqa: E402
    Revision, UpdatePolicy, compare, describe, local_revision,
    policy_from_config, pull)


# ── comparing revisions ─────────────────────────────────────────────────────

def test_a_short_sha_matches_the_full_one_it_came_from():
    """`/status` carries a short sha; `ls-remote` returns a full one. A naive
    equality check calls every peer out of date forever."""
    assert Revision(sha="abc1234").same_as("abc1234def5678901234")


def test_two_unknown_revisions_are_not_agreement():
    """The kind of match that hides a problem: a machine that cannot report
    its revision agreeing with another that cannot either."""
    assert Revision().same_as("") is False
    assert Revision(sha="abc1234").same_as("") is False


def test_being_behind_origin_is_detected():
    drift = compare(Revision(sha="aaaaaaa"), upstream="bbbbbbbcccc")
    assert drift.behind_origin and not drift.in_sync


def test_matching_origin_is_in_sync():
    drift = compare(Revision(sha="aaaaaaa"), upstream="aaaaaaabbbb")
    assert not drift.behind_origin and drift.in_sync


def test_a_peer_on_another_revision_is_reported():
    drift = compare(Revision(sha="aaaaaaa"), upstream="aaaaaaa",
                    peers={"pansa": "aaaaaaa", "air": "zzzzzzz"})
    assert drift.differing == ["air"]
    assert not drift.in_sync


def test_a_peer_that_reported_nothing_is_unknown_not_differing():
    """"I could not tell" and "they disagree" need different reactions."""
    drift = compare(Revision(sha="aaaaaaa"), upstream="aaaaaaa",
                    peers={"pi": ""})
    assert drift.unknown == ["pi"] and drift.differing == []


def test_an_unknown_upstream_does_not_claim_you_are_behind():
    """No network is not evidence of being out of date."""
    drift = compare(Revision(sha="aaaaaaa"), upstream="")
    assert not drift.behind_origin


# ── what it says ────────────────────────────────────────────────────────────

def test_being_behind_says_what_to_do():
    """"behind origin" has an action; "differs from a peer" has an
    investigation. Conflating them teaches the operator to skim the line."""
    text = describe(compare(Revision(sha="aaaaaaa"), upstream="bbbbbbb"))
    assert "behind origin" in text and "git pull" in text


def test_a_differing_peer_is_not_described_as_ahead():
    """Without walking a shared history this cannot tell ahead from behind,
    and claiming to know would be worse than saying they differ."""
    text = describe(compare(Revision(sha="aaaaaaa"), upstream="aaaaaaa",
                            peers={"air": "zzzzzzz"}))
    assert "differs from air" in text
    assert "ahead" not in text and "behind" not in text


def test_a_dirty_tree_is_always_mentioned():
    """It explains a peer that matches on sha and behaves differently."""
    text = describe(compare(Revision(sha="aaaaaaa", dirty=True),
                            upstream="aaaaaaa"))
    assert "dirty" in text


def test_not_a_checkout_says_so_rather_than_guessing():
    assert "not a git checkout" in describe(compare(Revision()))


def test_up_to_date_says_which_revision():
    text = describe(compare(Revision(sha="aaaaaaabbb"), upstream="aaaaaaabbb"))
    assert "up to date" in text and "aaaaaaa" in text


# ── policy ──────────────────────────────────────────────────────────────────

def test_checking_is_off_by_default():
    assert UpdatePolicy().enabled is False
    assert policy_from_config({}).enabled is False
    assert policy_from_config(None).enabled is False


def test_auto_pull_cannot_be_on_without_checking():
    """Otherwise a config can produce a policy that pulls without looking."""
    p = policy_from_config({"updates": {"auto_pull": True}})
    assert p.auto_pull is False


def test_a_configured_policy_is_read():
    p = policy_from_config({"updates": {"check_every": 900, "auto_pull": True}})
    assert p.check_every == 900 and p.auto_pull and p.enabled


def test_unparseable_config_is_off():
    for bad in (True, 5, "yes", {"check_every": "soon"}):
        assert policy_from_config({"updates": bad}).enabled is False


def test_pull_refuses_when_no_switch_is_on():
    """The default. Nothing about checking should move a working tree."""
    moved, why = pull(ROOT, UpdatePolicy(check_every=60))
    assert not moved and "report-only" in why


# ── peers are never an update source ────────────────────────────────────────

def test_code_is_only_ever_pulled_from_the_configured_remote(monkeypatch):
    """The security posture, asserted behaviourally rather than by grepping
    prose. A peer is trusted with fleet membership, not with supplying the
    software the fleet runs: one compromised node must not become all of them.

    So whatever a peer reports, the argv git is handed names a remote — never
    a host, port or URL that came from a peer.
    """
    from aion import selfupdate

    calls = []

    def fake_git(root, *args):
        calls.append(args)
        return "aaaaaaa" if args[:1] == ("rev-parse",) else "Updating"

    monkeypatch.setattr(selfupdate, "_git", fake_git)
    selfupdate.pull(ROOT, UpdatePolicy(check_every=60, auto_pull=True))

    pulls = [a for a in calls if a and a[0] == "pull"]
    assert pulls, "nothing was pulled"
    for args in pulls:
        assert "--ff-only" in args, "a merge or rebase could rewrite local work"
        assert args[-2:] == ("origin", "main")
        joined = " ".join(args)
        for forbidden in ("://", "@", "127.0.0.1", ":8", "ssh"):
            assert forbidden not in joined, f"peer-shaped argument: {joined}"


def test_a_divergent_tree_is_refused_rather_than_merged(monkeypatch):
    """Fast-forward only: a merge would resolve someone's local work
    automatically on a machine nobody is watching, and a rebase would rewrite
    it. Refusing is correct — it is a person's problem."""
    from aion import selfupdate

    monkeypatch.setattr(selfupdate, "_git",
                        lambda root, *a: "aaaaaaa" if a[:1] == ("rev-parse",) else "")
    moved, why = selfupdate.pull(ROOT, UpdatePolicy(check_every=60,
                                                    auto_pull=True))
    assert not moved and "diverged" in why


def test_the_local_revision_reads_this_repo():
    """Not a mock: this file lives in a checkout, so it must report one."""
    rev = local_revision(ROOT)
    assert rev.sha and len(rev.short) == 7


# ── the wire ────────────────────────────────────────────────────────────────

def test_peers_report_their_revision_in_status():
    """Without this the drift is only visible by ssh-ing into each box."""
    from aion import fleet
    from aion.core import Bus
    from aion.store import Store

    payload = fleet.status_payload(Store(bus=Bus()))
    assert "revision" in payload
    assert payload["revision"]["sha"]


def test_the_revision_is_cached_rather_than_shelled_out_per_poll():
    """`/status` is polled on a timer by every peer watching this machine. A
    git subprocess per poll turns a health check into a load source."""
    from aion import fleet

    fleet._REVISION_CACHE = None
    first = fleet._self_revision()
    assert fleet._REVISION_CACHE is not None
    assert fleet._self_revision() is first


# ── reporting ───────────────────────────────────────────────────────────────

def test_the_report_is_stable_so_it_can_be_compared():
    """Both surfaces only speak when the line CHANGES. A cockpit that repeats
    "up to date" every quarter hour trains you to stop reading it, so the
    description has to be identical for an identical situation."""
    a = describe(compare(Revision(sha="aaaaaaa"), upstream="bbbbbbb"))
    b = describe(compare(Revision(sha="aaaaaaa"), upstream="bbbbbbb"))
    assert a == b


def test_the_cockpit_checks_on_its_own_interval_not_the_heartbeat():
    """`git ls-remote` is a network call. Riding the heartbeat would put it in
    front of every task update and status refresh in the cockpit."""
    src = (ROOT / "src" / "aion" / "ui" / "app.py").read_text(encoding="utf-8")
    assert "self.set_interval(self._updates.check_every, self._check_updates)" in src
    assert "_updates.enabled" in src


def test_the_cockpit_check_runs_off_the_event_loop():
    """A six-second freeze every fifteen minutes is worse than not checking."""
    src = (ROOT / "src" / "aion" / "ui" / "app.py").read_text(encoding="utf-8")
    block = src.split("def _check_updates")[1].split("\n    def ")[0]
    assert "asyncio.to_thread" in block
    assert "asyncio.create_task" in block


def test_the_node_reports_too():
    """The machine nobody looks at is where a stale checkout survives
    longest — air was three weeks behind while answering every request."""
    src = (ROOT / "scripts" / "aion_node.py").read_text(encoding="utf-8")
    assert "_update_watch" in src
    block = src.split("async def _update_watch")[1]
    assert "policy.enabled" in block          # off unless configured
    assert "if line != last" in block         # only on change


def test_neither_surface_pulls_on_drift_alone():
    """Reporting and applying are separate switches. Both surfaces gate the
    pull on `auto_pull` as well as on being behind — and `pull()` refuses
    independently anyway, so this is a second lock on the same door rather
    than the only one."""
    node = (ROOT / "scripts" / "aion_node.py").read_text(encoding="utf-8")
    assert "drift.behind_origin and policy.auto_pull" in node
    app = (ROOT / "src" / "aion" / "ui" / "app.py").read_text(encoding="utf-8")
    # The cockpit returns early unless a switch is set, then asks if `ask`.
    assert "if not (self._updates.auto_pull or self._updates.ask):" in app


def test_applying_an_update_does_not_leave_stale_code_running():
    """Swapping the source under a running process gets something that is
    neither version. The cockpit cannot restart itself mid-session so it says
    so; the node can, and does."""
    app = (ROOT / "src" / "aion" / "ui" / "app.py").read_text(encoding="utf-8")
    assert "restart to run the new code" in app
    node = (ROOT / "scripts" / "aion_node.py").read_text(encoding="utf-8")
    assert "restarting onto the new revision" in node


# ── asking before applying ──────────────────────────────────────────────────

def test_ask_is_a_third_setting_between_telling_and_doing():
    p = policy_from_config({"updates": {"check_every": 60, "ask": True}})
    assert p.ask and not p.auto_pull and p.enabled


def test_auto_pull_wins_over_ask():
    """Asking about something already applied without asking is a prompt that
    means nothing."""
    p = policy_from_config({"updates": {"check_every": 60, "ask": True,
                                        "auto_pull": True}})
    assert p.auto_pull and not p.ask


def test_ask_cannot_be_on_without_checking():
    assert policy_from_config({"updates": {"ask": True}}).ask is False


def test_pull_still_refuses_when_both_switches_are_off():
    moved, why = pull(ROOT, UpdatePolicy(check_every=60))
    assert not moved and "report-only" in why


def test_the_question_names_both_revisions_and_the_branch():
    """"update aion?" is not a question anybody can answer. An unexpected
    branch has to be visible before the yes, not after it."""
    from aion.selfupdate import gate_action

    q = gate_action(compare(Revision(sha="aaaaaaa", branch="main"),
                            upstream="bbbbbbbccc"))
    assert "aaaaaaa" in q and "bbbbbbb" in q and "main" in q


def test_a_dirty_tree_is_refused_before_anyone_is_asked():
    """An approval that then refuses itself is worse than no prompt: it
    teaches you the prompt is decorative."""
    from aion.selfupdate import can_apply

    ok, why = can_apply(compare(Revision(sha="aaaaaaa", dirty=True),
                                upstream="bbbbbbb"))
    assert not ok and "uncommitted" in why


def test_nothing_to_do_is_not_asked_about():
    from aion.selfupdate import can_apply

    ok, why = can_apply(compare(Revision(sha="aaaaaaa"), upstream="aaaaaaa"))
    assert not ok and "up to date" in why


def test_an_unreachable_origin_is_not_asked_about():
    from aion.selfupdate import can_apply

    assert can_apply(compare(Revision(sha="aaaaaaa"), upstream=""))[0] is False


def test_the_cockpit_asks_through_the_normal_gate():
    """Same mechanism as every other dangerous action: recorded with who
    approved it, and unanswered means no."""
    src = (ROOT / "src" / "aion" / "ui" / "app.py").read_text(encoding="utf-8")
    block = src.split("def _check_updates")[1].split("\n    def ")[0]
    assert "gates.request(" in block and "RISK_HIGH" in block
    assert "gates.wait(" in block
    assert "self._update_asked == drift.upstream" in block, \
        "a gate that reappears every interval is one people dismiss"


def test_a_node_says_it_cannot_ask_rather_than_doing_nothing():
    """`ask` on a machine with nobody at it would otherwise be a setting whose
    name promises a prompt and silently does nothing."""
    src = (ROOT / "scripts" / "aion_node.py").read_text(encoding="utf-8")
    assert "`ask` needs a cockpit" in src


def test_a_node_restarts_onto_what_it_pulled():
    """A node running code it no longer has on disk is the worst of both."""
    src = (ROOT / "scripts" / "aion_node.py").read_text(encoding="utf-8")
    assert "signal.raise_signal(signal.SIGTERM)" in src
    unit = (ROOT / "scripts" / "aion-node.service").read_text(encoding="utf-8")
    assert "Restart=always" in unit, \
        "on-failure will not restart the clean exit an update makes"
