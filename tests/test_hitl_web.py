"""Approval gates across the process boundary.

`GateBook.wait()` is fail-closed, so a gate nobody sees is a gate that gets
denied. These tests cover the two halves of fixing that: publishing pending
gates so another process can SHOW them, and answering over the authenticated
transport — while proving the published file is not itself a way in.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion.hitl import GateBook, GateStore, read_all_pending  # noqa: E402


@pytest.fixture()
def book_and_store(tmp_path):
    return GateBook(), GateStore(tmp_path / "gates.json")


# ── publishing ───────────────────────────────────────────────────────────
def test_a_pending_gate_is_published(book_and_store):
    book, store = book_and_store
    book.request("t1", "run: rm -rf build/", risk="high")
    store.publish(book)
    got = store.read()
    assert len(got) == 1
    assert got[0]["action"] == "run: rm -rf build/" and got[0]["risk"] == "high"


def test_resolved_gates_disappear_from_the_published_set(book_and_store):
    book, store = book_and_store
    g = book.request("t1", "delete everything")
    store.publish(book)
    book.resolve(g.id, approved=True)
    store.publish(book)
    assert store.read() == []


def test_auto_approved_gates_are_never_published(book_and_store):
    """Policy-approved actions never blocked, so they were never waiting."""
    _, store = book_and_store
    book = GateBook(is_safe=lambda action: True)
    book.request("t1", "harmless thing")
    store.publish(book)
    assert store.read() == []


def test_reading_a_missing_file_is_empty(tmp_path):
    assert GateStore(tmp_path / "nope.json").read() == []


def test_reading_a_corrupt_file_is_empty_not_a_crash(tmp_path):
    p = tmp_path / "gates.json"
    p.write_text("{{{ not json")
    assert GateStore(p).read() == []


def test_garbage_entries_are_filtered(tmp_path):
    p = tmp_path / "gates.json"
    p.write_text(json.dumps([{"no_id": 1}, {"id": "g1", "action": "x"}]))
    assert [g["id"] for g in GateStore(p).read()] == ["g1"]


def test_read_all_pending_spans_instances(tmp_path, monkeypatch):
    root = tmp_path / "instances"
    for name in ("main", "worker"):
        d = root / name
        d.mkdir(parents=True)
        b = GateBook()
        b.request("t1", f"action on {name}")
        GateStore(d / "gates.json").publish(b)
    got = read_all_pending(root)
    assert {g["instance"] for g in got} == {"main", "worker"}


def test_read_all_pending_of_a_missing_root_is_empty(tmp_path):
    assert read_all_pending(tmp_path / "nothing") == []


# ── the file is display state, never a control channel ───────────────────
def test_writing_approved_into_the_file_approves_nothing(book_and_store):
    """The central safety property.

    gates.json sits in the user's home with ordinary permissions. If it were
    read back into the book, anything that could write a file could approve a
    destructive action. Nothing reads it back — approval only travels over the
    authenticated transport into the live GateBook.
    """
    book, store = book_and_store
    gate = book.request("t1", "run: rm -rf /")
    store.publish(book)

    # forge an approval on disk
    forged = store.read()
    forged[0]["resolved"] = True
    forged[0]["approved"] = True
    store.path.write_text(json.dumps(forged))

    assert gate.resolved is False and gate.approved is False
    assert book.has_pending() is True


@pytest.mark.asyncio
async def test_a_forged_file_does_not_release_a_waiting_harness(book_and_store):
    book, store = book_and_store
    gate = book.request("t1", "run: rm -rf /")
    store.publish(book)
    store.path.write_text(json.dumps([{**gate.as_dict(),
                                       "resolved": True, "approved": True}]))
    # still blocked; the timeout is what resolves it, and it resolves to DENY
    approved = await book.wait(gate, timeout=0.05)
    assert approved is False


@pytest.mark.asyncio
async def test_an_unanswered_gate_is_denied(book_and_store):
    book, _ = book_and_store
    gate = book.request("t1", "something privileged")
    assert await book.wait(gate, timeout=0.05) is False


# ── the transport is the only way in ─────────────────────────────────────
@pytest.mark.asyncio
async def test_remote_gate_endpoint_resolves_through_the_book():
    from aion.remotes import RemoteClient, RemoteNode, RemoteServer

    book = GateBook()
    gate = book.request("t1", "run: rm -rf build/")

    srv = RemoteServer(host="127.0.0.1", port=0, token="")
    srv.on_gate = lambda gid, approved: (
        {"ok": bool(book.resolve(gid, approved))})
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        node = RemoteNode(id="x", host="127.0.0.1", port=port)
        res = await RemoteClient(token="").resolve_gate(node, gate.id, True)
        assert res == {"ok": True}
        assert gate.resolved is True and gate.approved is True
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_a_non_boolean_approval_is_a_rejection():
    """`approved` must be the literal true — this releases a privileged action."""
    from aion.remotes import RemoteNode, RemoteServer

    seen = {}
    srv = RemoteServer(host="127.0.0.1", port=0, token="")
    srv.on_gate = lambda gid, approved: seen.update(approved=approved) or {"ok": True}
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"gate_id": "g1", "approved": "yes"})
        writer.write((f"POST /gate HTTP/1.1\r\nHost: x\r\n"
                      f"Content-Length: {len(body)}\r\n"
                      f"Connection: close\r\n\r\n{body}").encode())
        await writer.drain()
        await reader.read(4096)
        writer.close()
    finally:
        await srv.stop()
    assert seen["approved"] is False


@pytest.mark.asyncio
async def test_an_unauthenticated_caller_cannot_answer_a_gate():
    from aion.remotes import RemoteClient, RemoteNode, RemoteServer

    book = GateBook()
    gate = book.request("t1", "privileged")
    srv = RemoteServer(host="127.0.0.1", port=0, token="the-real-secret")
    srv.on_gate = lambda gid, approved: {"ok": bool(book.resolve(gid, approved))}
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        node = RemoteNode(id="x", host="127.0.0.1", port=port)
        res = await RemoteClient(token="wrong-secret").resolve_gate(node, gate.id, True)
        assert res is None                     # 401, surfaced as no result
        assert gate.resolved is False
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_with_no_handler_wired_nothing_is_approved():
    """A RemoteServer that was never given an on_gate must not default open."""
    from aion.remotes import RemoteClient, RemoteNode, RemoteServer

    srv = RemoteServer(host="127.0.0.1", port=0, token="")
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    try:
        node = RemoteNode(id="x", host="127.0.0.1", port=port)
        res = await RemoteClient(token="").resolve_gate(node, "g1", True)
        assert res and res.get("error")
    finally:
        await srv.stop()
