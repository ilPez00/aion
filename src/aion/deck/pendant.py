"""
pendant.py — Cyclops pendant gateway (Seeed XIAO ESP32-S3 Sense).

Third physical peripheral of the Cyclops trio, after the CyclUno deck
(link.py, USB serial) and the Colmi R02 ring (ring.py, BLE):

    Cycluno deck   -> discrete inputs  (joystick / buttons)            [DONE]
    Colmi R02 ring -> telemetry + taps (HR / SpO2 / accel gestures)    [DONE]
    XIAO pendant   -> camera + mic     (vision snapshots / voice)      [DONE]

Transport: **Wi-Fi HTTP**, pulling from the endpoints the cyclops firmware
already serves (`firmware/xiao/src/camera_capture.cpp`):

    GET /snap       image/jpeg   one still
    GET /capture    image/jpeg   same, without the download disposition
    GET /stream     MJPEG        multipart stream (not consumed here)
    GET /audio.wav  audio/wav    the PDM mic

That decision was recorded as open ("BLE vs Wi-Fi push vs USB-CDC") while the
firmware had already shipped the HTTP server, so this module simply meets the
device where it is. BLE stays available for the ring; the pendant's payloads
(VGA JPEGs, WAV) are far too big for the ~2 KB/s BLE notify throughput measured
during bring-up.

Pull, not push: the pendant has no route back to a laptop that moves networks,
and a device that only answers requests cannot wake the cockpit at 3am. Stills
are fetched on demand, plus an optional periodic poll.

stdlib only — no new dependency, unlike the deck (pyserial) and ring (bleak).
Graceful everywhere: no device, wrong IP, unplugged mid-session — `available`
goes False and the cockpit keeps running.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

DEFAULT_PORT = 80
CONNECT_TIMEOUT = 2.0     # probe: a pendant on the LAN answers fast or not at all
READ_TIMEOUT = 10.0       # a VGA JPEG over the XIAO's WiFi stack is not instant
MAX_BLOB = 4 * 1024 * 1024  # a still or a short clip; never an unbounded stream


@dataclass
class PendantEvent:
    """One decoded pendant payload handed to the cockpit loop."""
    kind: str                       # "vision" | "audio" | "telemetry"
    name: str = ""                  # e.g. "snapshot" | "ptt" | "battery"
    val: int = 0
    blob: bytes = b""               # jpeg frame / audio chunk, when present
    ts: float = field(default_factory=time.time)


def is_lan_host(host: str) -> bool:
    """True when `host` resolves to a private or loopback address.

    The pendant lives on your LAN. Refusing anything else means a typo or a
    stale config cannot make the cockpit fetch from — and hand the context
    engine content from — an arbitrary internet host. Same stance as the
    cyclops companion's SSRF guard on /api/capture.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        # every resolved address must be safe -- a name that returns one LAN
        # answer and one public answer is exactly the DNS-rebinding shape
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


class PendantLink:
    """Owns the XIAO pendant connection over HTTP.

    Args:
        host: pendant address (IP or name). Falls back to AION_PENDANT_HOST.
        port: HTTP port (firmware serves :80).
        on_event: called with each PendantEvent.
        poll_interval: seconds between background snapshots; 0 disables the
            thread entirely and leaves the link on-demand only.
        opener: injected for tests -- anything with .urlopen(url, timeout=…)
            returning a context manager with .status and .read(n).
    """

    def __init__(self, host: str | None = None, port: int = DEFAULT_PORT,
                 on_event: Callable[[PendantEvent], None] | None = None,
                 poll_interval: float = 0.0, opener=None) -> None:
        self.host = host or os.environ.get("AION_PENDANT_HOST", "")
        self.port = port
        self.on_event = on_event
        self.poll_interval = poll_interval
        self.available = False
        self.last_error = ""
        self._opener = opener or urllib.request
        self._running = False
        self._thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        self.available = self._connect()
        if self.available and self.poll_interval > 0:
            self._running = True
            self._thread = threading.Thread(
                target=self._poll_loop, name="aion-pendant", daemon=True)
            self._thread.start()
        return self.available

    def stop(self) -> None:
        self._running = False
        self.available = False

    def _connect(self) -> bool:
        if not self.host:
            print("[pendant] no host (set AION_PENDANT_HOST) -> pendant disabled")
            return False
        if not is_lan_host(self.host):
            # loud: a misconfigured host is a privacy problem, not a typo
            print(f"[pendant] refusing non-LAN host {self.host!r} -> disabled")
            self.last_error = "host is not on a private network"
            return False
        blob = self._get("/snap", timeout=CONNECT_TIMEOUT)
        if blob is None:
            print(f"[pendant] no answer from {self.host}:{self.port} -> disabled")
            return False
        print(f"[pendant] XIAO linked at {self.host}:{self.port}")
        return True

    # ---- transport -------------------------------------------------------
    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def _get(self, path: str, timeout: float = READ_TIMEOUT) -> bytes | None:
        """One GET. Returns the body, or None on any failure (never raises)."""
        try:
            with self._opener.urlopen(self.url(path), timeout=timeout) as resp:
                if getattr(resp, "status", 200) != 200:
                    self.last_error = f"HTTP {resp.status} on {path}"
                    return None
                # bounded read: the firmware also serves an endless MJPEG
                # stream, and one wrong path must not eat the host's RAM
                blob = resp.read(MAX_BLOB + 1)
        except Exception as e:  # noqa: BLE001  (unplugged, timeout, DNS, refused)
            self.last_error = f"{type(e).__name__}: {e}"
            self.available = False
            return None
        if len(blob) > MAX_BLOB:
            self.last_error = f"{path} exceeded {MAX_BLOB} bytes"
            return None
        self.last_error = ""
        return blob

    # ---- captures --------------------------------------------------------
    def snapshot(self) -> bytes | None:
        """One still from the OV2640. Dispatches a "vision" event."""
        blob = self._get("/snap")
        if blob:
            self._dispatch(PendantEvent(kind="vision", name="snapshot", blob=blob))
        return blob

    def audio(self) -> bytes | None:
        """One WAV from the PDM mic. Dispatches an "audio" event."""
        blob = self._get("/audio.wav")
        if blob:
            self._dispatch(PendantEvent(kind="audio", name="clip", blob=blob))
        return blob

    def stream_url(self) -> str:
        """MJPEG URL for a viewer to consume directly (never buffered here)."""
        return self.url("/stream")

    # ---- background poll --------------------------------------------------
    def _poll_loop(self) -> None:
        while self._running:
            time.sleep(self.poll_interval)
            if not self._running:
                return
            if self.snapshot() is None:
                # one miss is a hiccup; the link flips itself unavailable in
                # _get, so stop polling a device that has gone away
                if not self.available:
                    return

    def _dispatch(self, ev: PendantEvent) -> None:
        if self.on_event is not None:
            self.on_event(ev)
