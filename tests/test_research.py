"""Tests for the deepresearch loop. All injectable — no network, no API key."""
from __future__ import annotations

import pytest

from aion import research
from aion.research import (
    DONE_SENTINEL, Finding, ResearchReport, Source, run_research,
)


# ── fakes ─────────────────────────────────────────────────────────────────────
def make_search(pages: dict[str, list[dict]] | None = None):
    """search_fn returning canned results; records the queries it saw."""
    pages = pages or {}
    calls: list[str] = []

    def search(query: str, n: int) -> list[dict]:
        calls.append(query)
        return pages.get(query, [
            {"title": f"Result for {query}", "url": f"http://x/{len(calls)}",
             "snippet": f"snippet about {query}"},
        ])[:n]

    search.calls = calls  # type: ignore[attr-defined]
    return search


def make_chat(scripted=None, covered_after=None):
    """chat_fn. `scripted` maps a system-prompt keyword -> canned reply.
    `covered_after`: return DONE_SENTINEL on the coverage check once this many
    findings exist. None returns from every call (model 'unavailable')."""
    calls: list[list[dict]] = []

    def chat(messages):
        calls.append(messages)
        sysmsg = messages[0]["content"].lower()
        if "one query per line" in sysmsg or "search queries" in sysmsg:
            return (scripted or {}).get("plan")
        if "fully answer" in sysmsg:
            if covered_after is not None:
                finding_lines = messages[-1]["content"].count("\n- ")
                return DONE_SENTINEL if finding_lines >= covered_after else "missing bit"
            return "still missing something"
        if "summarise" in sysmsg:
            return (scripted or {}).get("note")
        if "concise" in sysmsg or "cite" in sysmsg:
            return (scripted or {}).get("report")
        return None

    chat.calls = calls  # type: ignore[attr-defined]
    return chat


# ── plan ──────────────────────────────────────────────────────────────────────
def test_plan_falls_back_to_raw_prompt_without_llm():
    q = research.plan_queries("what is X", make_chat(), budget=4)
    assert q == ["what is X"]


def test_plan_parses_and_caps_query_lines():
    chat = make_chat({"plan": "1. angle one\n2. angle two\n- angle three"})
    q = research.plan_queries("big question", chat, budget=2)
    assert len(q) == 2
    assert "angle one" in q[0] or q[0] == "big question"


def test_plan_prepends_the_raw_question():
    chat = make_chat({"plan": "sub query a\nsub query b"})
    q = research.plan_queries("root", chat, budget=5)
    assert q[0] == "root"


# ── notes ─────────────────────────────────────────────────────────────────────
def test_note_findings_falls_back_to_top_snippet():
    src = [Source("T", "http://u", "the snippet")]
    assert research.note_findings("q", src, make_chat()) == "the snippet"


def test_note_findings_handles_no_sources():
    assert research.note_findings("q", [], make_chat()) == "(no results)"


# ── loop ──────────────────────────────────────────────────────────────────────
def test_loop_runs_to_budget_without_llm():
    search = make_search()
    report = run_research("q", search, make_chat(), max_rounds=3)
    assert report.rounds == 1          # no-LLM plan => single query => 1 round
    assert report.stopped == "budget"
    assert report.answer               # always produces a report
    assert report.sources


def test_loop_searches_each_planned_query():
    search = make_search()
    chat = make_chat({"plan": "alpha\nbeta\ngamma"})
    report = run_research("root", search, chat, max_rounds=4)
    # root + alpha + beta + gamma, capped at max_rounds
    assert "root" in search.calls
    assert "alpha" in search.calls
    assert report.rounds == len(report.queries)


def test_loop_stops_early_when_covered():
    search = make_search()
    chat = make_chat({"plan": "a\nb\nc\nd"}, covered_after=1)
    report = run_research("root", search, chat, max_rounds=4)
    assert report.stopped == "covered"
    assert report.rounds < len(report.queries)


def test_loop_respects_max_rounds_cap():
    search = make_search()
    chat = make_chat({"plan": "a\nb\nc\nd\ne"})
    report = run_research("root", search, chat, max_rounds=2)
    assert report.rounds == 2
    assert report.stopped == "budget"


def test_loop_dedups_sources_across_rounds():
    dup = [{"title": "same", "url": "http://dup", "snippet": "s"}]
    search = make_search({"a": dup, "b": dup})
    chat = make_chat({"plan": "a\nb"})
    report = run_research("root", search, chat, max_rounds=3)
    urls = [s.url for s in report.sources]
    assert urls.count("http://dup") == 1


def test_loop_aborts_when_reporter_returns_false():
    search = make_search()

    def stop_at_search(phase, detail, done, total):
        return phase != "search"      # kill on the first search step

    report = run_research("q", search, make_chat(), max_rounds=3,
                          report_step=stop_at_search)
    assert report.stopped == "aborted"
    assert report.answer == ""


def test_reporter_sees_monotonic_progress():
    search = make_search()
    seen = []

    def rec(phase, detail, done, total):
        seen.append((done, total))
        return True

    run_research("q", search, make_chat(), max_rounds=2, report_step=rec)
    dones = [d for d, _ in seen]
    assert dones == sorted(dones)
    assert seen[-1][0] == seen[-1][1]   # ends at 100%


def test_report_as_dict_is_serialisable():
    import json
    report = run_research("q", make_search(), make_chat(), max_rounds=1)
    json.dumps(report.as_dict())        # must not raise


def test_synthesise_fallback_lists_sources_when_no_llm():
    findings = [Finding("q", "a note", [0])]
    sources = [Source("Title", "http://u", "snip")]
    out = research.synthesise("q", findings, sources, make_chat())
    assert "a note" in out
    assert "http://u" in out
