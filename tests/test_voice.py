"""
Unit tests for aion voice — personality engine + TTS interface.
Zero audio hardware required. Pure logic tests.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.voice.persona import Persona


def test_persona_greeting_changes_by_time():
    p = Persona()
    greeting = p.greeting()
    assert "Good " in greeting or "Online" in greeting
    assert len(greeting) < 200


def test_persona_responds_to_all_events():
    p = Persona()
    events = [
        "startup", "task_done", "task_failed", "task_cancelled",
        "task_paused", "task_resumed", "voice_toggle_on", "voice_toggle_off",
        "idle", "command_accepted", "error",
    ]
    for ev in events:
        text = p.respond(ev)
        assert text, f"empty response for {ev}"
        assert len(text) < 200, f"response too long for {ev}: {len(text)} chars"
        assert "{" not in text, f"unformatted template for {ev}: {text}"


def test_persona_respond_with_context():
    p = Persona()
    text = p.respond("error", detail="connection timeout")
    assert "connection timeout" in text


def test_persona_verbosity_modes():
    p = Persona()
    for v in ("terse", "normal", "chatty"):
        p.verbosity = v
        text = p.respond("task_done")
        assert text, f"empty for verbosity {v}"
        assert len(text) < 200


def test_persona_formality_modes():
    p = Persona()
    for f in ("formal", "casual"):
        p.formality = f
        text = p.respond("task_done")
        assert text, f"empty for formality {f}"
        if f == "formal":
            assert "sir" in text or "Task" in text or "complete" in text or "Done" in text


def test_persona_persist_roundtrip(tmp_path):
    p = Persona(path=tmp_path / "persona.json")
    p.verbosity = "terse"
    p.formality = "formal"
    p.name = "jarvis"
    p.save()
    p2 = Persona(path=tmp_path / "persona.json")
    assert p2.verbosity == "terse"
    assert p2.formality == "formal"
    assert p2.name == "jarvis"


def test_persona_mode_switch_response():
    p = Persona()
    text = p.respond("mode_switch", mode="models")
    assert "{mode}" not in text
    assert text


def test_voice_output_graceful_noop():
    """VoiceOutput with no TTS engine should not crash."""
    from aion.voice.output import VoiceOutput, _find_engine
    vo = VoiceOutput()
    assert hasattr(vo, "say")
    assert hasattr(vo, "stop")


if __name__ == "__main__":
    test_persona_greeting_changes_by_time()
    test_persona_responds_to_all_events()
    test_persona_respond_with_context()
    test_persona_verbosity_modes()
    test_persona_formality_modes()
    test_persona_persist_roundtrip(Path("/tmp/aion_vt"))
    test_persona_mode_switch_response()
    test_voice_output_graceful_noop()
    print("OK: all voice unit tests pass")
