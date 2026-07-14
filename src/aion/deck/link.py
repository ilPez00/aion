"""
link.py — serial transport to the CyclUno deck.

A background thread owns the serial port (pyserial is blocking) and feeds the
frame decoder; decoded INPUT_EVENTs are handed to the asyncio side through
loop.call_soon_threadsafe. Outbound frames (notes/status for the OLED) are
written from the event loop — pyserial writes are thread-safe for our sizes.

Graceful everywhere: no pyserial, no device, unplug mid-session — the deck
just reports unavailable and aion keeps working (same philosophy as evdev
joystick / voice).
"""
from __future__ import annotations

import asyncio
import glob
import threading
import time
from typing import Callable

from .protocol import (
    FrameDecoder, InputEvent, encode_frame,
    MSG_INPUT_EVENT, MSG_NOTE, MSG_STATUS,
)

BAUD = 115200
PORT_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")


def find_port(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    for pattern in PORT_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


class DeckLink:
    """Owns the CyclUno serial connection; emits InputEvents, sends HUD frames."""

    def __init__(self, port: str | None = None,
                 on_event: Callable[[InputEvent], None] | None = None) -> None:
        self.port_hint = port
        self.on_event = on_event      # called on the event loop
        self.available = False
        self._ser = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._decoder = FrameDecoder(self._on_frame)
        self._last_note = 0.0

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        try:
            import serial  # noqa: F401
        except ImportError:
            print("[deck] pyserial not installed -> deck disabled (pip install 'aion[deck]')")
            return False
        port = find_port(self.port_hint)
        if port is None:
            print("[deck] no /dev/ttyACM*|ttyUSB* device -> deck disabled")
            return False
        try:
            import serial
            self._ser = serial.Serial(port, BAUD, timeout=0.2)
        except Exception as e:  # noqa: BLE001
            print(f"[deck] cannot open {port}: {e} -> deck disabled")
            return False
        self._loop = asyncio.get_event_loop()
        self._running = True
        self.available = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        print(f"[deck] CyclUno linked on {port}")
        return True

    def stop(self) -> None:
        self._running = False
        self.available = False
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- reader thread ----------------------------------------------------
    def _read_loop(self) -> None:
        while self._running:
            try:
                data = self._ser.read(64)
            except Exception:  # noqa: BLE001  (unplugged)
                self.available = False
                break
            if data:
                self._decoder.push(data)

    def _on_frame(self, msg_type: int, payload: bytes) -> None:
        # runs on the reader thread — hop to the loop before touching aion
        if msg_type != MSG_INPUT_EVENT or self.on_event is None:
            return
        ev = InputEvent.unpack(payload)
        if ev is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.on_event, ev)

    # ---- outbound (OLED HUD) ----------------------------------------------
    def _write(self, frame: bytes) -> None:
        if not self.available or self._ser is None:
            return
        try:
            self._ser.write(frame)
        except Exception:  # noqa: BLE001
            self.available = False

    def send_note(self, text: str, min_interval: float = 0.5) -> None:
        """Push one OLED line (NOTE frame). Rate-limited: the Uno redraws over
        I2C and floods would starve its input polling. 16 chars = one row of
        the 128x128 panel's 16-col text grid."""
        now = time.monotonic()
        if now - self._last_note < min_interval:
            return
        self._last_note = now
        payload = ('{"text":"%s"}' % text[:16].replace('"', "'")).encode()
        self._write(encode_frame(MSG_NOTE, payload))

    def send_status(self, json_text: str) -> None:
        self._write(encode_frame(MSG_STATUS, json_text.encode()))
