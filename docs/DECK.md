# The CyclUno Deck — aion's physical console

aion is the Jarvis screen; the deck is what your hand rests on. One Arduino
Uno, two thumb joysticks, four face buttons, a mode switch, three LEDs and an
OLED — wired over USB serial, speaking the same v2 frame protocol as the
Cyclops wearable.

![console layout](https://raw.githubusercontent.com/ilPez00/CyclUno/main/docs/img/deck-layout.svg)

![deck wiring](https://raw.githubusercontent.com/ilPez00/CyclUno/main/docs/img/deck-wiring.svg)

Assembly guide (BOM, build order, troubleshooting):
[CyclUno/docs/WIRING.md](https://github.com/ilPez00/CyclUno/blob/main/docs/WIRING.md).

## Why two sticks

- **Joy2** — spatial navigation (workspaces left/right, rows up/down) in AION
  mode; a real analog gamepad stick in APP mode.
- **Joy1** — keeps its original Cyclops job: driving the local OLED HUD, so
  the unit still works standalone against the cyclops brain pipeline.

## Modes

**AION** (default) — deck events become cockpit Intents:

| Control | Intent |
|---------|--------|
| joy2 up/down | navigate up/down |
| joy2 left/right | previous/next workspace |
| X | pause/resume focused task |
| Y | cancel focused task |
| B | back |

**APP** (MODE button; D13 LED lit) — aion creates a uinput gamepad named
**CyclUno Pad** and injects every deck event into it:

| Control | Gamepad |
|---------|---------|
| joy2 | ABS_X / ABS_Y (±512, deadzone 8) |
| A / B / X / Y | BTN_SOUTH / EAST / WEST / NORTH |
| joy2 click | BTN_THUMBL |

Flow: `run app <program>` in the palette (AppHarness spawns it — pause is a
real SIGSTOP, cancel a SIGTERM), press MODE, play. Press MODE again to hand
the deck back to the cockpit; the program keeps running as a task.

The OLED always mirrors aion (active harness, top task progress, task count)
via NOTE frames, rate-limited to 2 Hz so the Uno's input polling never
starves. The REC LED still belongs to the cyclops HUD; LINK lights while
frames flow.

## Wire protocol

Frames: `0xAA 0x55 | len16 | type | payload | crc16` (CRC16-CCITT-FALSE over
len+type+payload) — identical to `cyclops_shared.h`. Deck events are
`MSG_INPUT_EVENT` (type 3) with a 4-byte payload `src, code, int16-LE value`;
the full enum table lives in `src/aion/deck/protocol.py` and is pinned
byte-for-byte by tests on both sides (`tests/test_deck.py` here,
`test/test_deck.cpp` in the CyclUno repo).

## Setup

```bash
pip install -e ".[deck]"        # pyserial
# uinput needs write access (APP mode only):
sudo usermod -aG input $USER    # or a udev rule for /dev/uinput
```

The deck auto-detects `/dev/ttyACM*`/`/dev/ttyUSB*`; pin it in
`config/layout.json` → `"deck": {"enabled": true, "port": "/dev/ttyACM0"}`.
No deck plugged in? aion boots normally — every layer degrades gracefully
(no pyserial → disabled, no port → disabled, no /dev/uinput → APP mode inert).

## If the Uno ever feels small

The design deliberately fits the Uno (56% of its 2 KB RAM). Upgrade paths,
in order of effort: an I/O expander (MCP23017) for more buttons on the same
I2C bus, or port the same headers to a Mega/ESP32 — `deck.h`/`joynav.h` are
pure logic and compile anywhere.
