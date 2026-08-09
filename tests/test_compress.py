"""Compression is only safe if it cannot change what a step would do.

The interesting tests here are not "does it get shorter" — that is easy and
uninteresting. They are the ones that pin what it must never lose: negations,
numbers, identifiers, URLs, paths, code, and stated uncertainty. Those are
asserted as set comparisons over a corpus rather than as hand-picked examples,
because the failure mode of a word list is that someone adds a plausible entry
and nobody notices what it shadowed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.compress import compress, fit, preserved_tokens, savings

# Output shapes a harness actually produces. Kept together because every
# invariant below is asserted across all of them, not one at a time.
CORPUS = [
    "The scout found the api base. It is basically just the default one.",
    "I could not reach the host. The port is not open, so the check did not run.",
    "Set `AION_CONFIG` to the path /home/gio/.config/aion/layout.json and retry.",
    "The endpoint https://api.example.com/v1/the/thing returned 404 in 1.5s.",
    "FACT api_base=https://api.example.com/the/root\nThe scout is quite sure.",
    "```python\nfor x in the_list:\n    print(a, an, the)\n```\nThat is the loop.",
    "Results: 0 passed, 12 failed, -3.5% coverage. None of the tests are green.",
    "It might be the wrong branch. The build probably needs a rerun.",
    "In order to fix it, due to the fact that the tree is dirty, run a stash.",
    "",
    "the",
    "No.",
]


# ── the invariants ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", CORPUS)
def test_nothing_load_bearing_is_lost(text):
    """Numbers, identifiers, URLs, paths and negations survive, all of them."""
    before = preserved_tokens(text)
    after = preserved_tokens(compress(text))
    assert before - after == set(), f"lost {before - after!r} from {text!r}"


@pytest.mark.parametrize("text", CORPUS)
def test_nothing_is_invented(text):
    """The other direction. Compression deletes and substitutes from a closed
    list; it must not conjure an identifier or a number that was not there."""
    assert preserved_tokens(compress(text)) - preserved_tokens(text) == set()


@pytest.mark.parametrize("text", CORPUS)
def test_compression_is_idempotent(text):
    """Compressing twice equals compressing once.

    Not a style preference: `prompt_for` may compress an output that a peer
    already compressed on the way over, and a rule that keeps biting would
    make the handoff depend on how many machines it crossed.
    """
    once = compress(text)
    assert compress(once) == once


@pytest.mark.parametrize("text", CORPUS)
def test_output_is_never_longer(text):
    assert len(compress(text)) <= len(text)


# ── negation, stated as its own case ─────────────────────────────────────────

def test_negations_are_never_dropped():
    text = ("The check did not pass. No host was reachable and none of the "
            "retries worked, so the run cannot continue without a fix.")
    out = compress(text)
    for word in ("not", "No", "none", "cannot", "without"):
        assert word in out, f"{word!r} vanished: {out!r}"


def test_a_negated_sentence_keeps_its_polarity():
    """The sharpest version: the compressed text must not read as the opposite."""
    assert "not" in compress("The port is not open.")
    assert "never" in compress("The scout never reached the endpoint.")


# ── hedges: the deliberate non-feature ───────────────────────────────────────

def test_uncertainty_survives_compression():
    """A guess must not be compressed into a finding.

    This is the reason hedges are absent from the drop list, so it is a test
    rather than a comment. If someone adds "might" or "probably" to FILLERS
    to save a few characters, this fails.
    """
    out = compress("The api base might be https://x.example.com, probably.")
    assert "might" in out
    assert "probably" in out


# ── protected spans ──────────────────────────────────────────────────────────

def test_fenced_code_is_untouched():
    text = "Here is the fix.\n```python\nthe = a_value\n# just a comment\n```\n"
    out = compress(text)
    assert "the = a_value" in out
    assert "# just a comment" in out


def test_inline_code_is_untouched():
    assert "`the_flag`" in compress("Set the flag `the_flag` to a value.")


def test_a_url_does_not_lose_a_path_segment():
    """`http://host/the/path` has a whole-word `the` between two slashes.

    Without protection this produced `http://host//path`, which is a different
    URL that resolves — the worst kind of wrong.
    """
    out = compress("Fetch the page at http://host/the/path now.")
    assert "http://host/the/path" in out


def test_a_filesystem_path_keeps_every_segment():
    out = compress("The config is at /etc/the/aion/a/layout.json really.")
    assert "/etc/the/aion/a/layout.json" in out


def test_fact_lines_pass_through_whole():
    """`swarmfacts` carries these uncompressed by design; compression must not
    reach around it and edit the line before it is parsed."""
    text = "FACT api_base=https://api.example.com/the/root\nThe scout is sure."
    assert "FACT api_base=https://api.example.com/the/root" in compress(text)


def test_indented_code_is_untouched():
    text = "Run the script:\n\n    the_cmd --a-flag the value\n\nThen check.\n"
    assert "    the_cmd --a-flag the value" in compress(text)


def test_a_protected_span_protects_only_itself():
    """The bug this pins: with re.DOTALL set globally, the indented-code
    alternative's `.*$` matched newlines and swallowed everything from the
    first snippet to the end of the output. A step whose report contained one
    code block had all its remaining prose silently exempted, and the suite
    did not notice because every fixture put the snippet last.
    """
    text = ("The first line is prose.\n\n"
            "    a_snippet --flag\n\n"
            "The last line is basically just prose as well.\n")
    out = compress(text)
    assert "    a_snippet --flag" in out              # still protected
    # Case-insensitive: a blank line counts as a sentence start, so the `L`
    # comes back after `The` is dropped.
    assert "last line is prose as well" in out.lower()
    assert "basically" not in out


def test_a_paragraph_after_a_code_block_starts_with_a_capital():
    """Sentence starts are normally found by looking for `.!?`, which is not
    available when the previous sentence ended inside a protected span. A
    blank line is the only signal left, and prose/code/prose is the common
    shape of a step's report."""
    out = compress("Intro.\n\n    a_snippet\n\nThe result was fine.")
    assert "\n\nResult was fine." in out


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_a_fence_still_spans_newlines(fence):
    """The other half of the same change: scoping DOTALL must not cost the
    fences the multi-line matching they need."""
    text = f"Prose.\n{fence}\nthe = a\nb = the\n{fence}\nMore basically prose."
    out = compress(text)
    assert "the = a\nb = the" in out
    assert "basically" not in out


