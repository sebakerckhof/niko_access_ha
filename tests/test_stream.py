"""The optional stream source (H.264 + audio as MPEG-TS)."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest
from aiohttp import web
from homeassistant.components.camera import CameraEntityFeature, async_get_stream_source

from custom_components.niko_access import camera as niko_camera
from custom_components.niko_access.cpd7.hik_decoder import HikStreamDecoder
from custom_components.niko_access.flv import FlvMuxer

from .test_hik_decoder import IDR, PPS, SLICE, SPS, ULAW, cpd7
from .test_integration import entry_for

CAMERA = "camera.external_unit_live_video"
CAMERA_OPTIONS = {"camera_ip": "10.6.2.6", "camera_channel": 1, "camera_quality": "main"}


async def setup_camera(hass, **options):
    entry = entry_for(hass)
    hass.config_entries.async_update_entry(entry, options={**CAMERA_OPTIONS, **options})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_mjpeg_only_by_default(hass, cloud):
    await setup_camera(hass)
    assert hass.states.get(CAMERA).attributes["supported_features"] == 0
    assert await async_get_stream_source(hass, CAMERA) is None


async def test_stream_source_when_enabled(hass, cloud):
    await setup_camera(hass, camera_stream=True)
    state = hass.states.get(CAMERA)
    assert state.attributes["supported_features"] == CameraEntityFeature.STREAM
    source = await async_get_stream_source(hass, CAMERA)
    assert source.startswith("http://127.0.0.1:")
    assert "/api/niko_access/stream/" in source
    assert CAMERA not in source


async def test_stream_view_access(hass, cloud, hass_client, hass_client_no_auth, monkeypatch):
    async def fake_stream(self, request):
        return web.Response(text=self.entity_id)

    monkeypatch.setattr(niko_camera.NikoCamera, "handle_native_stream", fake_stream)
    await setup_camera(hass, camera_stream=True)
    token = (await async_get_stream_source(hass, CAMERA)).rsplit("/", 1)[1]
    anonymous = await hass_client_no_auth()
    user = await hass_client()

    # The token in stream_source() is the credential for go2rtc and the stream worker.
    resp = await anonymous.get(f"/api/niko_access/stream/{token}")
    assert resp.status == 200
    assert await resp.text() == CAMERA
    # The entity ID needs a Home Assistant access token.
    assert (await anonymous.get(f"/api/niko_access/stream/{CAMERA}")).status == 404
    resp = await user.get(f"/api/niko_access/stream/{CAMERA}")
    assert resp.status == 200
    assert (await anonymous.get("/api/niko_access/stream/wrong")).status == 404


async def test_stream_muxes_video_and_audio(hass, cloud, hass_client, monkeypatch):
    """The handler turns decoded packets into FLV for ffmpeg (replaced by a copy here)."""
    decoder = HikStreamDecoder(1)
    decoder.feed(
        cpd7(96, 9000, SPS)
        + cpd7(96, 9000, PPS)
        + cpd7(96, 9000, IDR, marker=True)
        + cpd7(0, 800, ULAW, marker=True)
    )
    feed: asyncio.Queue = asyncio.Queue()
    feed.put_nowait((asyncio.get_running_loop().time(), decoder.take_media()))
    released = []

    async def subscribe(self):
        self._audio_format = "mulaw"
        return feed

    async def unsubscribe(self, queue):
        released.append(queue)

    async def fake_ffmpeg(hass):
        # Hand the first 300 bytes of FLV back as the "MPEG-TS" output, then exit.
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read(300))",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(niko_camera._Cpd7Stream, "subscribe", subscribe)
    monkeypatch.setattr(niko_camera._Cpd7Stream, "unsubscribe", unsubscribe)
    monkeypatch.setattr(niko_camera, "_start_native_ffmpeg", fake_ffmpeg)
    await setup_camera(hass, camera_stream=True)
    client = await hass_client()

    resp = await client.get(f"/api/niko_access/stream/{CAMERA}")
    assert resp.headers["Content-Type"] == "video/mp2t"
    body = await resp.read()
    assert body[:5] == b"FLV\x01\x05"  # video + audio
    assert SPS in body and IDR in body
    assert b"\x82" + ULAW[:20] in body
    assert released == [feed]


def keyframe(ts: int) -> bytes:
    return cpd7(96, ts, SPS) + cpd7(96, ts, PPS) + cpd7(96, ts, IDR, marker=True)


async def test_stream_continues_at_a_keyframe_after_losing_its_backlog(
    hass, cloud, hass_client, monkeypatch
):
    """After _FEED_RESET the handler drops frames up to the next keyframe."""
    decoder = HikStreamDecoder(1)
    items = []
    for data in (keyframe(9000), cpd7(96, 12600, SLICE, marker=True), keyframe(16200)):
        decoder.feed(data)
        items.append((100.0 + len(items) * 0.04, decoder.take_media()))
    feed: asyncio.Queue = asyncio.Queue()
    for item in (items[0], niko_camera._FEED_RESET, items[1], items[2]):
        feed.put_nowait(item)

    reference = FlvMuxer("mulaw")
    expected = b""
    for index, (at, media) in enumerate(items):
        if index == 1:
            reference.resync()
        expected += b"".join(reference.video(ts, marker, data, at) for _, ts, marker, data in media)

    async def subscribe(self):
        self._audio_format = "mulaw"
        return feed

    async def unsubscribe(self, queue):
        pass

    async def fake_ffmpeg(hass):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            f"import sys; sys.stdout.buffer.write(sys.stdin.buffer.read({len(expected)}))",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(niko_camera._Cpd7Stream, "subscribe", subscribe)
    monkeypatch.setattr(niko_camera._Cpd7Stream, "unsubscribe", unsubscribe)
    monkeypatch.setattr(niko_camera, "_start_native_ffmpeg", fake_ffmpeg)
    await setup_camera(hass, camera_stream=True)
    client = await hass_client()

    body = await (await client.get(f"/api/niko_access/stream/{CAMERA}")).read()
    assert body == expected
    assert body.count(IDR) == 2
    assert SLICE not in body


async def test_slow_consumer_loses_its_backlog(hass):
    stream = niko_camera._Cpd7Stream(hass, None, "X12345678", "10.6.2.6", 1, "main")
    feed: asyncio.Queue = asyncio.Queue(maxsize=3)
    stream._feeds.add(feed)
    for packet in range(5):
        stream._publish_media([packet])
    items = [feed.get_nowait() for _ in range(feed.qsize())]
    # 0-2 filled the queue; they were dropped when 3 arrived.
    assert items[0] is niko_camera._FEED_RESET
    assert [media for _, media in items[1:]] == [[3], [4]]


class FakeLan:
    """A station that sends a keyframe and an audio packet on every read."""

    def __init__(self) -> None:
        self.closed = False
        self._frame = 0

    def read_chunk(self) -> bytes:
        time.sleep(0.005)
        if self.closed:
            return b""
        self._frame += 1
        return keyframe(self._frame * 3600) + cpd7(0, self._frame * 320, ULAW, marker=True)

    def close(self) -> None:
        self.closed = True


# Stands in for the MJPEG encoder: one (empty) JPEG for everything it reads.
FAKE_ENCODER = """
import sys
while sys.stdin.buffer.read1(65536):
    sys.stdout.buffer.write(bytes([0xFF, 0xD8, 0xFF, 0xD9]))
    sys.stdout.buffer.flush()
