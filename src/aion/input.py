"""
input.py — the input router.

The whole point: every device emits the SAME Intent objects. The UI only
speaks Intents. That's what lets a joystick, a keyboard, a trackpad cursor,
and your voice all drive one screen.

  KeyboardInput  -> listens to Textual key events, maps via config keymap
  JoystickInput  -> evdev poll loop on a real stick (graceful if absent)
  VoiceInput     -> wake + STT stub -> intent parser (drop faster-whisper in here)

The Router owns the bus; callers just push Intents into it.
"""
from __future__ import annotations

import asyncio
import math
import time
import threading
from typing import Callable

try:
    import numpy as np          # voice extra; VoiceInput disables itself if absent
except ImportError:
    np = None

from .core import Bus, Intent, IntentType, TOPIC_INTENT, TOPIC_MODE


class Router:
    """Owns the Intent bus endpoint; everything calls .emit(intent)."""

    def __init__(self, bus: Bus, keymap: dict) -> None:
        self.bus = bus
        self.keymap = keymap
        self.devices: list["InputDevice"] = []

    async def emit(self, intent: Intent) -> None:
        await self.bus.publish(TOPIC_INTENT, intent)

    def register(self, device: "InputDevice") -> None:
        self.devices.append(device)
        device.router = self

    async def start_all(self) -> None:
        await asyncio.gather(*(d.start() for d in self.devices if d.available))

    async def stop_all(self) -> None:
        for d in self.devices:
            d.stop()


class InputDevice:
    available = True

    def __init__(self) -> None:
        self.router: Router | None = None

    async def start(self) -> None:
        ...

    def stop(self) -> None:
        ...


# --------------------------------------------------------------------------
# Keyboard — driven by Textual's on_key; this maps a key -> Intent.
# Lives partly in the UI (Textual owns the event loop), so we expose a
# pure mapping function the app calls on each keypress.
# --------------------------------------------------------------------------
class KeyboardMap:
    def __init__(self, keymap: dict) -> None:
        self.k = keymap

    def resolve(self, key: str) -> Intent | None:
        k = self.k
        if key in k["nav_up"]:
            return Intent.navigate("up")
        if key in k["nav_down"]:
            return Intent.navigate("down")
        if key in k["nav_left"]:
            return Intent.navigate("left")
        if key in k["nav_right"]:
            return Intent.navigate("right")
        if key in k["activate"]:
            return Intent.activate()
        if key in k["back"]:
            return Intent.back()
        if key in k["context"]:
            return Intent.context()
        for n in range(1, 10):
            if key == k.get(f"workspace_{n}"):
                return Intent.switch_workspace(index=n - 1)
        return None


# --------------------------------------------------------------------------
# Joystick — real evdev. Deadzone + hysteresis so it doesn't jitter between
# cells. Buttons -> Activate/Back/Context. Graceful if no device.
# --------------------------------------------------------------------------
DEADZONE = 0.35        # fraction of axis range considered "centered"
REPEAT_DELAY = 0.22    # seconds between repeated navigations while held


