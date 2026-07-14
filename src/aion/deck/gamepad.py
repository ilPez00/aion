"""
gamepad.py — expose the deck as a virtual Linux gamepad (APP mode).

While the deck is in APP mode, raw joystick 2 axes and the face
buttons are injected into a uinput device named "CyclUno Pad". Any program
that reads a gamepad (games, mpv, RetroArch, browsers) sees a real controller
— aion spawns the program (AppHarness) and the deck plays it.

map_event() is a pure function so the mapping is unit-testable without
/dev/uinput; VirtualPad is the thin evdev wrapper around it (graceful if
uinput is missing or permission-denied).
"""
from __future__ import annotations

from .protocol import (
    InputEvent,
    SRC_JOY2, SRC_BTN,
    CODE_RAW_X, CODE_RAW_Y,
    BTN_A, BTN_B, BTN_J2, BTN_X, BTN_Y,
)

# evdev event type/code numbers (hardcoded so mapping tests need no evdev)
EV_KEY, EV_REL, EV_ABS = 0x01, 0x02, 0x03
ABS_X, ABS_Y = 0x00, 0x01
BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x133, 0x134
BTN_THUMBL, BTN_SELECT = 0x13D, 0x13A

AXIS_RANGE = 512          # firmware sends raw-centered: reading - 512

_BTN_MAP = {
    BTN_A: BTN_SOUTH,      # J1 SW   -> A
    BTN_B: BTN_EAST,       # back    -> B
    BTN_X: BTN_WEST,       # X       -> X
    BTN_Y: BTN_NORTH,      # Y       -> Y
    BTN_J2: BTN_THUMBL,    # J2 stick click
}


def map_event(ev: InputEvent) -> tuple[int, int, int] | None:
    """InputEvent -> (ev_type, ev_code, value) for uinput, or None to drop."""
    if ev.src == SRC_JOY2:
        if ev.code == CODE_RAW_X:
            return (EV_ABS, ABS_X, max(-AXIS_RANGE, min(AXIS_RANGE, ev.val)))
        if ev.code == CODE_RAW_Y:
            return (EV_ABS, ABS_Y, max(-AXIS_RANGE, min(AXIS_RANGE, ev.val)))
        return None
    if ev.src == SRC_BTN:
        code = _BTN_MAP.get(ev.code)
        if code is None:
            return None
        return (EV_KEY, code, 1 if ev.val else 0)
    return None


class VirtualPad:
    """uinput device wrapper. create() lazily; unusable -> stays disabled."""

    def __init__(self) -> None:
        self._ui = None
        self.available = False

    def start(self) -> bool:
        if self._ui is not None:
            return True
        try:
            from evdev import UInput, AbsInfo, ecodes as e
            caps = {
                e.EV_KEY: sorted(_BTN_MAP.values()),
                e.EV_ABS: [
                    (ABS_X, AbsInfo(0, -AXIS_RANGE, AXIS_RANGE, 8, 16, 0)),
                    (ABS_Y, AbsInfo(0, -AXIS_RANGE, AXIS_RANGE, 8, 16, 0)),
                ],
            }
            self._ui = UInput(caps, name="CyclUno Pad", vendor=0x1209, product=0xC1C1)
            self.available = True
            return True
        except Exception as e:  # noqa: BLE001  (no /dev/uinput, no perms)
            print(f"[deck] virtual gamepad unavailable ({e}); APP mode inert")
            self.available = False
            return False

    def inject(self, ev: InputEvent) -> bool:
        mapped = map_event(ev)
        if mapped is None or not self.available:
            return False
        etype, ecode, value = mapped
        try:
            self._ui.write(etype, ecode, value)
            self._ui.syn()
            return True
        except Exception:  # noqa: BLE001
            self.available = False
            return False

    def stop(self) -> None:
        if self._ui is not None:
            try:
                self._ui.close()
            except Exception:  # noqa: BLE001
                pass
            self._ui = None
        self.available = False