"""


async def until(condition, timeout=5.0):  # noqa: ASYNC109
    async with asyncio.timeout(timeout):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.fixture
def station(monkeypatch):
    """Fake station and MJPEG encoder; returns the lists of those that were started."""
    lans: list[FakeLan] = []
    encoders: list[asyncio.subprocess.Process] = []

    async def open_lan(self):
        lans.append(FakeLan())
        return lans[-1]

    async def start_encoder(hass):
        encoders.append(
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                FAKE_ENCODER,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
        )
        return encoders[-1]

    monkeypatch.setattr(niko_camera._Cpd7Stream, "_open_lan", open_lan)
    monkeypatch.setattr(niko_camera, "_start_mjpeg_ffmpeg", start_encoder)
    monkeypatch.setattr(niko_camera, "_LINGER_SEC", 0.05)
    return lans, encoders


async def test_mjpeg_encoder_runs_only_for_mjpeg_viewers(hass, station):
    """A stream-source consumer does not pay for an MJPEG transcode nobody watches."""
    lans, encoders = station
    stream = niko_camera._Cpd7Stream(hass, None, "X12345678", "10.6.2.6", 1, "main")

    feed = await stream.subscribe()
    _, media = await asyncio.wait_for(feed.get(), 5)
    assert [kind for kind, *_ in media] == ["v", "v", "v", "a"]
    assert stream.audio_format == "mulaw"
    assert encoders == []

    # A snapshot or an MJPEG viewer starts the encoder on the running upstream...
    assert await stream.acquire()
    assert (len(lans), len(encoders)) == (1, 1)
    assert await stream.frame_after(0, 5) is not None
    await stream.release()
    # ... and it stops again when they left, while the stream source carries on.
    await until(lambda: stream._proc is None)
    assert stream.alive
    while not feed.empty():
        feed.get_nowait()
    assert await asyncio.wait_for(feed.get(), 5) is not niko_camera._FEED_RESET

    # A second MJPEG viewer gets a new encoder on the same upstream.
    assert await stream.acquire()
    assert (len(lans), len(encoders)) == (1, 2)
    assert await stream.frame_after(0, 5) is not None
    await stream.release()

    await stream.unsubscribe(feed)
    await until(lambda: lans[0].closed)
    assert not stream.alive
    assert stream._proc is None
    for encoder in encoders:
        await encoder.wait()


async def test_mjpeg_only_viewer_opens_and_closes_everything(hass, station):
    """Without a stream-source consumer the upstream follows the MJPEG viewers, as before."""
    lans, encoders = station
    stream = niko_camera._Cpd7Stream(hass, None, "X12345678", "10.6.2.6", 1, "main")

    assert await stream.acquire()
    assert await stream.frame_after(0, 5) is not None
    # A second viewer inside the linger window reuses both.
    await stream.release()
    assert await stream.acquire()
    assert (len(lans), len(encoders)) == (1, 1)
    await stream.release()
    await until(lambda: lans[0].closed)
    assert not stream.alive
    assert stream._proc is None
    await encoders[0].wait()