class JoystickInput(InputDevice):
    def __init__(self, device_path: str | None = None) -> None:
        super().__init__()
        self.path = device_path
        self._task: asyncio.Task | None = None
        self._last_dir: str | None = None
        self._last_t: float = 0.0
        self._running = False

    async def start(self) -> None:
        try:
            from evdev import InputDevice, ecodes, categorize  # type: ignore
        except Exception:  # noqa: BLE001
            self.available = False
            print("[joystick] evdev not installed -> disabled (pip install evdev)")
            return
        try:
            dev = InputDevice(self.path) if self.path else self._find_stick()
        except Exception as e:  # noqa: BLE001
            self.available = False
            print(f"[joystick] no device -> disabled ({e})")
            return
        if dev is None:
            self.available = False
            print("[joystick] no device found -> disabled")
            return
        self._dev = dev
        self._ec = ecodes
        self._cat = categorize
        self._running = True
        self._task = asyncio.create_task(self._loop())

    def _find_stick(self):
        from evdev import list_devices, InputDevice as ID  # type: ignore
        for p in list_devices():
            d = ID(p)
            # crude: anything with ABS_X and at least 2 buttons
            caps = d.capabilities()
            if 0 in caps and 1 in caps and len(caps[1]) >= 2:
                return d
        return None

    async def _loop(self) -> None:
        async for ev in self._dev.async_read_loop():
            if not self._running:
                break
            if ev.type == self._ec.EV_ABS:
                self._on_axis(ev.code, ev.value)
            elif ev.type == self._ec.EV_KEY:
                self._on_key(ev.code, ev.value)

    def _on_axis(self, code, value) -> None:
        # normalize -1..1
        norm = (value - 127) / 127.0
        if abs(norm) < DEADZONE:
            self._last_dir = None
            return
        now = time.time()
        if now - self._last_t < REPEAT_DELAY and self._last_dir == code:
            return
        self._last_t = now
        self._last_dir = code
        if code in (0,):  # ABS_X
            if norm < 0:
                asyncio.create_task(self.router.emit(Intent.navigate("left")))
            else:
                asyncio.create_task(self.router.emit(Intent.navigate("right")))
        elif code in (1,):  # ABS_Y
            if norm < 0:
                asyncio.create_task(self.router.emit(Intent.navigate("up")))
            else:
                asyncio.create_task(self.router.emit(Intent.navigate("down")))

    def _on_key(self, code, value) -> None:
        if value != 1:  # only key-down
            return
        if code in (304, 305):  # BTN_A / BTN_B-ish
            asyncio.create_task(self.router.emit(Intent.activate()))
        elif code in (306,):
            asyncio.create_task(self.router.emit(Intent.back()))
        elif code in (307,):
            asyncio.create_task(self.router.emit(Intent.context()))

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()


# --------------------------------------------------------------------------
# Voice — OFFLINE STT via faster-whisper + sounddevice capture.
# Press the voice key (v) to toggle. While ON, we listen in short chunks,
# run local VAD, and on speech-end transcribe with a lazy-loaded tiny model,
# then map the transcript to an Intent via parse().
# Everything is local (no API) — important on CGNAT / no public IP.
# Graceful: if sounddevice/whisper can't load, voice mode just stays off.
# --------------------------------------------------------------------------
class VoiceInput(InputDevice):
    def __init__(self, model_size: str = "tiny") -> None:
        super().__init__()
        self.model_size = model_size
        self._running = False
        self._thread: threading.Thread | None = None
        self._model = None          # lazy faster-whisper model
        self._stream = None
        self._buf: "list[np.ndarray]" = []
        self._vad_thresh = 0.012    # RMS energy gate
        self._silence_chunks = 0
        self._max_silence = 18      # ~0.3s @ 16k/512 — end of utterance
        self.ws_map = {"models": 0, "tasks": 1, "agent": 2, "memory": 3}

    async def start(self) -> None:
        self.available = True   # enabled on demand via toggle

    # ---- model lazy load (kept off the boot path) -----------------------
    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        try:
            if np is None:
                raise ImportError("numpy not installed (pip install 'aion[voice]')")
            import sounddevice as sd  # noqa: F401
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[voice] STT unavailable ({e}); voice mode disabled")
            self.available = False
            return False

    def toggle(self) -> None:
        if self._running:
            self.stop()
            return
        if not self._ensure_model():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.create_task(self._publish_mode(True)))

    def _loop(self) -> None:
        import sounddevice as sd
        try:
            with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                                blocksize=512, callback=self._cb) as self._stream:
                while self._running:
                    sd.sleep(50)
                    if self._buf and self._silence_chunks >= self._max_silence:
                        self._transcribe()
        except Exception as e:  # noqa: BLE001
            print(f"[voice] capture error: {e}")
            self.stop()
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda: asyncio.create_task(self._publish_mode(False)))

    def _cb(self, indata, frames, time_info, status) -> None:
        import numpy as np
        chunk = np.frombuffer(indata, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        if rms > self._vad_thresh:
            self._silence_chunks = 0
            self._buf.append(chunk)
        else:
            self._silence_chunks += 1

    def _transcribe(self) -> None:
        if not self._buf:
            return
        audio = np.concatenate(self._buf)
        self._buf = []
        self._silence_chunks = 0
        try:
            segments, _ = self._model.transcribe(audio, language="en", beam_size=1)
            text = " ".join(s.text for s in segments).strip()
        except Exception as e:  # noqa: BLE001
            print(f"[voice] transcribe err: {e}")
            return
        if not text:
            return
        print(f"[voice] «{text}»")
        intent = self.parse(text)
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda i=intent: asyncio.create_task(self.router.emit(i)), intent)

    async def _publish_mode(self, active: bool) -> None:
        self._running = active
        await self.router.bus.publish(TOPIC_MODE, {"mode": "voice", "active": active})

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._publish_mode(False)))

    def parse(self, text: str) -> Intent:
        """Map spoken text -> Intent. Replace with LLM/grammar later."""
        t = text.lower().strip()
        for name, idx in self.ws_map.items():
            if f"go to {name}" in t or f"show {name}" in t:
                return Intent.switch_workspace(index=idx)
        if " " in t and (t.startswith("run ") or t.startswith("start ")):
            return Intent.command(text=t.split(" ", 1)[1])
        if t in ("stop", "cancel", "back"):
            return Intent.back()
        return Intent.command(text=t)


