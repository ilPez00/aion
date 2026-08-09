"""compress.py — fit a step's output into the next step's prompt by dropping
words that carry nothing, rather than by cutting the end off.

`prompt_for` splices each dependency's stdout into the prompt of the step that
waited for it, and clips it with `text[:share]`. That clip is the problem
`swarmfacts` already described from the other side: the budget clips prose by
character count, and the one line that mattered is as likely to be past the cut
as anything else. A scout that spends four paragraphs reasoning and states its
conclusion last loses exactly the conclusion.

So this module removes words instead of removing the end. The comparison to
judge it by is not "compressed text vs. the full text" — that option was never
on the table at 2000 characters. It is "compressed vs. amputated".

Everything here is deterministic and local. No model, no network, no clock. A
compressor that calls an LLM to decide what matters is a second agent in the
handoff path with its own failure modes and its own bill, inserted at the one
point where the swarm is already struggling for context.

What it will not touch
----------------------
Deletion is only safe if the vocabulary is fixed and small. This drops words
from a closed list and applies a closed set of phrase rewrites; it never
summarises, never paraphrases, and never reorders. Anything outside the list
survives byte-for-byte.

Protected spans are exempt even from that: fenced and indented code, inline
`code`, URLs, filesystem paths, and `FACT key=value` lines. Not decoration —
`http://host/the/path` contains a whole-word `the` between two slashes, and a
compressor that does not know what a URL is will happily produce
`http://host//path`.

Hedges stay
-----------
The obvious next entry on the drop list is hedging — "might", "appears to",
"I think", "probably". It is not there, deliberately.

Between two agents, uncertainty is content. A scout reporting "the api base
might be https://x" compressed to "api base https://x" has not been shortened,
it has been upgraded from a guess to a finding, and the step downstream cannot
tell the difference. That is the exact failure `swarmfacts` exists to prevent —
a swarm producing a confidently wrong answer because a value got re-derived by
someone reading prose. Articles carry nothing. Doubt carries everything.

Negations stay, for the same reason and more bluntly: no rule here may remove
`not`, `never`, `no`, `none`, `without`, `except`, `unless`, `only`. Flipping a
sentence's meaning is worse than any number of characters saved, and
`preserved_tokens` asserts it rather than trusting the word lists to be right.
"""

from __future__ import annotations

import re

__all__ = ["compress", "savings", "preserved_tokens", "fit"]


# ── what must survive ────────────────────────────────────────────────────────
# Checked by `preserved_tokens`, which is the safety net under the word lists:
# if some future entry ever shadows one of these, the property test fails
# rather than the swarm quietly inverting a finding.
NEGATIONS = frozenset("""
    not never no none nor neither without except unless only cannot
    don't doesn't didn't won't wouldn't can't couldn't shouldn't isn't aren't
    wasn't weren't hasn't haven't hadn't
""".split())

# Spans copied through untouched. Order matters: fenced code before inline
# code, or a fence's backticks pair up with each other.
#
# DOTALL is scoped to the two fence alternatives with `(?s:...)` rather than
# set on the whole pattern, and that is load-bearing. Compiled with a global
# re.DOTALL, the last alternative's `.*$` matched newlines too and swallowed
# everything from the first indented line to the end of the output — so a step
# whose report contained one code snippet had all its remaining prose silently
# exempted from compression. The tests passed, because every one of them used a
# snippet at the end.
_PROTECTED = re.compile(
    r"(?s:```.*?```)"                   # fenced code
    r"|(?s:~~~.*?~~~)"                  # the other fence
    r"|`[^`\n]+`"                       # inline code
    r"|\b[a-zA-Z][\w+.\-]*://\S+"       # URL, any scheme
    r"|(?<![\w/])[~.]?/[\w.\-/]+"       # absolute, home-relative or ./ path
    r"|^[ \t]*(?:AION_)?FACT\s+.*$"     # a stated value, carried whole
    r"|^(?:[ ]{4}|\t).*$",              # indented code block
    re.MULTILINE | re.IGNORECASE)


