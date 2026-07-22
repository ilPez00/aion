"""
ring.py — BLE transport to the Colmi R02 smart ring (Cyclops peripheral).

Sibling to link.py (the CyclUno serial deck): another physical peripheral,
different wire. The deck owns a pyserial port on a background thread; the ring
owns a `bleak` client on its own background asyncio loop (bleak is async-native)
and hands decoded frames back to the cockpit loop via call_soon_threadsafe —
the same thread→loop crossing the deck already does.

Graceful everywhere: no bleak, ring out of range, unpaired, disconnect
mid-session — the ring reports unavailable and aion keeps working (same
philosophy as evdev joystick / voice / deck).

Wire protocol (open reverse of the Colmi R02 — BlueX RF03 SoC, Nordic-UART
style; see tahnok/colmi_r02_client, atc1441/ATC_RF03_Ring):
  Service : 6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E
  Notify  : 6E400003-B5A3-F393-E0A9-E50E24DCCA9E   (ring -> host)
  Write   : 6E400002-B5A3-F393-E0A9-E50E24DCCA9E   (host -> ring)
  Frame   : 16 bytes; [0]=command, [1..14]=payload, [15]=checksum
            checksum = sum(frame[0:15]) % 255      # NB: mod 255, not 256

Telemetry is real (battery, heart rate, SpO2, raw accelerometer). The host must
first WRITE an enable command — the ring does not stream unsolicited. There is
NO physical button on the R02 and no native tap/wave BLE event: the "button" the
Cyclops spec wants is DERIVED here from accelerometer spikes (TapDetector).
So this transport emits two event kinds:
    RingEvent(kind="telemetry", ...)  — always real, feeds the HUD
    RingEvent(kind="gesture",  name=) — single_tap / double_tap, derived from accel
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# Nordic-UART style service/characteristics for the Colmi R02.
SERVICE_UUID = "6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E"
NOTIFY_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
WRITE_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"

FRAME_LEN = 16

# command bytes (open-protocol subset we use)
CMD_BATTERY = 0x03
CMD_REAL_TIME = 0x69        # 105 — start/stop real-time HR/SpO2 stream
CMD_REAL_TIME_STOP = 0x6A   # 106
CMD_RAW = 0xA1              # 161 — raw sensor (accelerometer / PPG) stream

# real-time reading subtypes (payload[1] of a CMD_REAL_TIME request/response)
RT_HEART_RATE = 0x01
RT_SPO2 = 0x03
RT_START = 0x01             # action byte: 1=start, 0=continue/stop

# raw-sensor subtypes (payload[0] of a CMD_RAW response)
RAW_ACCEL = 0x03


def checksum(frame: bytes) -> int:
    """R02 trailing checksum: sum of the first 15 bytes, mod 255."""
    return sum(frame[:FRAME_LEN - 1]) % 255


def valid(frame: bytes) -> bool:
    return len(frame) == FRAME_LEN and frame[FRAME_LEN - 1] == checksum(frame)


def encode(cmd: int, payload: bytes = b"") -> bytes:
    """Build a 16-byte outbound frame (checksum appended). Host -> ring."""
    if len(payload) > FRAME_LEN - 2:
        raise ValueError("payload too long for a 16-byte frame")
    f = bytearray(FRAME_LEN)
    f[0] = cmd & 0xFF
    f[1:1 + len(payload)] = payload
    f[FRAME_LEN - 1] = checksum(f)
    return bytes(f)


# Frames the host writes on connect to make the ring start streaming.
def start_real_time(subtype: int) -> bytes:
    return encode(CMD_REAL_TIME, bytes([RT_START, subtype]))


def start_raw_accel() -> bytes:
    return encode(CMD_RAW, bytes([RT_START, RAW_ACCEL]))


def _s16be(hi: int, lo: int) -> int:
    """Two bytes -> signed 16-bit (big-endian)."""
    v = (hi << 8) | lo
    return v - 0x10000 if v & 0x8000 else v


def decode_accel(frame: bytes) -> tuple[int, int, int] | None:
    """Raw accelerometer XYZ from a CMD_RAW/RAW_ACCEL frame.

    NB: exact byte offsets/endianness vary slightly across ring firmware
    revisions — VERIFY against your ring and adjust the slice if XYZ look
    wrong. Tap detection keys off magnitude, so it survives a consistent
    mis-mapping, but real accel values need this pinned.
    """
    if frame[0] != CMD_RAW or frame[1] != RAW_ACCEL:
        return None
    x = _s16be(frame[2], frame[3])
    y = _s16be(frame[4], frame[5])
    z = _s16be(frame[6], frame[7])
    return (x, y, z)


@dataclass
class RingEvent:
    """One decoded ring notification handed to the cockpit loop."""
    kind: str                       # "telemetry" | "gesture"
    name: str = ""                  # telemetry field, or gesture name
    val: int = 0                    # bpm / % / battery / tap count
    xyz: tuple[int, int, int] | None = None   # accel triplet (telemetry accel)
    raw: bytes = b""
    ts: float = field(default_factory=time.time)


def decode(frame: bytes) -> RingEvent | None:
    """Pure frame -> RingEvent (None = ignore). Unit-testable, no BLE."""
    if not valid(frame):
        return None
    cmd = frame[0]
    if cmd == CMD_BATTERY:
        return RingEvent("telemetry", "battery", frame[1], raw=frame)
    if cmd == CMD_REAL_TIME:
        sub, err, val = frame[1], frame[2], frame[3]
        if err:                     # ring reports "no reading yet" — skip
            return None
        if sub == RT_HEART_RATE:
            return RingEvent("telemetry", "heart_rate", val, raw=frame)
        if sub == RT_SPO2:
            return RingEvent("telemetry", "spo2", val, raw=frame)
        return None
    if cmd == CMD_RAW:
        xyz = decode_accel(frame)
        if xyz is None:
            return None
        return RingEvent("telemetry", "accel", xyz=xyz, raw=frame)
    return None


class TapDetector:
    """Accel magnitude -> single/double tap. Pure; the R02 has no real button.

    A "tap" is a spike in |accel| above `threshold` followed by a refractory
    quiet period (debounce). Two taps inside `double_window` seconds fold into
    one "double_tap"; a lone tap that ages past the window emits "single_tap".
    feed() returns a gesture name when one resolves, else None.
    """

    def __init__(self, threshold: float = 2200.0, refractory: float = 0.12,
                 double_window: float = 0.45) -> None:
        self.threshold = threshold
        self.refractory = refractory
        self.double_window = double_window
        self._last_spike = -1e9         # ts of last accepted spike
        self._pending_tap: float | None = None  # ts of a tap awaiting a 2nd

    def feed(self, xyz: tuple[int, int, int], now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        mag = math.sqrt(xyz[0] ** 2 + xyz[1] ** 2 + xyz[2] ** 2)
        if mag >= self.threshold and (now - self._last_spike) >= self.refractory:
            self._last_spike = now
            if (self._pending_tap is not None
                    and (now - self._pending_tap) <= self.double_window):
                self._pending_tap = None
                return "double_tap"
            self._pending_tap = now
            return None                 # wait: might become a double
        return None

    def poll(self, now: float | None = None) -> str | None:
        """Resolve a lone tap once the double-tap window has closed."""
        now = time.time() if now is None else now
        if (self._pending_tap is not None
                and (now - self._pending_tap) > self.double_window):
            self._pending_tap = None
            return "single_tap"
        return None


class RingLink:
    """Owns the Colmi R02 BLE connection; emits RingEvents, graceful if absent."""

    def __init__(self, address: str | None = None,
                 on_event: Callable[[RingEvent], None] | None = None) -> None:
        self.address = address          # explicit MAC, or None to scan
        self.on_event = on_event        # called on the *cockpit* event loop
        self.available = False
        self.battery = -1               # last-known telemetry (HUD reads these)
        self.heart_rate = -1
        self.spo2 = -1
        self._cockpit_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._client = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        """Spin up the BLE loop on a background thread. False if bleak absent."""
        try:
            import bleak  # noqa: F401
        except ImportError:
            print("[ring] bleak not installed; Colmi R02 off "
                  "(pip install 'aion[ring]')")
            return False
        try:
            self._cockpit_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._cockpit_loop = None   # started outside a loop -> emit inline
        self._running = True
        self._thread = threading.Thread(target=self._ble_thread, daemon=True)
        self._thread.start()
        self.available = True
        return True

    def stop(self) -> None:
        self._running = False           # BLE loop notices and unwinds

    # ── background BLE loop ──────────────────────────────────────────────────
    def _ble_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ble_main())
        except Exception as e:  # noqa: BLE001  (out of range, adapter down)
            print(f"[ring] BLE loop ended ({e}); telemetry off")
        finally:
            loop.close()

    async def _ble_main(self) -> None:
        from bleak import BleakClient, BleakScanner
        address = self.address
        if address is None:
            dev = await BleakScanner.find_device_by_name("R02", timeout=8.0)
            address = dev.address if dev else None
        if not address:
            print("[ring] no Colmi R02 found on scan; telemetry off")
            self.available = False
            return
        async with BleakClient(address) as client:
            self._client = client
            await client.start_notify(NOTIFY_UUID, self._on_notify)
            # ask the ring to start streaming (it stays silent otherwise)
            for frame in (start_real_time(RT_HEART_RATE),
                          start_real_time(RT_SPO2),
                          start_raw_accel()):
                await client.write_gatt_char(WRITE_UUID, frame, response=False)
                await asyncio.sleep(0.1)
            while self._running and client.is_connected:
                await asyncio.sleep(0.2)
            await client.stop_notify(NOTIFY_UUID)

    def _on_notify(self, _char, data: bytearray) -> None:
        ev = decode(bytes(data))
        if ev is None:
            return
        if ev.name == "battery":
            self.battery = ev.val
        elif ev.name == "heart_rate":
            self.heart_rate = ev.val
        elif ev.name == "spo2":
            self.spo2 = ev.val
        self._dispatch(ev)

    def _dispatch(self, ev: RingEvent) -> None:
        """Hand the event to the cockpit loop (thread crossing), or inline."""
        if self.on_event is None:
            return
        if self._cockpit_loop is not None:
            self._cockpit_loop.call_soon_threadsafe(self.on_event, ev)
        else:
            self.on_event(ev)
