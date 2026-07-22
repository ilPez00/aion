"""
pendant.py — Cyclops pendant gateway (Seeed XIAO ESP32-S3 Sense).

Third physical peripheral of the Cyclops trio, after the CyclUno deck
(link.py, USB serial) and the Colmi R02 ring (ring.py, BLE):

    Cycluno deck   -> discrete inputs  (joystick / encoder / buttons)   [DONE]
    Colmi R02 ring -> telemetry + taps (HR / SpO2 / accel gestures)     [DONE]
    XIAO pendant   -> camera + mic     (vision snapshots / voice PTT)   [STUB]

The XIAO ESP32-S3 Sense carries an OV2640 camera, an I2S mic, a microSD slot,
and BLE. In the Cyclops design it is the mobile sensing node: it captures the
scene / voice and feeds them into aion's context, and can also act as a BLE
gateway to the ring when the host has no adapter.

This is a SCAFFOLD. The transport is deliberately unimplemented — the firmware
side (how the pendant advertises: BLE GATT vs. Wi-Fi HTTP push vs. USB CDC) is a
hardware decision not yet made. `available` stays False so nothing downstream
breaks; wiring one transport in later only means filling `_connect()` and
calling `_dispatch()` with real frames. Mirrors the graceful-degrade contract of
link.py / ring.py.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PendantEvent:
    """One decoded pendant payload handed to the cockpit loop."""
    kind: str                       # "vision" | "audio" | "telemetry"
    name: str = ""                  # e.g. "snapshot" | "ptt" | "battery"
    val: int = 0
    blob: bytes = b""               # jpeg frame / audio chunk, when present
    ts: float = field(default_factory=time.time)


class PendantLink:
    """Owns the XIAO pendant connection. Scaffold: reports unavailable.

    Transport TBD (BLE / Wi-Fi / USB-CDC). When chosen, implement `_connect`
    to open it and call `self._dispatch(PendantEvent(...))` per payload; flip
    `available` True on success. Keep it graceful — an absent pendant must never
    stall the cockpit.
    """

    def __init__(self, on_event: Callable[[PendantEvent], None] | None = None) -> None:
        self.on_event = on_event
        self.available = False
        self._running = False

    def start(self) -> bool:
        # No transport implemented yet — stay inert, same as an unplugged deck.
        self.available = self._connect()
        return self.available

    def stop(self) -> None:
        self._running = False

    def _connect(self) -> bool:
        # TODO(cyclops): open the chosen transport to the XIAO ESP32-S3 Sense.
        return False

    def _dispatch(self, ev: PendantEvent) -> None:
        if self.on_event is not None:
            self.on_event(ev)