# ── the closed vocabulary ────────────────────────────────────────────────────
# Every entry had to answer one question: does removing it change what a
# downstream agent would DO? Anything arguable was left out. The list is short
# on purpose — most of the win is articles, which are ~8% of English prose.
ARTICLES = ("a", "an", "the")

# Intensifiers and discourse particles with no propositional content. Note the
# absence of "clearly" and "obviously": those are certainty markers, and while
# dropping a booster is safer than dropping a hedge, it still edits how sure
# the writer was. Not worth three characters.
FILLERS = (
    "just", "really", "basically", "actually", "simply", "quite", "very",
    "literally", "essentially", "truly", "rather", "somewhat", "fairly",
)

# Openers a harness emits out of politeness or format habit. Matched at the
# start of a line only — "sure" mid-sentence can be a real word.
_OPENERS = re.compile(
    r"^[ \t]*(?:sure|certainly|of course|absolutely|great question|"
    r"happy to help|i'd be happy to|let me|i'll go ahead and|"
    r"here(?:'s| is) (?:the|a|an|what)?)[,:!.]?\s*",
    re.IGNORECASE | re.MULTILINE)

# Multi-word forms with a shorter exact equivalent. Exact, not approximate:
# "due to the fact that" IS "because", where "in my opinion" is NOT "".
PHRASES = (
    (r"in order to", "to"),
    (r"due to the fact that", "because"),
    (r"for the reason that", "because"),
    (r"at this point in time", "now"),
    (r"at the present time", "now"),
    (r"in the event that", "if"),
    (r"is able to", "can"),
    (r"are able to", "can"),
    (r"has the ability to", "can"),
    (r"have the ability to", "can"),
    (r"a large number of", "many"),
    (r"a small number of", "few"),
    (r"the majority of", "most"),
    (r"in spite of the fact that", "although"),
    (r"it is worth noting that", ""),
    (r"it should be noted that", ""),
    (r"please note that", ""),
    (r"as you can see", ""),
)

_PHRASE_RES = tuple(
    (re.compile(rf"\b{pat}\b", re.IGNORECASE), sub) for pat, sub in PHRASES)

# The trailing `[ \t]*` is consumed, not just matched: deleting a word between
# two spaces leaves two, and `_squeeze_inner` folds those to one everywhere
# except at the start of a line, where there is nothing on the left to fold
# into. That left "\n config is at ..." — an indent that was never written.
_DROP_RE = re.compile(
    r"\b(?:" + "|".join(ARTICLES + FILLERS) + r")\b(?=\s|$|[,;:.!?])[ \t]*",
    re.IGNORECASE)

# Tokens whose presence is checked before and after. Numbers include their
# sign and decimals so "-0.5" is one token, not a stray minus and a 5.
_SIGNIFICANT = re.compile(
    r"-?\d+(?:\.\d+)?%?"                # numbers, with unit-ish suffix
    r"|\b[A-Za-z_][\w.\-]*(?:_[\w.\-]+)+\b"   # snake_case / dotted identifiers
    r"|\b[a-zA-Z][\w+.\-]*://\S+"       # URLs
    r"|(?<![\w/])[~.]?/[\w.\-/]+")      # paths


