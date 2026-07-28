"""Voice grammar — you cannot eyeball a speech interface, so it gets tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aion import voicecmd  # noqa: E402
from aion.voicecmd import MIN_CONFIDENCE, parse  # noqa: E402


# ── the safety line ──────────────────────────────────────────────────────
@pytest.mark.parametrize("phrase", [
    "approve", "approved", "accept", "allow", "go ahead", "confirm",
    "authorise", "yes go ahead",
])
def test_voice_can_never_approve_a_gate(phrase):
    """A microphone hears the room. A podcast saying "approve" must not
    release `rm -rf`."""
    a = parse(phrase)
    assert a.action == "gate" and a.ok is False
    assert a.args["decision"] == "approve"
    assert "button" in a.say


@pytest.mark.parametrize("phrase", ["reject", "deny", "cancel that", "stop", "no"])
def test_voice_may_always_deny_a_gate(phrase):
    """Denial moves toward the state the engine already defaults to, so a
    mishearing costs a re-run rather than a deleted tree."""
    a = parse(phrase)
    assert a.action == "gate" and a.ok is True
    assert a.args["decision"] == "reject"


def test_approve_inside_a_longer_sentence_is_not_a_gate_approval():
    """Only a leading approval word counts; prose must not trip it."""
    a = parse("the approved design uses a parser")
    assert not (a.action == "gate" and a.args.get("decision") == "approve")


# ── confidence ───────────────────────────────────────────────────────────
def test_a_low_confidence_phrase_is_not_executed():
    a = parse("go to files", confidence=MIN_CONFIDENCE - 0.01)
    assert a.action == "unsure" and a.ok is False
    assert "go to files" in a.say


def test_a_confident_phrase_executes():
    a = parse("go to files", confidence=0.99)
    assert a.action == "goto" and a.args["module"] == "files"


def test_low_confidence_cannot_deny_a_gate_either():
    """Uncertainty means ask, in both directions."""
    a = parse("reject", confidence=0.2)
    assert a.action == "unsure"


# ── navigation ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("phrase,module", [
    ("go to files", "files"),
    ("show agents", "agents"),
    ("open the vault", "vault"),
    ("switch to settings", "settings"),
    ("take me to the terminal", "term"),
    ("bring up kanban", "board"),
    ("system", "system"),
    ("notes", "vault"),
])
def test_module_navigation(phrase, module):
    a = parse(phrase)
    assert a.action == "goto" and a.args["module"] == module


def test_wake_word_is_stripped_not_required():
    assert parse("hey aion go to files").args["module"] == "files"
    assert parse("go to files").args["module"] == "files"


def test_trailing_punctuation_is_ignored():
    assert parse("go to files.").args["module"] == "files"


def test_open_a_folder_is_a_scan_not_a_failure():
    a = parse("open ~/dev/aion")
    assert a.action == "scan" and a.args["dir"] == "~/dev/aion"


# ── graph verbs ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("phrase,expect", [
    ("scan ~/dev", ("scan", "dir", "~/dev")),
    ("browse /tmp", ("scan", "dir", "/tmp")),
    ("filter parser", ("filter", "query", "parser")),
    ("highlight lexer", ("filter", "query", "lexer")),
    ("search for tokenizer", ("search", "query", "tokenizer")),
    ("find the parser", ("search", "query", "the parser")),
    ("isolate factory", ("isolate", "query", "factory")),
])
def test_graph_verbs(phrase, expect):
    action, key, value = expect
    a = parse(phrase)
    assert a.action == action and a.args[key] == value


@pytest.mark.parametrize("phrase,action,args", [
    ("list", "view", {"mode": "list"}),
    ("show the graph", "view", {"mode": "graph"}),
    ("fit", "fit", {}),
    ("zoom out", "fit", {}),
    ("refresh", "refresh", {}),
    ("rescan", "refresh", {}),
    ("clear filter", "filter", {"query": ""}),
    ("go back", "back", {}),
    ("up a level", "up", {}),
    ("what can i say", "help", {}),
])
def test_view_controls(phrase, action, args):
    a = parse(phrase)
    assert a.action == action and a.args == args


# ── delegation + fallback ────────────────────────────────────────────────
def test_todos_delegate_to_the_existing_interpreter():
    """No reason to grow a second vocabulary for something aion already has."""
    a = parse("todo buy milk")
    assert a.action == "command" and "todo" in a.args["command"]


def test_unrecognised_speech_becomes_chat():
    a = parse("what do you think about the parser design")
    assert a.action == "chat" and a.args["text"]


def test_empty_input_is_handled():
    a = parse("")
    assert a.action == "none" and a.ok is False


def test_whitespace_only_input_is_handled():
    assert parse("    ").action == "none"


# ── shape ────────────────────────────────────────────────────────────────
def test_every_action_serialises():
    for phrase in ("go to files", "approve", "reject", "todo x", "hello there", ""):
        d = parse(phrase).as_dict()
        assert set(d) == {"action", "ok", "args", "say", "transcript", "confidence"}


def test_transcript_is_preserved_verbatim():
    a = parse("Go To Files")
    assert a.transcript == "Go To Files"


def test_vocabulary_is_documented():
    v = voicecmd.vocabulary()
    assert v and all("say" in x and "does" in x for x in v)
    assert any("approve" in x["say"] for x in v), "the refusal must be discoverable"