# --------------------------------------------------------------------------
# CyclUno deck — physical console over USB serial (see aion.deck).
# AION mode: joy2 + buttons become Intents (this class).
# APP mode: the same events are injected into a uinput gamepad instead
# (deck.gamepad.VirtualPad) so a spawned program can be driven directly.
# deck_intent() is pure so the mapping is unit-testable with no hardware.
# --------------------------------------------------------------------------
from .deck.protocol import (  # noqa: E402
    InputEvent as DeckEvent,
    SRC_JOY2, SRC_BTN, SRC_MODE,
    CODE_STEP_X, CODE_STEP_Y,
    BTN_A, BTN_B, BTN_J2, BTN_X, BTN_Y,
)


def deck_intent(ev: DeckEvent) -> Intent | None:
    """AION-mode mapping: one deck event -> one Intent (None = ignore)."""
    if ev.src == SRC_JOY2:
        if ev.code == CODE_STEP_Y and ev.val:
            return Intent.navigate("down" if ev.val > 0 else "up")
        if ev.code == CODE_STEP_X and ev.val:
            return Intent.navigate("right" if ev.val > 0 else "left")
        return None
    if ev.src == SRC_BTN and ev.val:  # press edges only
        return {
            BTN_A: Intent.activate(),
            BTN_J2: Intent.activate(),
            BTN_B: Intent.back(),
            BTN_X: Intent(IntentType.PAUSE),
            BTN_Y: Intent(IntentType.CANCEL),
        }.get(ev.code)
    return None


class DeckInput(InputDevice):
    """Routes CyclUno events: AION mode -> Intents, APP mode -> uinput pad."""

    def __init__(self, link=None, pad=None, port: str | None = None) -> None:
        super().__init__()
        from .deck.link import DeckLink
        from .deck.gamepad import VirtualPad
        self.link = link or DeckLink(port=port, on_event=self.on_deck_event)
        self.link.on_event = self.on_deck_event
        self.pad = pad or VirtualPad()
        self.app_mode = False

    async def start(self) -> None:
        self.available = self.link.start()

    def stop(self) -> None:
        self.link.stop()
        self.pad.stop()

    def on_deck_event(self, ev: DeckEvent) -> None:
        if ev.src == SRC_MODE:
            self._set_mode(bool(ev.val))
            return
        if self.app_mode:
            self.pad.inject(ev)
            return
        intent = deck_intent(ev)
        if intent is not None and self.router is not None:
            asyncio.create_task(self.router.emit(intent))

    def _set_mode(self, app: bool) -> None:
        self.app_mode = app
        if app:
            self.pad.start()
        if self.router is not None:
            asyncio.create_task(self.router.bus.publish(
                TOPIC_MODE, {"mode": "deck_app", "active": app}))