def _squeeze_inner(text: str) -> str:
    """Collapse the whitespace that deletion leaves behind, within one span.

    Done after every rule rather than inside each one: a rule that deletes a
    word between two spaces leaves two spaces, and chaining eight such rules
    without cleanup produces text with more whitespace than it started with.

    Deliberately does not strip the ends. A free span sits between protected
    ones, and its trailing space is the only thing keeping the last word off
    the front of the code block that follows. The whole result is stripped
    once, at the end, where there is nothing on the other side to glue to.
    """
    text = re.sub(r"[ \t]+([,;:.!?])", r"\1", text)   # space before punctuation
    text = re.sub(r"[ \t]{2,}", " ", text)
    # Trailing whitespace only where a newline follows. `$` with MULTILINE also
    # matches the end of the span, and stripping THERE deleted the single space
    # between the last word and the code block after it — producing
    # `Set`AION_CONFIG`` and `path/home/gio/...`, the second of which stops
    # being a recognisable path at all.
    text = re.sub(r"[ \t]+(?=\n)", "", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _recapitalise(text: str, at_start: bool) -> str:
    """Restore the capital that a dropped leading article took with it.

    "The scout found ..." becomes "scout found ..." otherwise, which reads as
    truncation — and a downstream agent that thinks its input was cut off
    behaves differently from one that thinks it is complete.

    `at_start` is False for every span after the first, because those begin
    mid-sentence. Without it, the prose resuming after a URL got capitalised —
    "https://... Returned 404" — which invents a sentence boundary that was
    never in the output.
    """
    def fix(m: re.Match) -> str:
        return m.group(1) + m.group(2).upper()
    # A blank line starts a sentence as reliably as a full stop does, and it is
    # the only signal available when the previous sentence ended inside a
    # protected span — which is the common shape: prose, code block, prose.
    head = r"\A\s*|" if at_start else ""
    head += r"\n\n[ \t]*|"
    # `\A\s*` rather than `\A`: this runs on a span whose ends are deliberately
    # not stripped, so the dropped article has left a space in front of the
    # word that now begins the sentence.
    return re.sub(rf"((?:{head}[.!?]\s+))([a-z])", fix, text)


def _compress_free(text: str, at_start: bool = True) -> str:
    """Compress one unprotected span, tidy-up included.

    Squeezing and recapitalising happen HERE rather than once over the joined
    result, and that is not a refactor — it is the fix for a real defect. Run
    over the whole string, `[ \\t]{2,} -> " "` reached inside an indented code
    block that had already been copied through safely and collapsed its
    indentation, which in Python is not whitespace but syntax. A protected
    span has to be protected from the cleanup too, not only from the rules.
    """
    text = _OPENERS.sub("", text)
    for rx, sub in _PHRASE_RES:
        text = rx.sub(sub, text)
    text = _DROP_RE.sub("", text)
    return _recapitalise(_squeeze_inner(text), at_start)


def compress(text: str) -> str:
    """Shorten prose without paraphrasing it. Pure and deterministic.

    Protected spans are copied through byte-for-byte; only the gaps between
    them are edited, and only by the closed rules above.
    """
    if not text:
        return ""
    out: list[str] = []
    end = 0
    first = True
    for m in _PROTECTED.finditer(text):
        out.append(_compress_free(text[end:m.start()], first))
        out.append(m.group(0))
        end, first = m.end(), False
    out.append(_compress_free(text[end:], first))
    return "".join(out).strip()


def preserved_tokens(text: str) -> set[str]:
    """Everything in `text` that compression is forbidden to lose.

    Numbers, identifiers, URLs, paths, and every negation. Compared as a set
    in both directions by the tests: a rule that drops one of these is a
    correctness bug, not a tuning question, and word lists reviewed by eye are
    exactly the thing that goes wrong quietly.
    """
    found = set(_SIGNIFICANT.findall(text))
    for word in re.findall(r"[\w']+", text.lower()):
        if word in NEGATIONS:
            found.add(word)
    return found


def savings(before: str, after: str) -> tuple[int, float]:
    """(characters removed, fraction removed). 0.0 for empty input."""
    if not before:
        return 0, 0.0
    gone = len(before) - len(after)
    return gone, gone / len(before)


def fit(text: str, limit: int) -> tuple[str, bool]:
    """Get `text` under `limit` characters, compressing before truncating.

    Returns (text, compressed). Text already under the limit is returned
    untouched — compression is lossy, and paying for it when nothing was going
    to be cut is a cost with no benefit. Only when the alternative is losing
    the end of the output does it become the better of two lossy options.

    The caller still has to truncate if compression was not enough; this does
    not do it, because the truncation notice belongs with the prompt-building
    that knows how to phrase it.
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    return compress(text), True
