"""Minimal FLV muxer for the door station's H.264 video and G.711 audio.

FLV carries both codecs as the station sends them, so ffmpeg only has to copy the
video and encode the audio. Timestamps follow the RTP clocks (90 kHz video, 8 kHz
audio). Each clock is anchored to the arrival of its first packet, which keeps
audio and video in sync, and re-anchored when it jumps, e.g. after the upstream
was reopened.
"""

from __future__ import annotations

_SC = b"\x00\x00\x00\x01"
_VIDEO_RATE = 90000
_AUDIO_RATE = 8000
_RESYNC_MS = 3000
# Audio tag header: SoundFormat 7 (A-law) or 8 (u-law), 16-bit, mono. FLV defines
# G.711 at 8 kHz only, which is what the station sends.
_AUDIO_HEADER = {"alaw": 0x72, "mulaw": 0x82}
_TAG_AUDIO = 8
_TAG_VIDEO = 9


def _tag(kind: int, ms: int, data: bytes) -> bytes:
    size = len(data)
    return (
        bytes([kind])
        + size.to_bytes(3, "big")
        + (ms & 0xFFFFFF).to_bytes(3, "big")
        + bytes([(ms >> 24) & 0xFF, 0, 0, 0])
        + data
        + (11 + size).to_bytes(4, "big")
    )


class FlvMuxer:
    """Turns ``HikStreamDecoder.take_media()`` packets into an FLV byte stream.

    Output starts with the first keyframe that has its SPS/PPS; audio before that
    is dropped. ``audio_format`` None writes a video-only FLV. After ``resync()``
    the video continues at the next keyframe.
    """

    def __init__(self, audio_format: str | None) -> None:
        self._audio_header = _AUDIO_HEADER.get(audio_format or "")
        self._start: float | None = None
        self._clocks: dict[int, tuple[int, float]] = {}
        self._last: dict[int, int] = {}
        self._unit = bytearray()
        self._unit_ts: int | None = None
        self._unit_at = 0.0
        self._sps = b""
        self._pps = b""
        self._config = b""
        self._resync = False

    def video(self, ts: int, marker: bool, data: bytes, now: float) -> bytes:
        """One RTP packet's Annex-B bytes; returns FLV bytes for completed frames."""
        out = b""
        if self._unit_ts is not None and ts != self._unit_ts:
            out = self._flush()
        if self._unit_ts is None:
            self._unit_ts, self._unit_at = ts, now
        self._unit += data
        if marker:
            out += self._flush()
        return out

    def audio(self, ts: int, data: bytes, now: float) -> bytes:
        """One RTP packet of G.711; returns its FLV tag once the video started."""
        if self._start is None or self._audio_header is None:
            return b""
        ms = self._ms(_TAG_AUDIO, ts, _AUDIO_RATE, now)
        return _tag(_TAG_AUDIO, ms, bytes([self._audio_header]) + data)

    def resync(self) -> None:
        """Packets were lost: drop the frame in progress, resume at the next keyframe."""
        self._unit.clear()
        self._unit_ts = None
        self._resync = True

    def _flush(self) -> bytes:
        nals = [nal for nal in bytes(self._unit).split(_SC) if nal]
        ts, now = self._unit_ts, self._unit_at
        self._unit.clear()
        self._unit_ts = None
        keyframe = False
        for nal in nals:
            nal_type = nal[0] & 0x1F
            if nal_type == 7 and len(nal) >= 4:
                self._sps = nal
            elif nal_type == 8:
                self._pps = nal
            elif nal_type == 5:
                keyframe = True
        if self._resync:
            if not keyframe:
                return b""
            self._resync = False
        out = b""
        if self._start is None:
            if not (keyframe and self._sps and self._pps):
                return b""
            self._start = now
            flags = 0x01 | (0x04 if self._audio_header is not None else 0)
            out = b"FLV\x01" + bytes([flags]) + (9).to_bytes(4, "big") + bytes(4)
        ms = self._ms(_TAG_VIDEO, ts, _VIDEO_RATE, now)
        config = (
            bytes([1, self._sps[1], self._sps[2], self._sps[3], 0xFF, 0xE1])
            + len(self._sps).to_bytes(2, "big")
            + self._sps
            + b"\x01"
            + len(self._pps).to_bytes(2, "big")
            + self._pps
        )
        if config != self._config:
            # AVC sequence header (AVCDecoderConfigurationRecord).
            self._config = config
            out += _tag(_TAG_VIDEO, ms, b"\x17\x00\x00\x00\x00" + config)
        body = b"".join(len(nal).to_bytes(4, "big") + nal for nal in nals)
        frame = b"\x17" if keyframe else b"\x27"
        return out + _tag(_TAG_VIDEO, ms, frame + b"\x01\x00\x00\x00" + body)

    def _ms(self, kind: int, ts: int, rate: int, now: float) -> int:
        """Milliseconds since the first keyframe on the RTP clock of ``kind``."""
        wall = (now - self._start) * 1000
        ms = wall
        clock = self._clocks.get(kind)
        if clock is not None:
            ms = clock[1] + ((ts - clock[0]) & 0xFFFFFFFF) * 1000 / rate
            if abs(ms - wall) > _RESYNC_MS:
                clock = None
                ms = wall
        if clock is None:
            self._clocks[kind] = (ts, ms)
        ms = max(round(ms), self._last.get(kind, -1) + 1)
        self._last[kind] = ms
        return ms
