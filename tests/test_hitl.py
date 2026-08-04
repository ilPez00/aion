"""HITL gate tests — pure GateBook + interpret rules. No UI, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncio
import pytest

from aion.hitl import GateBook, Gate, RISK_HIGH
from aion.interpret import interpret


def test_request_creates_pending_gate():
    book = GateBook()
    g = book.request("t1", "run rm -rf build", risk=RISK_HIGH)
    assert isinstance(g, Gate) and not g.resolved
    assert book.has_pending() and book.pending() == [g]


def test_safe_policy_auto_approves_without_a_human():
    book = GateBook(is_safe=lambda action: action.startswith("read "))
    g = book.request("t1", "read config.json")
    assert g.resolved and g.approved and g.auto
    assert not book.has_pending()


def test_unsafe_action_stays_pending():
    book = GateBook(is_safe=lambda action: False)
    g = book.request("t1", "rm -rf /")
    assert not g.resolved and book.has_pending()


def test_resolve_latest_targets_newest_pending():
    book = GateBook()
    g1 = book.request("t1", "a")
    g2 = book.request("t2", "b")
    resolved = book.resolve_latest(approved=True)
    assert resolved is g2 and g2.approved
    assert book.pending() == [g1]


def test_resolve_latest_none_when_empty():
    assert GateBook().resolve_latest(True) is None


@pytest.mark.asyncio
async def test_wait_returns_when_approved():
    book = GateBook()
    g = book.request("t1", "danger")

    async def approver():
        await asyncio.sleep(0.01)
        book.resolve(g.id, approved=True)

    asyncio.create_task(approver())
    assert await book.wait(g) is True


@pytest.mark.asyncio
async def test_wait_fail_closed_on_timeout():
    # unanswered gate must DENY, never approve
    book = GateBook()
    g = book.request("t1", "danger")
    assert await book.wait(g, timeout=0.02) is False
    assert g.resolved and not g.approved


@pytest.mark.asyncio
async def test_auto_approved_gate_returns_immediately():
    book = GateBook(is_safe=lambda a: True)
    g = book.request("t1", "harmless")
    assert await book.wait(g, timeout=0.01) is True


def test_clear_resolved_prunes_book():
    book = GateBook()
    g = book.request("t1", "x")
    book.resolve(g.id, approved=False)
    book.clear_resolved()
    assert book.pending() == [] and not book._gates


def test_interpret_voice_approve_reject():
    assert interpret("approve") == "hitl approve"
    assert interpret("go ahead") == "hitl approve"
    assert interpret("reject") == "hitl reject"
    assert interpret("cancel that") == "hitl reject"
    assert interpret("what's the weather") != "hitl approve"


# ── the audit trail ─────────────────────────────────────────────────────────
# "Who approved what" used to be answerable for about as long as the process
# lived: the decision went into a task's in-memory log, `clear_resolved()`
# dropped the gate, and gates.json only ever holds what is still PENDING. For a
# fleet running privileged actions on other machines, that is the one record
# worth keeping.
from aion.hitl import AuditLog, read_all_recent  # noqa: E402


def _book():
    seen = []
    return GateBook(audit=seen.append), seen


def test_an_approval_is_recorded_with_who_made_it():
    book, seen = _book()
    g = book.request("t1", "run rm -rf build", risk=RISK_HIGH)
    book.resolve(g.id, True, by="cockpit")
    assert seen[0]["decision"] == "approved"
    assert seen[0]["by"] == "cockpit"
    assert seen[0]["gate"] == g.id and seen[0]["task"] == "t1"
    assert seen[0]["risk"] == RISK_HIGH


def test_a_rejection_is_recorded_too():
    book, seen = _book()
    g = book.request("t1", "drop the database")
    book.resolve(g.id, False, by="cockpit")
    assert seen[0]["decision"] == "rejected"


def test_a_remote_answer_is_distinguishable_from_a_local_one():
    """Different levels of evidence. A log that flattens them answers "was
    this approved" but never "by whom"."""
    book, seen = _book()
    g = book.request("t1", "deploy")
    book.resolve(g.id, True, by="remote")
    assert seen[0]["by"] == "remote"


