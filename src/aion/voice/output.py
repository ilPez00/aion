"""
output.py — TTS engine for aion.

Wraps edge-tts (offline-capable, British voice available, fast streaming).
Falls back gracefully: pyttsx3 -> espeak -> silent no-op.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)


class VoiceOutput:
    """Text-to-speech engine. async say(text) speaks asynchronously.

    Subscribes to bus voice events. Graceful if no audio device or TTS engine.
    """

    def __init__(self, voice: str = "en-GB-SoniaNeural") -> None:
        self._voice = voice
        self._engine: _TtsEngine = _find_engine()
        self._playing = False

    async def say(self, text: str) -> None:
        """Speak `text` asynchronously. Returns immediately; audio plays in bg."""
        if not text or self._engine is None:
            return
        self._playing = True
        try:
            await self._engine.say(text, self._voice)
        except Exception as e:
            log.warning("TTS failed: %s", e)
        finally:
            self._playing = False

    def stop(self) -> None:
        self._playing = False


class _TtsEngine:
    def say(self, text: str, voice: str) -> asyncio.Storable:
        ...


class _EdgeTts(_TtsEngine):
    async def say(self, text: str, voice: str) -> None:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save("/tmp/aion_tts.mp3")
        subprocess.run(
            ["ffplay", "-nodisp", "-autoexit", "/tmp/aion_tts.mp3"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


class _Pyttsx3Engine(_TtsEngine):
    async def say(self, text: str, voice: str = "") -> None:
        import pyttsx3
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()


class _SubprocTts(_TtsEngine):
    """Fallback: espeak or say command (macOS)."""
    async def say(self, text: str, voice: str = "") -> None:
        if sys.platform == "darwin":
            subprocess.run(["say", text])
        elif shutil.which("espeak"):
            subprocess.run(["espeak", text])
        # no fallback = silent


def _find_engine() -> _TtsEngine | None:
    try:
        import edge_tts  # noqa: F401
        return _EdgeTts()
    except ImportError:
        pass
    try:
        import pyttsx3  # noqa: F401
        return _Pyttsx3Engine()
    except ImportError:
        pass
    if shutil.which("espeak") or sys.platform == "darwin":
        return _SubprocTts()
    return None
