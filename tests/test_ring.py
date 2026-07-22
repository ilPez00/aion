"""
Colmi R02 ring tests — no radio, no bleak, no device.

Checksum is pinned to the open-protocol rule (sum of the first 15 bytes mod
255). decode(), encode(), decode_accel(), TapDetector, and ring_intent() are
all pure, so the telemetry + derived-tap path is exercised with no Bluetooth.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aion.deck.ring import (
    decode, encode, checksum, valid, decode_accel, TapDetector, RingEvent,
    CMD_BATTERY, CMD_REAL_TIME, CMD_RAW, RT_HEART_RATE, RT_SPO2, RT_START,
    RAW_ACCEL, FRAME_LEN,
)
from aion.input import ring_intent
from aion.core import IntentType


def test_checksum_is_mod_255_not_256():
    # a frame whose first-15 sum is exactly 255 must wrap to 0, not stay 255
    f = bytearray(FRAME_LEN)
    f[0] = 200; f[1] = 55            # sums to 255
    assert checksum(f) == 0          # 255 % 255 == 0  (the mod-256 bug gave 255)


def test_encode_roundtrip_and_valid():
    frame = encode(CMD_REAL_TIME, bytes([RT_START, RT_HEART_RATE]))
    assert len(frame) == FRAME_LEN and valid(frame)
    bad = bytearray(frame); bad[-1] ^= 0xFF
    assert not valid(bytes(bad))
    assert decode(bytes(bad)) is None    # bad checksum drops silently


def test_battery_decode():
    ev = decode(encode(CMD_BATTERY, bytes([88])))
    assert (ev.kind, ev.name, ev.val) == ("telemetry", "battery", 88)


def test_realtime_hr_and_spo2_and_error_skip():
    hr = decode(encode(CMD_REAL_TIME, bytes([RT_HEART_RATE, 0, 72])))
    assert (hr.name, hr.val) == ("heart_rate", 72)
    spo2 = decode(encode(CMD_REAL_TIME, bytes([RT_SPO2, 0, 98])))
    assert spo2.name == "spo2"
    # error byte set -> "no reading yet" -> dropped
    assert decode(encode(CMD_REAL_TIME, bytes([RT_HEART_RATE, 1, 0]))) is None


def test_accel_decode_signed_bigendian():
    # x=+1, y=-1, z=+256
    payload = bytes([RAW_ACCEL, 0x00, 0x01, 0xFF, 0xFF, 0x01, 0x00])
    ev = decode(encode(CMD_RAW, payload))
    assert ev.name == "accel" and ev.xyz == (1, -1, 256)
    assert decode_accel(encode(CMD_RAW, payload)) == (1, -1, 256)


def test_tap_detector_single_then_double():
    d = TapDetector(threshold=1000.0, refractory=0.05, double_window=0.4)
    quiet = (0, 0, 0)
    spike = (1000, 0, 0)
    # one spike: no immediate gesture (might become a double)
    assert d.feed(spike, now=0.0) is None
    # window closes with no 2nd spike -> single_tap
    assert d.poll(now=0.5) == "single_tap"
    # two spikes inside the window -> double_tap on the 2nd
    d2 = TapDetector(threshold=1000.0, refractory=0.05, double_window=0.4)
    assert d2.feed(spike, now=0.0) is None
    assert d2.feed(spike, now=0.2) == "double_tap"
    # sub-threshold motion never taps
    assert d2.feed(quiet, now=1.0) is None


def test_gesture_intent_mapping():
    assert ring_intent("single_tap").type is IntentType.MODE_TOGGLE
    assert ring_intent("single_tap").payload == {"mode": "voice"}
    assert ring_intent("double_tap").type is IntentType.ACTIVATE
    assert ring_intent("unknown") is None