# ── what it actually removes ─────────────────────────────────────────────────

def test_articles_and_fillers_go():
    out = compress("The scout basically just found the answer.")
    assert out == "Scout found answer."


def test_phrases_are_replaced_with_their_exact_equivalent():
    out = compress("In order to proceed, due to the fact that it failed, retry.")
    # Leading capital restored by _recapitalise, hence the case-insensitive read.
    assert "to proceed" in out.lower()
    assert "because it failed" in out


def test_a_dropped_leading_article_leaves_a_capital_behind():
    """Otherwise the output reads as though it had been cut off at the front,
    and a step that thinks its input is truncated behaves differently."""
    assert compress("The scout ran.").startswith("Scout")


def test_openers_are_stripped_only_at_the_start_of_a_line():
    assert compress("Sure! The answer is 42.").startswith("Answer")
    # "sure" as a real word mid-sentence stays.
    assert "sure" in compress("The scout is sure it is 42.")


def test_it_saves_something_on_real_prose():
    text = ("The scout is basically just checking the endpoints. In order to "
            "do that, the runner has to have a token, and the token is really "
            "the same one the cockpit uses.")
    out = compress(text)
    gone, frac = savings(text, out)
    assert gone > 0 and frac > 0.15, (frac, out)


# ── fit() ────────────────────────────────────────────────────────────────────

def test_text_under_the_limit_is_not_compressed():
    """Compression is lossy. Paying for it when nothing would be cut is a cost
    with no benefit, so the untouched text comes back and `compressed` is False.
    """
    text = "The scout found the answer."
    out, compressed = fit(text, 1000)
    assert out == text and compressed is False


def test_text_over_the_limit_is_compressed():
    text = "The scout is basically just checking all of the endpoints again. " * 4
    out, compressed = fit(text, 100)
    assert compressed is True
    assert len(out) < len(text)


def test_fit_does_not_truncate_on_its_own():
    """Even compressed, it may still be over — `fit` reports rather than cuts,
    because the truncation notice belongs with the prompt builder that knows
    how to phrase it."""
    text = "The scout checked the endpoint. " * 50
    out, compressed = fit(text, 10)
    assert compressed is True
    assert len(out) > 10


def test_a_zero_limit_means_no_limit_rather_than_everything_cut():
    text = "The scout ran."
    assert fit(text, 0) == (text, False)


def test_savings_on_empty_input_is_not_a_division_by_zero():
    assert savings("", "") == (0, 0.0)
