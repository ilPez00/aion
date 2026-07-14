"""aion.deck — CyclUno control deck integration.

The CyclUno (Arduino Uno + joysticks/buttons/OLED) is aion's physical
console: in AION mode its inputs drive the cockpit as Intents; in APP mode
they become a virtual Linux gamepad (uinput) that controls whatever program
aion spawned. The OLED mirrors aion status (active harness, task progress).

Wire protocol: the cyclops v2 framing (0xAA 0x55, len, type, payload, CRC16)
over USB serial @115200 — see protocol.py, kept in sync with the firmware's
cyclops_shared.h.
"""
from .protocol import (  # noqa: F401
    MSG_INPUT_EVENT, MSG_NOTE, MSG_DISPLAY_CMD, MSG_STATUS, MSG_HELLO,
    SRC_JOY1, SRC_JOY2, SRC_BTN, SRC_MODE,
    CODE_STEP_X, CODE_STEP_Y, CODE_RAW_X, CODE_RAW_Y,
    BTN_A, BTN_B, BTN_J2, BTN_MODE, BTN_X, BTN_Y,
    MODE_AION, MODE_APP,
    InputEvent, encode_frame, FrameDecoder, crc16_ccitt_false,
)
from .link import DeckLink  # noqa: F401
from .gamepad import VirtualPad, map_event  # noqa: F401
