"""Incremental decoder for Hik-Connect indoor stations (unencrypted CPD7 media).

Unlike the EZVIZ HP7 (ChaCha20 ``$\\x02`` packets), Hik-Connect indoor stations
such as the DS-KH6320-WTE1 send the local 9020 media UNENCRYPTED.  Each media
unit on the wire is::

    $ \\x01 <len:2 BE>            # RTSP-interleaved framing
      <12-byte RTP header>       # 80 60 <seq> <ts> <ssrc=session>
      <13-byte Hikvision header> # payload[0] == 0x0d, per-packet counter
      <RFC 6184 H.264 payload>   # 0x67 SPS / 0x68 PPS / single NAL / 0x7c FU-A

Decode = strip framing -> drop 12+13 header -> standard RFC 6184 depacketize ->
Annex-B H.264.  Verified against a DS-KH6320-WTE1: H.264 Baseline 1280x720 25fps.

The 13-byte header is 0x0d followed by a nested RTP header.  Its payload type
separates the video from the G.711 audio the station interleaves on the same
channel (a Niko 510-31001 sends PT 0, u-law 8 kHz mono, 320 bytes per 40 ms), so
audio is no longer depacketized as H.264.  ``take_media`` returns both per packet
with the nested RTP timestamp and marker, for muxing with correct timing.
"""

from __future__ import annotations

import logging
import struct

_LOGGER = logging.getLogger(__name__)

_SC = b"\x00\x00\x00\x01"
_RTP_HEADER = 12
_HIK_HEADER = 13
# Static RTP payload types (RFC 3551) of the nested header: G.711 u-law / A-law.
_AUDIO_FORMATS = {0: "mulaw", 8: "alaw"}


class HikStreamDecoder:
    """Incremental Hik-Connect (unencrypted) CPD7 stream decoder.

    Usage mirrors ``cpd7.decoder.StreamDecoder`` so it drops into the same loop::

        d = HikStreamDecoder()
        d.feed(raw_bytes_from_play_socket)
        annexb = d.take()
    """

    def __init__(self, interleave: int = 1) -> None:
        # RTSP-interleaved channel byte after '$'; equals the CPD7 channel
        # (ch1 -> $\x01, ch2 -> $\x02, ...).  Syncing on the wrong channel
        # locks onto stray 0x24 bytes in the payload and emits garbage.
        self._interleave = interleave & 0xFF
        self._marker = bytes([0x24, self._interleave])
        self._buf = bytearray()
        self._out = bytearray()
        self._media: list[tuple[str, int, bool, bytes]] = []
        self._synced = False
        # "mulaw" or "alaw" once the station sent audio.
        self.audio_format: str | None = None

    @property
    def keys_derived(self) -> bool:  # parity with StreamDecoder API
        return True

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self._buf.extend(data)
        while self._consume_one_chunk():
            pass

    def take(self) -> bytes:
        out = bytes(self._out)
        self._out.clear()
        return out

    def take_media(self) -> list[tuple[str, int, bool, bytes]]:
        """``(kind, rtp_ts, marker, data)`` per packet since the last call.

        ``kind`` "v": the Annex-B bytes this packet added to ``take()``;
        "a": raw G.711 audio (see ``audio_format``), which ``take()`` never holds.
        Call it alongside ``take()``, or the list keeps growing.
        """
        out = self._media
        self._media = []
        return out

    # -- internals --------------------------------------------------------
    def _consume_one_chunk(self) -> bool:
        # Resync to the first RTSP-interleaved marker ($ + this channel).
        if not self._synced:
            i = self._buf.find(self._marker)
            if i < 0:
                # keep only the last byte in case '$' straddles a read boundary
                if len(self._buf) > 1:
                    del self._buf[:-1]
                return False
            del self._buf[:i]
            self._synced = True

        if len(self._buf) < 4:
            return False
        if self._buf[0] != 0x24 or self._buf[1] != self._interleave:
            self._synced = False
            return True
        plen = struct.unpack(">H", bytes(self._buf[2:4]))[0]
        total = 4 + plen
        if len(self._buf) < total:
            return False
        packet = bytes(self._buf[4:total])
        del self._buf[:total]
        self._handle_packet(packet)
        return True

    def _handle_packet(self, packet: bytes) -> None:
        if len(packet) < _RTP_HEADER + _HIK_HEADER + 1:
            return
        # Nested RTP header at packet[13:]: marker + payload type, timestamp.
        marker_pt = packet[_RTP_HEADER + 2]
        ts = struct.unpack(">I", packet[_RTP_HEADER + 5:_RTP_HEADER + 9])[0]
        audio_format = _AUDIO_FORMATS.get(marker_pt & 0x7F)
        if audio_format is not None:
            self.audio_format = audio_format
            self._media.append(("a", ts, True, packet[_RTP_HEADER + _HIK_HEADER:]))
            return
        start = len(self._out)
        pay = packet[_RTP_HEADER + _HIK_HEADER:]  # RFC 6184 payload
        t = pay[0] & 0x1F
        if 1 <= t <= 23:
            self._out += _SC + pay
        elif t == 28:  # FU-A
            fu_ind, fu_hdr = pay[0], pay[1]
            if fu_hdr & 0x80:  # start fragment
                self._out += _SC + bytes([(fu_ind & 0xE0) | (fu_hdr & 0x1F)]) + pay[2:]
            else:
                self._out += pay[2:]
        elif t == 24:  # STAP-A
            i = 1
            while i + 2 <= len(pay):
                sz = struct.unpack(">H", pay[i:i + 2])[0]
                i += 2
                if i + sz > len(pay):
                    break
                self._out += _SC + pay[i:i + sz]
                i += sz
        if len(self._out) > start:
            self._media.append(("v", ts, bool(marker_pt & 0x80), bytes(self._out[start:])))
