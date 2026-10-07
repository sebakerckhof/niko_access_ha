"""Two-way talk client for the Niko/Hik-Connect door station (TTS relay, :9664).

The talk
channel is an UNENCRYPTED length-prefixed TCP protocol to the cloud TTS relay:

    frame = [msgType:4BE][subType:4BE][reserved:4BE][payloadLen:4BE] + payload

Handshake (client -> relay):
    mt=1 st=4  JSON  {"url":"tts://<relay>/talk://<serial>:1:1:cas.ys7.com:6500?
                      clientSessionNotUse:<ssn>:0:378","timestamp":"<ms>","uuid":"<uuid>"}
    mt=1 st=2  the bare tts:// url string
    <- mt=2 st=1  "restult: OK;audio_code_type: G711_MU;audio_code_value=1;"
Audio (both directions):
    mt=3 st=16640  raw G.711 u-law, 8 kHz mono.
    uplink (mic):   160-byte payloads (20 ms).  downlink: 320-byte (40 ms).

`ssn` is the CAS operation code; `<relay>` is ttsAddr:ttsPort from
/v3/configurations/system/info. Talk only works while a call is active.
"""

from __future__ import annotations

import socket
import struct
import time
import uuid

MSG_CONTROL = 1
SUB_INVITE_JSON = 4
SUB_INVITE_URL = 2
MSG_RESULT = 2
MSG_AUDIO = 3
SUB_AUDIO = 16640  # VOICETALK_BUTTON_NORMAL_CMD (0x4100)

UPLINK_FRAME = 160  # bytes of G.711 per 20 ms uplink frame
_HDR = struct.Struct(">IIII")


def _frame(mt: int, st: int, payload: bytes) -> bytes:
    return _HDR.pack(mt, st, 0, len(payload)) + payload


def build_talk_url(relay_ip: str, relay_port: int, serial: str, ssn: str) -> str:
    return (
        f"tts://{relay_ip}:{relay_port}/talk://{serial}:1:1:cas.ys7.com:6500"
        f"?clientSessionNotUse:{ssn}:0:378"
    )


class NikoTalkClient:
    """One active talk session to the TTS relay."""

    def __init__(
        self,
        relay_ip: str,
        relay_port: int,
        serial: str,
        ssn: str,
        timeout: float = 8.0,
    ) -> None:
        self._ip = relay_ip
        self._port = relay_port
        self._serial = serial
        self._ssn = ssn
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._rx = bytearray()
        self.codec: str | None = None
        self.reply_mt = self.reply_st = None
        self.relay_ip: str | None = None

    def start(self) -> str:
        """Connect and perform the invite handshake; returns the result line."""
        # ttsAddr is a load-balancer hostname resolving to several relay nodes;
        # the relay validates the URL host against its own IP, so resolve to a
        # concrete IP and use that same IP both to connect and in the URL.
        family_addr = socket.getaddrinfo(self._ip, self._port, type=socket.SOCK_STREAM)[0]
        relay_ip = family_addr[4][0]
        url = build_talk_url(relay_ip, self._port, self._serial, self._ssn)
        self._sock = socket.create_connection(family_addr[4], self._timeout)
        self._sock.settimeout(self._timeout)
        # The relay's parser is byte-exact: it scans for `\r\n "<key>":"` so the
        # invite must use the captured `{\r\n "url":...,\r\n "timestamp":...,
        # \r\n "uuid":...\r\n}` layout, not compact json.dumps output.
        ts = str(int(time.time() * 1000))
        uid = str(uuid.uuid4())
        invite = (
            '{\r\n'
            f' "url":"{url}",\r\n'
            f' "timestamp":"{ts}",\r\n'
            f' "uuid":"{uid}"\r\n'
            '}'
        )
        self._sock.sendall(_frame(MSG_CONTROL, SUB_INVITE_JSON, invite.encode()))
        self._sock.sendall(_frame(MSG_CONTROL, SUB_INVITE_URL, url.encode()))
        mt, st, payload = self._read_msg()
        self.reply_mt, self.reply_st = mt, st
        self.relay_ip = relay_ip
        text = payload.decode("latin1", "replace")
        if mt != MSG_RESULT or "OK" not in text:
            raise ConnectionError(
                f"talk invite rejected (relay {relay_ip}, mt={mt} st={st}): {text!r}"
            )
        self.codec = text
        return text

    def send_audio(self, mulaw: bytes) -> None:
        """Send G.711 u-law mic audio (chunked into 20 ms frames)."""
        if not self._sock:
            raise ConnectionError("talk not started")
        for off in range(0, len(mulaw), UPLINK_FRAME):
            chunk = mulaw[off:off + UPLINK_FRAME]
            self._sock.sendall(_frame(MSG_AUDIO, SUB_AUDIO, chunk))

    def read_audio(self) -> bytes | None:
        """Read one inbound audio frame's G.711 u-law payload (or None)."""
        try:
            mt, st, payload = self._read_msg()
        except TimeoutError:
            return None
        return payload if mt == MSG_AUDIO else b""

    def _read_msg(self) -> tuple[int, int, bytes]:
        while len(self._rx) < 16:
            self._recv_more()
        mt, st, _res, ln = _HDR.unpack(self._rx[:16])
        while len(self._rx) < 16 + ln:
            self._recv_more()
        payload = bytes(self._rx[16:16 + ln])
        del self._rx[:16 + ln]
        return mt, st, payload

    def _recv_more(self) -> None:
        data = self._sock.recv(4096)
        if not data:
            raise ConnectionError("talk relay closed the connection")
        self._rx += data

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
