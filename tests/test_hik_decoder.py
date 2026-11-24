"""CPD7 media decoding: H.264 video vs. the G.711 audio on the same channel."""

from __future__ import annotations

from custom_components.niko_access.cpd7.hik_decoder import HikStreamDecoder

SC = b"\x00\x00\x00\x01"
SPS = b"\x67\x42\xc0\x1f\x8c\x8d\x40"
PPS = b"\x68\xce\x3c\x80"
IDR = b"\x65\x88\x84\x00\x33"
SLICE = b"\x41\x9a\x02\x04"
# Starts with 0x7c, which the H.264 path would take for an FU-A fragment.
ULAW = bytes([0x7C, 0x7D]) * 160


def cpd7(pt: int, ts: int, payload: bytes, marker: bool = False) -> bytes:
    """One play-socket unit: $-framing, outer RTP, 0x0d, nested RTP, payload."""
    ssrc = b"\x55\x66\x77\x88"
    outer = b"\x80\x60\x00\x01" + ts.to_bytes(4, "big") + ssrc
    inner = bytes([0x80, (0x80 if marker else 0) | pt]) + b"\x00\x01" + ts.to_bytes(4, "big")
    packet = outer + b"\x0d" + inner + ssrc + payload
    return b"$\x01" + len(packet).to_bytes(2, "big") + packet


def test_audio_stays_out_of_the_video():
    d = HikStreamDecoder(1)
    d.feed(
        cpd7(96, 9000, SPS)
        + cpd7(96, 9000, PPS)
        + cpd7(0, 800, ULAW, marker=True)
        + cpd7(96, 9000, IDR, marker=True)
    )
    assert d.take() == SC + SPS + SC + PPS + SC + IDR
    assert d.audio_format == "mulaw"
    assert d.take_media() == [
        ("v", 9000, False, SC + SPS),
        ("v", 9000, False, SC + PPS),
        ("a", 800, True, ULAW),
        ("v", 9000, True, SC + IDR),
    ]
    assert d.take_media() == []


def test_alaw_audio():
    d = HikStreamDecoder(1)
    d.feed(cpd7(8, 0, b"\xd5" * 320, marker=True))
    assert d.take() == b""
    assert d.audio_format == "alaw"
