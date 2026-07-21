"""research.py — the deepresearch loop.

`web.deepsearch_answer` answers in one shot: one search, one synthesis. That is
fine for a lookup and wrong for a question with several facets, where the first
search does not know what the second should have asked.

This module runs the loop instead:

    plan     decompose the question into a few focused queries
    gather   for each round: search one query, note what it found
    reflect  decide whether the findings cover the question, or name the gap
    report   synthesise everything, citing sources by [n]

The engine is pure and injectable: `search_fn` and `chat_fn` are passed in, so
the whole loop runs in tests against canned data with no network and no API
key. `ResearchHarness` wires the real web search + LLM to it.

Every LLM step degrades rather than fails: no key, no daemon, a timeout -- each
falls back to something usable (the raw query as its own plan, the snippets as
their own notes) so the loop always produces a report.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

# search_fn(query, n) -> [{"title","url","snippet"}]
SearchFn = Callable[[str, int], list[dict]]
# chat_fn(messages) -> str | None   (None = model unavailable, caller falls back)
ChatFn = Callable[[list[dict]], "str | None"]

# The reflect step ends a round with one of these on its own line.
DONE_SENTINEL = "RESEARCH_COMPLETE"

MAX_QUERIES = 5
SNIPPETS_PER_QUERY = 5


class StepReporter(Protocol):
    """Called once per loop step so the harness can drive progress/log/kill.

    Returns False to abort (kill/pause-then-kill); the loop stops cleanly and
    reports what it has so far rather than throwing.
    """
    def __call__(self, phase: str, detail: str, done: int, total: int) -> bool: ...


@dataclass
class Source:
    title: str
    url: str
    snippet: str = ""

    def key(self) -> str:
        # dedup on URL when present, else on the (normalised) title
        return self.url.strip().lower() or self.title.strip().lower()


@dataclass
class Finding:
    query: str
    note: str
    source_ids: list[int] = field(default_factory=list)


@dataclass
class ResearchReport:
    prompt: str
    queries: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    answer: str = ""
    rounds: int = 0
    stopped: str = ""        # "" | "budget" | "covered" | "aborted"

    def as_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "answer": self.answer,
            "rounds": self.rounds,
            "stopped": self.stopped,
            "queries": self.queries,
            "sources": [{"title": s.title, "url": s.url} for s in self.sources],
            "findings": [{"query": f.query, "note": f.note,
                          "sources": f.source_ids} for f in self.findings],
        }


# ── LLM steps (each has a no-LLM fallback) ────────────────────────────────────
def plan_queries(prompt: str, chat_fn: ChatFn, budget: int) -> list[str]:
    """Decompose the question into up to `budget` focused search queries."""
    budget = max(1, min(budget, MAX_QUERIES))
    out = chat_fn([
        {"role": "system", "content":
            "Break the user's question into distinct web-search queries, each "
            "targeting one facet. Output one query per line, no numbering, no "
            f"prose. At most {budget} lines."},
        {"role": "user", "content": prompt},
    ])
    queries = _clean_lines(out) if out else []
    # Fall back to the raw question, and always cover it directly first.
    if not queries:
        return [prompt]
    if prompt not in queries:
        queries = [prompt, *queries]
    return queries[:budget]


def note_findings(query: str, sources: list[Source], chat_fn: ChatFn) -> str:
    """Summarise what this round's sources say about its query."""
    if not sources:
        return "(no results)"
    corpus = "\n".join(f"[{i+1}] {s.title}: {s.snippet}"
                       for i, s in enumerate(sources))
    out = chat_fn([
        {"role": "system", "content":
            "Summarise, in 1-2 sentences, what these snippets establish about "
            "the query. State only what the snippets support."},
        {"role": "user", "content": f"Query: {query}\n\n{corpus}"},
    ])
    # Fallback: the top snippet is better than nothing.
    return out.strip() if out else (sources[0].snippet or sources[0].title)


