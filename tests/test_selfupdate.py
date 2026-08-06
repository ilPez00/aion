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


def test_pull_refuses_when_auto_pull_is_off():
    """The default. Nothing about checking should move a working tree."""
    moved, why = pull(ROOT, UpdatePolicy(check_every=60))
    assert not moved and "auto_pull is off" in why


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
