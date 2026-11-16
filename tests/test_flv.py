"""The FLV muxer that feeds ffmpeg for the stream with audio."""

from __future__ import annotations

from custom_components.niko_access.flv import FlvMuxer

from .test_hik_decoder import IDR, PPS, SC, SLICE, SPS, ULAW


def flv_tags(data: bytes) -> list[tuple[int, int, bytes]]:
    """(tag type, timestamp ms, body) of each tag after the FLV header."""
    assert data[:3] == b"FLV"
    tags, i = [], 13
    while i < len(data):
        size = int.from_bytes(data[i + 1 : i + 4], "big")
        ms = int.from_bytes(data[i + 4 : i + 7], "big") | data[i + 7] << 24
        tags.append((data[i], ms, data[i + 11 : i + 11 + size]))
        assert int.from_bytes(data[i + 11 + size : i + 15 + size], "big") == 11 + size
        i += 15 + size
    return tags


def test_starts_at_a_keyframe_with_parameter_sets():
    m = FlvMuxer("mulaw")
    assert m.video(0, True, SC + SLICE, 10.0) == b""
    assert m.audio(0, ULAW, 10.0) == b""  # no audio before the video starts
    out = m.video(3000, False, SC + SPS + SC + PPS, 10.1) + m.video(3000, True, SC + IDR, 10.1)
    assert out[:5] == b"FLV\x01\x05"
    (config_type, config_ms, config), (frame_type, frame_ms, frame) = flv_tags(out)
    assert (config_type, config_ms, config[:2]) == (9, 0, b"\x17\x00")
    assert config[5:] == (
        b"\x01\x42\xc0\x1f\xff\xe1"
        + len(SPS).to_bytes(2, "big")
        + SPS
        + b"\x01"
        + len(PPS).to_bytes(2, "big")
        + PPS
    )
    assert (frame_type, frame_ms, frame[:2]) == (9, 0, b"\x17\x01")
    assert frame[5:] == b"".join(len(n).to_bytes(4, "big") + n for n in (SPS, PPS, IDR))


def test_timestamps_follow_the_rtp_clocks():
    m = FlvMuxer("mulaw")
    m.video(90000, True, SC + SPS + SC + PPS + SC + IDR, 5.0)
    # Arrival jitter does not matter, only the RTP timestamps after the first packet.
    frame = flv_tags(b"FLV\x01\x05" + bytes(8) + m.video(93000, True, SC + SLICE, 5.4))
    assert frame[0][:2] == (9, 33)
    first = flv_tags(b"FLV\x01\x05" + bytes(8) + m.audio(1000, ULAW, 5.02))
    second = flv_tags(b"FLV\x01\x05" + bytes(8) + m.audio(1320, ULAW, 5.3))
    assert [first[0][:2], second[0][:2]] == [(8, 20), (8, 60)]
    assert first[0][2] == b"\x82" + ULAW
    # A new upstream session restarts the RTP clock: follow the arrival time again.
    frame = flv_tags(b"FLV\x01\x05" + bytes(8) + m.video(12345, True, SC + IDR, 15.0))
    assert frame[0][:2] == (9, 10000)


def test_without_audio():
    m = FlvMuxer(None)
    out = m.video(0, True, SC + SPS + SC + PPS + SC + IDR, 1.0)
    assert out[:5] == b"FLV\x01\x01"
    assert m.audio(0, ULAW, 1.0) == b""
