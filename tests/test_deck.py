"""
Deck + memory + app-harness tests — no hardware, no serial port, no uinput.

Protocol framing is pinned against the C++ implementation (cyclops_shared.h):
same CRC16-CCITT-FALSE (checked with the classic "123456789" -> 0x29B1
vector), same byte layout, so a frame encoded here decodes on the Uno and
vice versa.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.deck.protocol import (
    crc16_ccitt_false, encode_frame, FrameDecoder, InputEvent,
    MSG_INPUT_EVENT, MSG_NOTE,
    SRC_JOY2, SRC_WHEEL, SRC_BTN, SRC_MODE,
    CODE_STEP_X, CODE_STEP_Y, CODE_RAW_X, CODE_WHEEL_STEP,
    BTN_A, BTN_B, BTN_WHEEL, BTN_X, BTN_Y, MODE_APP, MODE_AION,
)
from aion.deck.gamepad import map_event, EV_ABS, EV_REL, EV_KEY, ABS_X, REL_WHEEL, BTN_SOUTH
from aion.input import deck_intent, DeckInput
from aion.core import IntentType, Bus, TaskRegistry, TaskState
from aion.memory import MemoryStore
from aion.harnesses import AppHarness, HarnessConfig


# ---- protocol -----------------------------------------------------------

def test_crc_reference_vector():
    # CRC16/CCITT-FALSE("123456789") == 0x29B1 — same algorithm as the firmware
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_frame_roundtrip_with_garbage_and_splits():
    got = []
    dec = FrameDecoder(lambda t, p: got.append((t, p)))
    ev = InputEvent(SRC_WHEEL, CODE_WHEEL_STEP, -3)
    frame = encode_frame(MSG_INPUT_EVENT, ev.pack())
    # garbage before, frame split into single bytes, garbage after
    stream = b"\x00\xaa\x99" + frame + b"\xff" + encode_frame(MSG_NOTE, b'{"text":"hi"}')
    for i in range(len(stream)):
        dec.push(stream[i:i + 1])
    assert got == [(MSG_INPUT_EVENT, ev.pack()), (MSG_NOTE, b'{"text":"hi"}')]
    assert InputEvent.unpack(got[0][1]) == ev


def test_corrupt_crc_dropped_then_resync():
    got = []
    dec = FrameDecoder(lambda t, p: got.append(t))
    bad = bytearray(encode_frame(MSG_NOTE, b"x"))
    bad[-1] ^= 0xFF
    dec.push(bytes(bad) + encode_frame(MSG_NOTE, b"y"))
    assert got == [MSG_NOTE] and len(got) == 1


def test_input_event_negative_val():
    ev = InputEvent(SRC_JOY2, CODE_RAW_X, -512)
    assert InputEvent.unpack(ev.pack()) == ev


# ---- AION-mode intent mapping --------------------------------------------

def test_deck_intent_mapping():
    i = deck_intent(InputEvent(SRC_WHEEL, CODE_WHEEL_STEP, 1))
    assert i.type == IntentType.NAVIGATE and i.payload["dir"] == "down"
    i = deck_intent(InputEvent(SRC_WHEEL, CODE_WHEEL_STEP, -1))
    assert i.payload["dir"] == "up"
    i = deck_intent(InputEvent(SRC_JOY2, CODE_STEP_X, 1))
    assert i.payload["dir"] == "right"
    i = deck_intent(InputEvent(SRC_JOY2, CODE_STEP_Y, -1))
    assert i.payload["dir"] == "up"
    assert deck_intent(InputEvent(SRC_BTN, BTN_WHEEL, 1)).type == IntentType.ACTIVATE
    assert deck_intent(InputEvent(SRC_BTN, BTN_B, 1)).type == IntentType.BACK
    assert deck_intent(InputEvent(SRC_BTN, BTN_X, 1)).type == IntentType.PAUSE
    assert deck_intent(InputEvent(SRC_BTN, BTN_Y, 1)).type == IntentType.CANCEL
    # releases are ignored
    assert deck_intent(InputEvent(SRC_BTN, BTN_A, 0)) is None


# ---- APP-mode gamepad mapping ---------------------------------------------

def test_gamepad_mapping():
    assert map_event(InputEvent(SRC_JOY2, CODE_RAW_X, 300)) == (EV_ABS, ABS_X, 300)
    assert map_event(InputEvent(SRC_JOY2, CODE_RAW_X, 9999)) == (EV_ABS, ABS_X, 512)
    assert map_event(InputEvent(SRC_WHEEL, CODE_WHEEL_STEP, -1)) == (EV_REL, REL_WHEEL, -1)
    assert map_event(InputEvent(SRC_BTN, BTN_A, 1)) == (EV_KEY, BTN_SOUTH, 1)
    assert map_event(InputEvent(SRC_BTN, BTN_A, 0)) == (EV_KEY, BTN_SOUTH, 0)
    # nav-step events never leak into the gamepad
    assert map_event(InputEvent(SRC_JOY2, CODE_STEP_X, 1)) is None


# ---- DeckInput mode routing ------------------------------------------------

class _FakeLink:
    available = True
    def __init__(self):
        self.on_event = None
    def start(self):
        return True
    def stop(self):
        pass


class _FakePad:
    def __init__(self):
        self.injected = []
        self.available = True
        self.started = False
    def start(self):
        self.started = True
        return True
    def inject(self, ev):
        self.injected.append(ev)
        return True
    def stop(self):
        pass


class _FakeRouter:
    def __init__(self):
        self.bus = Bus()
        self.emitted = []
    async def emit(self, intent):
        self.emitted.append(intent)


def test_deckinput_mode_routing():
    async def go():
        d = DeckInput(link=_FakeLink(), pad=_FakePad())
        r = _FakeRouter()
        d.router = r
        modes = []

        async def on_mode(m):
            modes.append(m)
        r.bus.subscribe("mode", on_mode)

        d.on_deck_event(InputEvent(SRC_BTN, BTN_WHEEL, 1))   # AION: -> intent
        await asyncio.sleep(0.05)
        assert len(r.emitted) == 1 and r.emitted[0].type == IntentType.ACTIVATE

        d.on_deck_event(InputEvent(SRC_MODE, 0, MODE_APP))   # switch to APP
        await asyncio.sleep(0.05)
        assert d.app_mode and d.pad.started
        assert modes == [{"mode": "deck_app", "active": True}]

        d.on_deck_event(InputEvent(SRC_JOY2, CODE_RAW_X, 100))  # APP: -> pad
        d.on_deck_event(InputEvent(SRC_BTN, BTN_A, 1))
        await asyncio.sleep(0.05)
        assert len(d.pad.injected) == 2 and len(r.emitted) == 1

        d.on_deck_event(InputEvent(SRC_MODE, 0, MODE_AION))  # back to AION
        await asyncio.sleep(0.05)
        assert not d.app_mode
    asyncio.run(go())


# ---- memory ---------------------------------------------------------------

def test_memory_store(tmp_path=Path("/tmp/aion_mem_ut")):
    tmp_path.mkdir(exist_ok=True)
    f = tmp_path / "memory.json"
    f.unlink(missing_ok=True)
    m = MemoryStore(f)
    m.add("cyclops uses xiao esp32")
    time.sleep(0.01)
    m.add("deck wheel is a KY-040")
    assert len(m.search("")) == 2
    assert m.search("ky-040")[0]["text"] == "deck wheel is a KY-040"
    assert m.items()[0]["n"] == 1 and m.items()[0]["when"] == "today"
    # persists across instances
    assert MemoryStore(f).search("xiao")[0]["text"] == "cyclops uses xiao esp32"
    # forget by view index (newest first -> #1 is the KY-040 fact)
    assert m.forget(1)
    assert [x["text"] for x in m.search("")] == ["cyclops uses xiao esp32"]
    assert not m.forget(99)


# ---- app harness ------------------------------------------------------------

def _proc_state(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    except OSError:
        return "?"


def test_app_harness_spawn_pause_cancel():
    async def go():
        bus = Bus()
        reg = TaskRegistry(bus)
        cfg = HarnessConfig(id="app", type="app", name="App", command="{p}")
        h = AppHarness(cfg, bus, reg)
        task = reg.create("App: sleep", "app")
        runner = asyncio.create_task(h.run(task, "sleep 30"))
        for _ in range(50):
            await asyncio.sleep(0.05)
            if task.state == TaskState.RUNNING:
                break
        assert task.state == TaskState.RUNNING
        pid = next(iter(reg.tasks.values())).log[-1].split("pid ")[1].split(":")[0]
        pid = int(pid)
        assert _proc_state(pid) in ("S", "R")

        h.pause(task)
        for _ in range(20):
            await asyncio.sleep(0.05)
            if _proc_state(pid) == "T":
                break
        assert _proc_state(pid) == "T", "SIGSTOP did not freeze the app"

        h.resume(task)
        for _ in range(20):
            await asyncio.sleep(0.05)
            if _proc_state(pid) in ("S", "R"):
                break
        assert _proc_state(pid) in ("S", "R")

        h.cancel(task)
        await asyncio.wait_for(runner, timeout=5)
        assert task.state == TaskState.CANCELLED
        assert _proc_state(pid) == "?"
    asyncio.run(go())


def test_app_harness_clean_exit():
    async def go():
        bus = Bus()
        reg = TaskRegistry(bus)
        cfg = HarnessConfig(id="app", type="app", name="App", command="{p}")
        h = AppHarness(cfg, bus, reg)
        task = reg.create("App: true", "app")
        await asyncio.wait_for(h.run(task, "true"), timeout=5)
        assert task.state == TaskState.DONE and task.progress == 1.0
    asyncio.run(go())


if __name__ == "__main__":
    test_crc_reference_vector()
    test_frame_roundtrip_with_garbage_and_splits()
    test_corrupt_crc_dropped_then_resync()
    test_input_event_negative_val()
    test_deck_intent_mapping()
    test_gamepad_mapping()
    test_deckinput_mode_routing()
    test_memory_store()
    test_app_harness_spawn_pause_cancel()
    test_app_harness_clean_exit()
    print("OK: deck protocol + intent/gamepad mapping + memory + app harness pass")