def test_an_auto_approval_is_the_most_important_one_to_record():
    """Nobody was asked, so the log is the only evidence the action was ever
    allowed."""
    book, seen = _book()
    book.is_safe = lambda action: True
    book.request("t1", "ls")
    assert seen[0]["decision"] == "approved"
    assert seen[0]["by"] == "policy" and seen[0]["auto"] is True


def test_an_unanswered_gate_records_its_own_denial():
    book, seen = _book()
    g = book.request("t1", "rm -rf /")
    asyncio.run(book.wait(g, timeout=0.01))
    assert seen[0]["decision"] == "rejected" and seen[0]["by"] == "timeout"


def test_a_gate_is_recorded_before_the_task_is_released():
    """A crash between releasing the task and recording the decision leaves an
    action performed with nothing saying who allowed it. Recording first can at
    worst leave a decision on record that never took effect."""
    order = []
    book = GateBook(audit=lambda e: order.append("recorded"))
    g = book.request("t1", "deploy")

    async def scenario():
        async def waiter():
            await book.wait(g)
            order.append("released")
        t = asyncio.ensure_future(waiter())
        await asyncio.sleep(0)
        book.resolve(g.id, True)
        await t
    asyncio.run(scenario())
    assert order == ["recorded", "released"]


def test_resolving_twice_records_once():
    book, seen = _book()
    g = book.request("t1", "deploy")
    book.resolve(g.id, True)
    book.resolve(g.id, False)
    assert len(seen) == 1 and seen[0]["decision"] == "approved"


def test_a_broken_audit_sink_still_lets_the_gate_resolve():
    """A cockpit that deadlocks on a full disk is worse than one with a gap in
    its log."""
    def boom(_entry):
        raise OSError("disk full")
    book = GateBook(audit=boom)
    g = book.request("t1", "deploy")
    assert book.resolve(g.id, True) is not None
    assert g.approved is True


def test_a_book_with_no_sink_behaves_exactly_as_before():
    book = GateBook()
    g = book.request("t1", "deploy")
    assert book.resolve(g.id, True).approved is True


# ── the log on disk ─────────────────────────────────────────────────────────

def test_the_log_appends_rather_than_rewriting(tmp_path):
    """A log that gets rewritten is not evidence of anything."""
    log = AuditLog(tmp_path / "approvals.jsonl")
    log.record({"gate": "g1", "decision": "approved"})
    log.record({"gate": "g2", "decision": "rejected"})
    assert [e["gate"] for e in log.read()] == ["g1", "g2"]


def test_a_torn_line_costs_only_itself(tmp_path):
    p = tmp_path / "approvals.jsonl"
    log = AuditLog(p)
    log.record({"gate": "g1", "decision": "approved"})
    with p.open("a") as fh:
        fh.write('{"gate": "g2", "deci\n')          # a crash mid-write
    log.record({"gate": "g3", "decision": "rejected"})
    assert [e["gate"] for e in log.read()] == ["g1", "g3"]


def test_reading_a_log_that_does_not_exist_is_empty_not_an_error(tmp_path):
    assert AuditLog(tmp_path / "nope.jsonl").read() == []


def test_the_reader_returns_the_most_recent_entries(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(10):
        log.record({"gate": f"g{i}", "decision": "approved"})
    assert [e["gate"] for e in log.read(limit=3)] == ["g7", "g8", "g9"]


def test_the_fleet_view_merges_instances_by_time(tmp_path):
    root = tmp_path / "instances"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir(parents=True)
    AuditLog(root / "alpha" / "approvals.jsonl").record(
        {"gate": "g1", "ts": 100.0, "decision": "approved"})
    AuditLog(root / "beta" / "approvals.jsonl").record(
        {"gate": "g2", "ts": 50.0, "decision": "rejected"})
    out = read_all_recent(root)
    assert [(e["gate"], e["instance"]) for e in out] == [("g2", "beta"), ("g1", "alpha")]


def test_the_fleet_view_is_empty_without_a_fleet(tmp_path):
    assert read_all_recent(tmp_path / "nothing") == []
