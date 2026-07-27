"""XIAO pendant link — HTTP transport, no radio, no device.

The pendant pulls stills and audio from the endpoints the cyclops firmware
serves (/snap, /audio.wav, /stream). Everything here runs against an injected
opener; nothing touches the network.
"""
from __future__ import annotations

import io

import pytest

from aion.deck.pendant import MAX_BLOB, PendantEvent, PendantLink, is_lan_host


class _Resp(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    """Serves canned bodies per path; records what was asked for."""

    def __init__(self, routes: dict, fail=False):
        self.routes = routes
        self.fail = fail
        self.seen: list[str] = []

    def urlopen(self, url, timeout=None):
        self.seen.append(url)
        if self.fail:
            raise OSError("connection refused")
        for path, resp in self.routes.items():
            if url.endswith(path):
                body, status = resp if isinstance(resp, tuple) else (resp, 200)
                return _Resp(body, status)
        return _Resp(b"", 404)


JPEG = b"\xff\xd8\xff\xe0" + b"pretend jpeg" * 4
WAV = b"RIFF....WAVEfmt "


def _link(**kw):
    routes = kw.pop("routes", {"/snap": JPEG, "/audio.wav": WAV})
    kw.setdefault("host", "192.168.1.42")
    return PendantLink(opener=_Opener(routes, fail=kw.pop("fail", False)), **kw)


# ---- host guard ----------------------------------------------------------

@pytest.mark.parametrize("host", ["192.168.1.42", "10.0.0.5", "127.0.0.1",
                                  "172.16.3.9", "localhost"])
def test_lan_hosts_allowed(host):
    assert is_lan_host(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1", "93.184.216.34", ""])
def test_public_hosts_refused(host):
    assert not is_lan_host(host)


def test_unresolvable_host_refused():
    assert not is_lan_host("no-such-host.invalid")


def test_connect_refuses_non_lan_host():
    """A typo'd or stale host must not turn the cockpit into a fetcher for an
    arbitrary internet server -- the blob feeds the context engine."""
    p = PendantLink(host="8.8.8.8", opener=_Opener({"/snap": JPEG}))
    assert p.start() is False
    assert not p.available
    assert "private" in p.last_error


# ---- lifecycle -----------------------------------------------------------

def test_no_host_stays_inert(monkeypatch):
    monkeypatch.delenv("AION_PENDANT_HOST", raising=False)
    p = PendantLink(opener=_Opener({}))
    assert p.start() is False
    assert not p.available


def test_host_from_env(monkeypatch):
    monkeypatch.setenv("AION_PENDANT_HOST", "10.1.2.3")
    p = PendantLink(opener=_Opener({"/snap": JPEG}))
    assert p.host == "10.1.2.3"
    assert p.start() is True


def test_connect_probes_snap():
    p = _link()
    assert p.start() is True
    assert p.available
    assert p._opener.seen == ["http://192.168.1.42:80/snap"]


def test_unreachable_device_degrades_gracefully():
    p = _link(fail=True)
    assert p.start() is False
    assert not p.available
    assert "OSError" in p.last_error


# ---- captures ------------------------------------------------------------

def test_snapshot_returns_jpeg_and_dispatches():
    seen: list[PendantEvent] = []
    p = _link(on_event=seen.append)
    p.start()
    assert p.snapshot() == JPEG
    assert [e.kind for e in seen] == ["vision"]
    assert seen[0].name == "snapshot" and seen[0].blob == JPEG


def test_audio_returns_wav_and_dispatches():
    seen: list[PendantEvent] = []
    p = _link(on_event=seen.append)
    p.start()
    assert p.audio() == WAV
    assert seen[-1].kind == "audio"


def test_non_200_yields_nothing():
    p = _link(routes={"/snap": (b"capture failed", 503)})
    p.start()
    assert p.snapshot() is None
    assert "503" in p.last_error


def test_oversized_body_is_refused():
    """/stream is an endless multipart response; a wrong path must not eat
    the host's RAM."""
    p = _link(routes={"/snap": b"x" * (MAX_BLOB + 10)})
    p.start()
    assert p.snapshot() is None
    assert "exceeded" in p.last_error


def test_midsession_unplug_flips_available():
    p = _link()
    p.start()
    assert p.available
    p._opener.fail = True
    assert p.snapshot() is None
    assert not p.available


def test_stream_url_is_not_fetched():
    p = _link()
    p.start()
    before = len(p._opener.seen)
    assert p.stream_url() == "http://192.168.1.42:80/stream"
    assert len(p._opener.seen) == before  # URL only, no request
