# Aion + Cyclops — spec vs. reality (reconciliation)

The multi-project PRS ("Aion enterprise spatial workspace" + Cyclops/Cycluno
BOM) describes a **greenfield** system with a flat `bridge/ core/ tui/ web/`
layout. This repo is **not** greenfield — most of the spec already exists under
better names and a more mature design. This doc maps the spec onto reality so
nobody rebuilds what's here, and flags where the spec claims things the
hardware/transport can't actually do.

## Deliverable map (spec → what already exists)

| Spec deliverable            | Reality in this repo                              | Status |
|-----------------------------|---------------------------------------------------|--------|
| `bridge/cycluno.py` (serial)| `deck/link.py` + `deck/protocol.py` + `deck/gamepad.py` | **Exists, better.** Binary CRC16-CCITT-FALSE frames pinned to the Uno's `cyclops_shared.h` — not the spec's "clean JSON strings over serial". Background thread, graceful degrade, HUD writeback, APP-mode uinput gamepad. |
| `bridge/colmi_ring.py` (BLE)| `deck/ring.py` **(new — this change)**            | **Was the one real gap.** Now: `RingLink` BLE transport + pure `decode()`, mirroring `link.py`. |
| `core/engine.py` (WS host)  | `core.py` (Bus/Intent/registry) + `remotes.py` (transport) | **Exists, different transport.** See the transport note below. |
| `tui/app.py` (Textual)      | `ui/app.py` + `ui/{visualizers,gauges,fleet_panel}.py` | **Exists.** Concentric rings = `visualizers.pulse_radar`. |
| `web/index.html` (WebGL HUD)| `static/index.html` (+ `web` extra: websockets, pyte) | **Partial.** `web.py` is **DeepSearch**, NOT the HUD server — name collision. |

## Where the spec is wrong (the critique)

1. **Transport: WebSocket @ `0.0.0.0:8080`, 60 Hz — does not exist.** Reality
   is `remotes.py`: plain HTTP/1.1 JSON, stdlib-only, HMAC-signed, port **8765**,
   poll-based, mDNS marked "future". `websockets` is a **declared-but-unused**
   dep (`web` extra). The spec's "60 FPS state broadcast" is aspirational; the
   real model is request/poll between sibling cockpits. Pick one before building
   the WebGL HUD — don't assume the socket is there.

2. **The R02 has no physical button; "ring tap" is derived, not native.**
   The open Colmi R02 BLE protocol (BlueX RF03 SoC) exposes **telemetry**
   (battery, heart rate, SpO2) and a **raw accelerometer** stream — no button,
   no tap/wave opcode. So the Cyclops "button" is **synthesized** in
   `ring.TapDetector`: accel-magnitude spikes → single/double tap, with a
   refractory debounce and a double-tap window. Two facts the spec got wrong and
   are now fixed in code: (a) checksum is **`sum(first15) % 255`** (mod 255, NOT
   mod 256); (b) the ring streams **nothing until the host writes an enable
   frame** — `RingLink` now writes start-real-time (HR/SpO2) + start-raw-accel
   on connect. The gesture→Intent map (`input.ring_intent`) keeps the spec's
   verbs (single tap → voice PTT, double tap → recenter/ACTIVATE); **that mapping
   plus the TapDetector thresholds are the real tuning knobs.** Accel byte
   offsets in `decode_accel()` are flagged verify-against-ring (firmware varies);
   tap detection keys off magnitude so it survives a consistent mis-mapping.

3. **Latency budget (<16 ms hardware→display) is untested here.** The deck
   crosses thread→loop via `call_soon_threadsafe`; the ring now does the same.
   That's fine for input, but "60 FPS across browser + iPad + Pi3 + AR optics"
   is a rendering claim no module in this repo currently backs.

4. **"Vector Anchors / Memory Core" = `physis.py`.** Not a generic vector store —
   it's a thin client to Gio's Rust `physis_pro` coherence engine
   (`http://127.0.0.1:19876`). Treat that as the brain; don't build a parallel one.

## What this change adds

- `src/aion/deck/ring.py` — Colmi R02 BLE transport (`RingLink`, bleak-optional,
  graceful) + pure `encode()`/`decode()`/`checksum()` (mod 255) + `decode_accel()`
  + `TapDetector` (accel → single/double tap, the synthesized "button"). Sends
  the enable frames the ring requires on connect.
- `src/aion/input.py` — pure `ring_intent(name)` + `RingInput(InputDevice)`:
  telemetry → `TOPIC_MODE` for the HUD, accel → TapDetector → gesture Intents,
  with a `_poll_taps` task resolving lone single-taps after the double window.
- `src/aion/ui/app.py` — `RingInput` **registered on the Router** next to
  `DeckInput`, gated by `cfg["ring"]{enabled,address}`. This is the "aion rework":
  the ring now actually drives the cockpit.
- `src/aion/deck/pendant.py` — Cyclops **pendant scaffold** (XIAO ESP32-S3 Sense,
  camera/mic gateway). Inert `PendantLink` (transport TBD) so the trio
  deck+ring+pendant is represented without breaking anything.
- `pyproject.toml` — `ring = ["bleak"]` optional extra.
- `tests/test_ring.py` — checksum-mod-255, encode roundtrip, HR/SpO2/battery/accel
  decode, TapDetector single-vs-double. No radio. **470 suite tests green.**

## Cyclops trio status

| Peripheral            | Module              | State |
|-----------------------|---------------------|-------|
| Cycluno deck (serial) | `deck/{link,protocol,gamepad}.py` | working |
| Colmi R02 ring (BLE)  | `deck/ring.py` + `input.RingInput` | working (telemetry + derived taps) |
| XIAO pendant (cam/mic)| `deck/pendant.py`   | working transport — HTTP pull from the firmware's `/snap`, `/audio.wav`, `/stream`; LAN-only host guard; stdlib. Ingest into physis context is the remaining piece. |

Verify the ring against real hardware:

    python scripts/verify_ring_accel.py [ADDRESS] [SECONDS]

Streams live accelerometer XYZ + raw frame hex + tap events, so the
`decode_accel()` byte offsets can be confirmed (tap the ring, watch which bytes
move) and the `TapDetector` threshold tuned (it prints a suggested value from
your peak tap). Reuses `deck/ring.py`, so verifying it verifies the cockpit path.

Next real gap: the pendant transport (BLE vs Wi-Fi push vs USB-CDC) and feeding
its vision/audio blobs into aion's context (`physis.py` / the voice pipeline).