def is_covered(prompt: str, findings: list[Finding], chat_fn: ChatFn) -> bool:
    """Ask whether the findings already answer the question."""
    if not findings:
        return False
    joined = "\n".join(f"- {f.query}: {f.note}" for f in findings)
    out = chat_fn([
        {"role": "system", "content":
            "Do these findings fully answer the question? Reply with exactly "
            f"'{DONE_SENTINEL}' if yes, or a single missing sub-question if no."},
        {"role": "user", "content": f"Question: {prompt}\n\nFindings:\n{joined}"},
    ])
    return bool(out) and DONE_SENTINEL in out


def synthesise(prompt: str, findings: list[Finding], sources: list[Source],
               chat_fn: ChatFn) -> str:
    """Write the final report, citing sources by [n]."""
    if not findings:
        return "No findings — the search returned nothing usable."
    notes = "\n".join(f"- {f.query}: {f.note} "
                      f"{''.join(f'[{i+1}]' for i in f.source_ids)}"
                      for f in findings)
    cites = "\n".join(f"[{i+1}] {s.title} — {s.url}"
                      for i, s in enumerate(sources))
    out = chat_fn([
        {"role": "system", "content":
            "Write a concise, well-structured answer from these findings. Cite "
            "sources inline as [n]. Do not invent facts beyond the findings."},
        {"role": "user", "content":
            f"Question: {prompt}\n\nFindings:\n{notes}\n\nSources:\n{cites}"},
    ])
    if out:
        return out.strip()
    # Fallback report: the notes themselves, plus the source list.
    body = "\n".join(f"- {f.note}" for f in findings)
    return f"{body}\n\nSources:\n{cites}"


# ── the loop ──────────────────────────────────────────────────────────────────
def run_research(prompt: str, search_fn: SearchFn, chat_fn: ChatFn,
                 max_rounds: int = 4,
                 report_step: StepReporter | None = None) -> ResearchReport:
    """Plan, search round by round until covered or out of budget, synthesise.

    max_rounds is the hard iteration cap (safe-run guard, lesson #4). The loop
    also stops early when `is_covered` says the findings answer the question,
    so a simple query does not burn the whole budget.
    """
    max_rounds = max(1, max_rounds)
    report = ResearchReport(prompt=prompt)
    step = report_step or (lambda *a, **k: True)

    # total steps for progress: plan + (search+reflect per round) + synthesis.
    total = 2 + max_rounds

    if not step("plan", "decomposing the question", 0, total):
        report.stopped = "aborted"
        return report
    report.queries = plan_queries(prompt, chat_fn, max_rounds)

    seen: dict[str, int] = {}   # source key -> index in report.sources
    for i, query in enumerate(report.queries[:max_rounds]):
        if not step("search", query, 1 + i, total):
            report.stopped = "aborted"
            return report
        report.rounds += 1

        raw = search_fn(query, SNIPPETS_PER_QUERY)
        round_sources = [Source(r.get("title", ""), r.get("url", ""),
                                r.get("snippet", "")) for r in raw]
        ids: list[int] = []
        for s in round_sources:
            if not (s.title or s.url):
                continue
            k = s.key()
            if k not in seen:
                seen[k] = len(report.sources)
                report.sources.append(s)
            ids.append(seen[k])

        note = note_findings(query, [report.sources[j] for j in ids], chat_fn)
        report.findings.append(Finding(query=query, note=note, source_ids=ids))

        # Reflect: stop early if the question is already answered.
        if not step("reflect", "assessing coverage", 1 + i, total):
            report.stopped = "aborted"
            return report
        if i + 1 < len(report.queries) and is_covered(prompt, report.findings, chat_fn):
            report.stopped = "covered"
            break

    if not report.stopped:
        report.stopped = "budget"

    if not step("report", "writing the answer", total - 1, total):
        report.stopped = "aborted"
        return report
    report.answer = synthesise(prompt, report.findings, report.sources, chat_fn)
    step("done", report.stopped, total, total)
    return report


# ── helpers ───────────────────────────────────────────────────────────────────
def _clean_lines(text: str) -> list[str]:
    """One query per non-empty line, stripped of numbering/bullets/quotes."""
    out: list[str] = []
    for line in text.splitlines():
        line = re.sub(r'^\s*(?:[-*•]|\d+[.)])\s*', "", line).strip().strip('"')
        if line and not line.lower().startswith(("here", "sure", "query:")):
            out.append(line)
    return out
