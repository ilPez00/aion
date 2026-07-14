"""
protocol.py — python port of the cyclops v2 wire framing (cyclops_shared.h).

Frame: 0xAA 0x55 | len_lo len_hi | type | payload… | crc_lo crc_hi
CRC16-CCITT (FALSE, poly 0x1021, seed 0xFFFF) over len(2)+type(1)+payload.
Kept byte-compatible with the C++ side; test_deck.py pins known vectors.

INPUT_EVENT payload (deck -> host), 4 bytes: src(1) code(1) val(int16 LE).

  src 0 JOY1 | 1 JOY2 | 2 (was WHEEL) | 3 BTN | 4 MODE
  joy codes:   0 step-x (±1) · 1 step-y (±1) · 2 raw-x · 3 raw-y (centered)
  btn codes:   0 A · 1 B · 2 J2-SW · 3 (was WHEEL-SW) · 4 MODE · 5 X · 6 Y
               val 1=down 0=up
  mode codes:  0 (val 0=AION 1=APP), sent on every toggle
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# ---- MsgType (subset used over the deck link; ids match cyclops_shared.h)
MSG_HELLO = 1
MSG_HEARTBEAT = 2
MSG_INPUT_EVENT = 3
MSG_DISPLAY_CMD = 6
MSG_NOTE = 7
MSG_STATUS = 8
MSG_CMD = 9

# ---- InputEvent enums
SRC_JOY1, SRC_JOY2, SRC_WHEEL, SRC_BTN, SRC_MODE = 0, 1, 2, 3, 4
CODE_STEP_X, CODE_STEP_Y, CODE_RAW_X, CODE_RAW_Y = 0, 1, 2, 3
# NOTE: CODE_WHEEL_STEP and BTN_WHEEL were removed with the KY-040 wheel;
# their numeric ids (2/3) are kept unused so old firmware stays compatible.
BTN_A, BTN_B, BTN_J2, BTN_WHEEL, BTN_MODE, BTN_X, BTN_Y = 0, 1, 2, 3, 4, 5, 6
MODE_AION, MODE_APP = 0, 1


def crc16_ccitt_false(data: bytes, seed: int = 0xFFFF) -> int:
    crc = seed
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(msg_type: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large")
    body = struct.pack("<HB", len(payload), msg_type) + payload
    crc = crc16_ccitt_false(body)
    return b"\xaa\x55" + body + struct.pack("<H", crc)


class FrameDecoder:
    """Streaming decoder mirroring the C++ state machine (resyncs on garbage)."""

    MAX_PAYLOAD = 253  # C++ buf_ is 256 with 3 bytes of len+type header

    def __init__(self, callback) -> None:
        self._cb = callback  # callback(type: int, payload: bytes)
        self._buf = bytearray()
        self._state = "M1"
        self._len = 0
        self._type = 0
        self._crc = 0

    def push(self, data: bytes) -> None:
        for b in data:
            self._push_byte(b)

    def _push_byte(self, b: int) -> None:
        st = self._state
        if st == "M1":
            if b == 0xAA:
                self._state = "M2"
        elif st == "M2":
            self._state = "L1" if b == 0x55 else ("M2" if b == 0xAA else "M1")
        elif st == "L1":
            self._len = b
            self._state = "L2"
        elif st == "L2":
            self._len |= b << 8
            self._state = "T"
        elif st == "T":
            if self._len > self.MAX_PAYLOAD:
                self._reset()
                return
            self._type = b
            self._buf = bytearray(struct.pack("<HB", self._len, b))
            self._state = "P" if self._len else "CR1"
        elif st == "P":
            self._buf.append(b)
            if len(self._buf) - 3 >= self._len:
                self._state = "CR1"
        elif st == "CR1":
            self._crc = b
            self._state = "CR2"
        elif st == "CR2":
            self._crc |= b << 8
            if self._crc == crc16_ccitt_false(bytes(self._buf)):
                self._cb(self._type, bytes(self._buf[3:]))
            self._reset()

    def _reset(self) -> None:
        self._state = "M1"
        self._buf = bytearray()
        self._len = 0


@dataclass(frozen=True)
class InputEvent:
    src: int
    code: int
    val: int

    def pack(self) -> bytes:
        return struct.pack("<BBh", self.src, self.code, self.val)

    @classmethod
    def unpack(cls, payload: bytes) -> "InputEvent | None":
        if len(payload) != 4:
            return None
        src, code, val = struct.unpack("<BBh", payload)
        return cls(src, code, val)
