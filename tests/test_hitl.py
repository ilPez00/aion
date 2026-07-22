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
