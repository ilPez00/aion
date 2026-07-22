"""Verify the Colmi R02 accelerometer stream + tap detection against real hardware.

The ring's raw-accel byte offsets vary across firmware revisions, so ring.py's
decode_accel() is flagged "verify against your ring". This is that verification:
connect, enable the raw stream, and print — for every accelerometer frame — the
raw 16 bytes in hex NEXT TO the parsed (x, y, z) and magnitude. Tap the ring and
watch which bytes move: if the spike lands in bytes [2:8] the decode is correct;
if it lands elsewhere, you can see exactly which slice to fix.

It also runs the production TapDetector so you can tune its threshold: the live
running-max magnitude tells you where to set it (a firm tap should clear it, a
wrist flick should not).

Usage:
    python scripts/verify_ring_accel.py [ADDRESS] [SECONDS]

    ADDRESS   optional BLE MAC/UUID (default: scan for a device named "R02")
    SECONDS   how long to stream (default: 30)

Reuses src/aion/deck/ring.py — verifying this verifies the cockpit's real path.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.deck.ring import (  # noqa: E402
    NOTIFY_UUID, WRITE_UUID, CMD_RAW, RAW_ACCEL,
    decode, decode_accel, start_raw_accel, TapDetector,
)


async def main(address: str | None, secs: float) -> int:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        print("[ring-verify] bleak not installed — pip install 'aion[ring]'")
        return 1

    if address is None:
        print("[ring-verify] scanning for a device named 'R02' (8s) ...")
        dev = await BleakScanner.find_device_by_name("R02", timeout=8.0)
        if dev is None:
            print("[ring-verify] no R02 found. Pass the MAC explicitly, or make "
                  "sure the ring is charged and not connected to the phone app.")
            return 1
        address = dev.address
    print(f"[ring-verify] connecting to {address} ...")

    taps = TapDetector()
    max_mag = 0.0
    counts = {"accel": 0, "other": 0}

    def on_notify(_char, data: bytearray) -> None:
        nonlocal max_mag
        frame = bytes(data)
        hexs = frame.hex(" ")
        if frame[0] == CMD_RAW and len(frame) > 1 and frame[1] == RAW_ACCEL:
            counts["accel"] += 1
            xyz = decode_accel(frame)
            if xyz is None:
                print(f"[accel?] {hexs}   (decode_accel returned None)")
                return
            mag = (xyz[0] ** 2 + xyz[1] ** 2 + xyz[2] ** 2) ** 0.5
            max_mag = max(max_mag, mag)
            gesture = taps.feed(xyz) or taps.poll()
            tag = f"  <<< {gesture.upper()}" if gesture else ""
            print(f"[accel] {hexs}  xyz={xyz!s:<20} |a|={mag:8.0f} "
                  f"max={max_mag:8.0f}{tag}")
        else:
            counts["other"] += 1
            ev = decode(frame)
            note = f"  ({ev.name}={ev.val})" if ev else ""
            print(f"[other] {hexs}{note}")

    async with BleakClient(address) as client:
        await client.start_notify(NOTIFY_UUID, on_notify)
        await client.write_gatt_char(WRITE_UUID, start_raw_accel(), response=False)
        print(f"[ring-verify] streaming {secs:.0f}s — TAP THE RING a few times, "
              "then a double-tap. Ctrl-C to stop early.\n")
        deadline = time.time() + secs
        try:
            while time.time() < deadline:
                # poll resolves a lone single-tap after its double-window closes
                g = taps.poll()
                if g:
                    print(f"        <<< {g.upper()} (resolved)")
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        await client.stop_notify(NOTIFY_UUID)

    print(f"\n[ring-verify] done. accel frames={counts['accel']} "
          f"other={counts['other']} peak|a|={max_mag:.0f}")
    if counts["accel"] == 0:
        print("[ring-verify] NO accel frames — the enable command or the raw "
              "subtype byte is wrong for this firmware. Check start_raw_accel().")
        return 2
    print(f"[ring-verify] suggested TapDetector threshold ~= {max_mag * 0.6:.0f} "
          "(60% of your peak tap). Set it in TapDetector(threshold=...).")
    return 0


if __name__ == "__main__":
    addr = sys.argv[1] if len(sys.argv) > 1 else None
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    try:
        raise SystemExit(asyncio.run(main(addr, duration)))
    except KeyboardInterrupt:
        print("\n[ring-verify] interrupted")
