"""Local live video for the Niko door station over the CPD7 LAN protocol.

The cloud only brokers a per-device control key (via CAS); the H.264 video is
pulled directly from the station on ports 9010/9020, de-framed in pure Python
(vendored `cpd7/`), and transcoded to MJPEG by ffmpeg for Home Assistant.

With the "stream with audio" option, the camera also offers a stream source: the
station's own H.264, copied, with its G.711 audio encoded to AAC, as MPEG-TS. Home
Assistant then plays it over WebRTC/HLS with sound.

The CPD7 pipeline (shared ffmpeg encoder, keyframe-aware feed) is adapted from
rtammekivi/hikconnect_intercom (MIT); see THIRD_PARTY_LICENSES.md. It is enabled
only when a camera IP is set in the integration options, since the cloud does
not report the station's LAN address.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http import KEY_AUTHENTICATED, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_CAMERA_CHANNEL,
    CONF_CAMERA_IP,
    CONF_CAMERA_QUALITY,
    CONF_CAMERA_STREAM,
    DEFAULT_CAMERA_CHANNEL,
    DEFAULT_CAMERA_QUALITY,
    DEFAULT_CAMERA_STREAM,
    DOMAIN,
    MJPEG_FPS,
    MJPEG_HEIGHT,
    MJPEG_QUALITY,
    MJPEG_WIDTH,
)
from .coordinator import NikoAccessConfigEntry, NikoAccessCoordinator
from .cpd7.hik_decoder import HikStreamDecoder
from .cpd7.lan_client import ControlKeyError, Cpd7LanClient
from .discovery import async_find_ip
from .entity import NikoAccessEntity
from .flv import FlvMuxer

_LOGGER = logging.getLogger(__name__)

_SC = b"\x00\x00\x00\x01"
_EOI = b"\xff\xd9"
_FEED_QUEUE_MAX = 240
_LINGER_SEC = 8.0
_FRAME_TIMEOUT = 10.0
_FIRST_FRAME_TIMEOUT = 15.0
_MAX_EMPTY_READS = 40
# The station drops the local stream while a ring is being set up. Viewers keep
# their response open by repeating the last frame while the upstream reopens.
_KEEPALIVE_SEC = 0.25
_MAX_STALL_SEC = 90.0
_MJPEG_BOUNDARY = "frame"
# Stream source (H.264 + audio as MPEG-TS).
_NATIVE_QUEUE_MAX = 1000
_NATIVE_WAIT_SEC = 1.0
# How long a new stream waits to learn whether the station sends audio.
_AUDIO_PROBE_SEC = 1.0
_NATIVE_STREAMS = f"{DOMAIN}_native_streams"
# Queued for a stream-source consumer whose backlog was dropped.
_FEED_RESET = object()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NikoAccessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    ip = entry.options.get(CONF_CAMERA_IP)
    if not ip:
        return  # camera disabled until the station's LAN IP is configured
    coordinator = entry.runtime_data
    channel = int(entry.options.get(CONF_CAMERA_CHANNEL, DEFAULT_CAMERA_CHANNEL))
    quality = entry.options.get(CONF_CAMERA_QUALITY, DEFAULT_CAMERA_QUALITY)
    native = entry.options.get(CONF_CAMERA_STREAM, DEFAULT_CAMERA_STREAM)
    if native and _NATIVE_STREAMS not in hass.data:
        hass.data[_NATIVE_STREAMS] = {}
        hass.http.register_view(_NativeStreamView(hass.data[_NATIVE_STREAMS]))
    # One camera per device that answered the call endpoint (the door station).
    serials = [s for s, st in coordinator.data.items() if st.call is not None]
    serials = serials or list(coordinator.data)
    async_add_entities(
        NikoCamera(coordinator, serial, ip, channel, quality, native) for serial in serials
    )


class _Cpd7Stream:
    """One CPD7 upstream + one ffmpeg encoder, shared by all viewers.

    Adapted from hikconnect_intercom's _ChannelStream, decoupled from its
    coordinators: the station IP and the CAS key come from this integration.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: NikoAccessCoordinator,
        serial: str,
        ip: str,
        channel: int,
        quality: str,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._serial = serial
        self._ip = ip
        self._channel = channel
        self._quality = quality
        self._key: str | None = None
        self._lan: Cpd7LanClient | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._users = 0
        self._jpeg: bytes | None = None
        self._seq = 0
        self._waiters: set[asyncio.Event] = set()
        self._sps = b""
        self._pps = b""
        self._lock = asyncio.Lock()
        self._linger = None
        self._reviving: asyncio.Task | None = None
        # Stream-source consumers: queues of (arrival time, take_media() list).
        self._feeds: set[asyncio.Queue] = set()
        self._audio_format: str | None = None

    async def acquire(self) -> bool:
        async with self._lock:
            if self._linger is not None:
                self._linger.cancel()
                self._linger = None
            if self._proc is not None and any(t.done() for t in self._tasks):
                await self._teardown()
            if self._proc is None and not await self._open():
                return False
            self._users += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._users = max(0, self._users - 1)
            if self._users or self._proc is None:
                return
            loop = asyncio.get_running_loop()
            self._linger = loop.call_later(
                _LINGER_SEC, lambda: asyncio.create_task(self._linger_teardown())
            )

    async def subscribe(self) -> asyncio.Queue | None:
        """Open (or join) the upstream and receive its decoded media packets.

        A consumer that falls a full queue behind loses its backlog and gets
        ``_FEED_RESET``: it has to continue at the next keyframe.
        """
        if not await self.acquire():
            return None
        feed: asyncio.Queue = asyncio.Queue(maxsize=_NATIVE_QUEUE_MAX)
        self._feeds.add(feed)
        return feed

    async def unsubscribe(self, feed: asyncio.Queue) -> None:
        self._feeds.discard(feed)
        await self.release()

    def _publish_media(self, media: list) -> None:
        item = (asyncio.get_running_loop().time(), media)
        for feed in self._feeds:
            if feed.full():
                while not feed.empty():
                    feed.get_nowait()
                feed.put_nowait(_FEED_RESET)
            feed.put_nowait(item)

    @property
    def audio_format(self) -> str | None:
        """G.711 variant ("mulaw"/"alaw") once the station sent audio."""
        return self._audio_format

    @property
    def alive(self) -> bool:
        return self._proc is not None and not any(t.done() for t in self._tasks)

    def revive(self) -> None:
        """Reopen the upstream in the background, keeping the current viewers."""
        if self._reviving is None or self._reviving.done():
            self._reviving = asyncio.create_task(self._revive())

    async def _revive(self) -> None:
        async with self._lock:
            if not self._users:
                return
            _LOGGER.debug("reopening live feed for %s", self._serial)
            await self._teardown()
            await self._open()

    async def _linger_teardown(self) -> None:
        async with self._lock:
            self._linger = None
            if not self._users:
                await self._teardown()

    @property
    def latest(self) -> tuple[int, bytes] | None:
        return (self._seq, self._jpeg) if self._jpeg is not None else None

    async def frame_after(self, seq: int, timeout: float) -> tuple[int, bytes] | None:  # noqa: ASYNC109
        if self._seq > seq and self._jpeg is not None:
            return self._seq, self._jpeg
        ev = asyncio.Event()
        self._waiters.add(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            return None
        finally:
            self._waiters.discard(ev)
        return self.latest

    def _publish(self, jpeg: bytes) -> None:
        self._jpeg = jpeg
        self._seq += 1
        for ev in self._waiters:
            ev.set()

    async def _control_key(self, refresh: bool) -> str:
        if self._key is None or refresh:
            # CAS fetch is blocking socket I/O -> executor.
            await self._coordinator.client.async_get_sysconf()
            token = self._coordinator.client.cas_token()
            self._key = await self._hass.async_add_executor_job(
                _cas_key, token, self._serial
            )
        return self._key

    async def _open_lan(self) -> Cpd7LanClient | None:
        for refresh in (False, True):
            try:
                key = await self._control_key(refresh)
                c = Cpd7LanClient(
                    self._ip,
                    self._serial,
                    key.encode("ascii"),
                    channel=self._channel,
                    encrypt_stream=True,
                    stream_quality=self._quality,
                )
                await self._hass.async_add_executor_job(c.start)
                return c
            except ControlKeyError:
                self._key = None
                if refresh:
                    _LOGGER.warning("live feed refused for %s after key refresh", self._serial)
                    return None
            except Exception as err:  # noqa: BLE001
                # The station may have moved (DHCP); re-discover its IP once.
                new_ip = await async_find_ip(self._serial)
                if new_ip and new_ip != self._ip:
                    _LOGGER.info("%s moved %s -> %s (SADP)", self._serial, self._ip, new_ip)
                    self._ip = new_ip
                    if not refresh:
                        continue
                _LOGGER.warning("no live feed for %s at %s: %s", self._serial, self._ip, err)
                return None
        return None

    async def _open(self) -> bool:
        lan = await self._open_lan()
        if lan is None:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                get_ffmpeg_manager(self._hass).binary, "-loglevel", "error",
                "-fflags", "+discardcorrupt", "-f", "h264", "-i", "pipe:0",
                "-an", "-c:v", "mjpeg", "-q:v", str(MJPEG_QUALITY), "-r", str(MJPEG_FPS),
                "-vf", f"scale={MJPEG_WIDTH}:{MJPEG_HEIGHT}",
                "-f", "image2pipe", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("ffmpeg failed to start for %s: %s", self._serial, err)
            with contextlib.suppress(Exception):
                await self._hass.async_add_executor_job(lan.close)
            return False
        self._lan = lan
        self._proc = proc
        self._stopping = False
        self._jpeg = None
        self._queue = asyncio.Queue(maxsize=_FEED_QUEUE_MAX)
        if self._sps and self._pps:
            self._queue.put_nowait((False, self._sps + self._pps))
        self._tasks = [
            asyncio.create_task(self._pump_loop()),
            asyncio.create_task(self._feed_loop()),
            asyncio.create_task(self._read_loop()),
        ]
        return True

    async def _pump_loop(self) -> None:
        decoder = HikStreamDecoder(self._channel)
        empty = 0
        try:
            while not self._stopping:
                buf = await self._hass.async_add_executor_job(self._lan.read_chunk)
                if not buf:
                    empty += 1
                    if empty >= _MAX_EMPTY_READS:
                        break
                    continue
                empty = 0
                decoder.feed(buf)
                media = decoder.take_media()
                if decoder.audio_format:
                    self._audio_format = decoder.audio_format
                if media and self._feeds:
                    self._publish_media(media)
                h = decoder.take()
                if not h:
                    continue
                rap = self._scan(h)
                if self._queue.full():
                    self._resync(self._queue)
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait((rap, h))
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("CPD7 pump %s ended: %s", self._serial, err)
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)

    async def _feed_loop(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                self._proc.stdin.write(item[1])
                await self._proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()

    async def _read_loop(self) -> None:
        buf = bytearray()
        while True:
            chunk = await self._proc.stdout.read(64 * 1024)
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                idx = buf.find(_EOI)
                if idx < 0:
                    break
                end = idx + len(_EOI)
                self._publish(bytes(buf[:end]))
                del buf[:end]

    async def _teardown(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.kill()
            self._proc = None
        self._queue = None
        if self._lan is not None:
            with contextlib.suppress(Exception):
                await self._hass.async_add_executor_job(self._lan.close)
            self._lan = None

    def _scan(self, h: bytes) -> bool:
        """Cache the latest SPS/PPS; report whether this chunk opens a new GOP."""
        rap = False
        for seg in h.split(_SC)[1:]:
            if not seg:
                continue
            t = seg[0] & 0x1F
            if t == 7:
                self._sps = _SC + seg
                rap = True
            elif t == 8:
                self._pps = _SC + seg
            elif t == 5:
                rap = True
        return rap

    def _resync(self, q: asyncio.Queue) -> None:
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        keep: list = []
        for i in range(len(items) - 1, 0, -1):
            if items[i][0]:
                keep = items[i:]
                break
        if len(keep) >= _FEED_QUEUE_MAX - 1:
            keep = []
        if keep and self._sps and self._pps:
            q.put_nowait((False, self._sps + self._pps))
        for item in keep:
            q.put_nowait(item)


def _cas_key(token: dict, serial: str) -> str:
    """Blocking CAS call to mint the per-device control key (runs in executor)."""
    from .cpd7.cas import EzvizCAS  # local import: optional deps

    doc = EzvizCAS(token).cas_get_encryption(serial)
    return doc["Response"]["Session"]["@Key"]


class NikoCamera(NikoAccessEntity, Camera):
    _attr_translation_key = "camera"
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: NikoAccessCoordinator,
        serial: str,
        ip: str,
        channel: int,
        quality: str,
        native: bool = False,
    ) -> None:
        NikoAccessEntity.__init__(self, coordinator, serial, "camera")
        Camera.__init__(self)
        self._source = _Cpd7Stream(
            coordinator.hass, coordinator, serial, ip, channel, quality
        )
        self._last: bytes | None = None
        self._native = native
        self._token = secrets.token_urlsafe(32)
        if native:
            self._attr_supported_features = CameraEntityFeature.STREAM

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._native:
            self.hass.data[_NATIVE_STREAMS][self._token] = self

    async def async_will_remove_from_hass(self) -> None:
        if self._native:
            self.hass.data[_NATIVE_STREAMS].pop(self._token, None)
        await super().async_will_remove_from_hass()

    async def stream_source(self) -> str | None:
        """The stream served by _NativeStreamView; its token is the credential."""
        api = self.hass.config.api
        if not self._native or api is None:
            return None
        scheme = "https" if api.use_ssl else "http"
        return f"{scheme}://127.0.0.1:{api.port}/api/{DOMAIN}/stream/{self._token}"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if not await self._source.acquire():
            return self._last
        try:
            got = self._source.latest
            if got is None:
                got = await self._source.frame_after(0, _FIRST_FRAME_TIMEOUT)
            if got is not None:
                self._last = got[1]
        finally:
            await self._source.release()
        return self._last

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        if not await self._source.acquire():
            return web.Response(status=503, text="no live feed")
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}"
            },
        )
        await response.prepare(request)
        loop = asyncio.get_running_loop()
        seq = 0
        jpeg: bytes | None = None
        last_fresh = loop.time()
        next_revive = 0.0
        try:
            while True:
                got = await self._source.frame_after(seq, _KEEPALIVE_SEC)
                now = loop.time()
                if got is not None:
                    seq, jpeg = got
                    self._last = jpeg
                    last_fresh = now
                elif now - last_fresh >= _MAX_STALL_SEC:
                    break
                else:
                    # Stalled: reopen the upstream (right away if it died),
                    # giving each attempt time to reach its first keyframe.
                    stalled = now - last_fresh >= _FRAME_TIMEOUT
                    if now >= next_revive and (stalled or not self._source.alive):
                        self._source.revive()
                        next_revive = now + _FIRST_FRAME_TIMEOUT
                    if jpeg is None:
                        continue  # nothing to repeat yet
                await response.write(
                    b"--" + _MJPEG_BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg + b"\r\n"
                )
        except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
            pass
        finally:
            await self._source.release()
        return response

    async def handle_native_stream(self, request: web.Request) -> web.StreamResponse:
        """The station's H.264 (copied) and G.711 audio (encoded to AAC) as MPEG-TS."""
        feed = await self._source.subscribe()
        if feed is None:
            return web.Response(status=503, text="no live feed")
        response = web.StreamResponse(status=200, headers={"Content-Type": "video/mp2t"})
        loop = asyncio.get_running_loop()
        probe_until = loop.time() + _AUDIO_PROBE_SEC
        last_fresh = loop.time()
        next_revive = 0.0
        pending: list = []
        muxer: FlvMuxer | None = None
        proc: asyncio.subprocess.Process | None = None
        copy: asyncio.Task | None = None
        try:
            await response.prepare(request)
            proc = await _start_native_ffmpeg(self.hass)
            if proc is None:
                return response
            copy = asyncio.create_task(_copy_output(proc, response))
            while not copy.done():
                try:
                    item = await asyncio.wait_for(feed.get(), _NATIVE_WAIT_SEC)
                except TimeoutError:
                    item = None
                now = loop.time()
                if item is None:
                    if now - last_fresh >= _MAX_STALL_SEC:
                        break
                    # Stalled: reopen the upstream, as for MJPEG viewers.
                    stalled = now - last_fresh >= _FRAME_TIMEOUT
                    if now >= next_revive and (stalled or not self._source.alive):
                        self._source.revive()
                        next_revive = now + _FIRST_FRAME_TIMEOUT
                    continue
                last_fresh = now
                if item is _FEED_RESET:
                    # Too far behind: the backlog is gone, continue at a keyframe.
                    pending.clear()
                    if muxer is not None:
                        muxer.resync()
                    continue
                pending.append(item)
                if muxer is None:
                    # Packets carry their own timing, so they can wait until it is
                    # known whether the station sends audio.
                    if self._source.audio_format is None and now < probe_until:
                        continue
                    muxer = FlvMuxer(self._source.audio_format)
                data = b"".join(
                    muxer.audio(ts, payload, at)
                    if kind == "a"
                    else muxer.video(ts, marker, payload, at)
                    for at, media in pending
                    for kind, ts, marker, payload in media
                )
                pending.clear()
                if data:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            asyncio.CancelledError,
        ):
            pass
        finally:
            try:
                if copy is not None:
                    copy.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await copy
                if proc is not None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            finally:
                # Whatever happens above, give the upstream back.
                await self._source.unsubscribe(feed)
        return response


async def _start_native_ffmpeg(hass: HomeAssistant) -> asyncio.subprocess.Process | None:
    """ffmpeg reading FLV on stdin and writing MPEG-TS (video copied, audio as AAC)."""
    try:
        return await asyncio.create_subprocess_exec(
            get_ffmpeg_manager(hass).binary, "-loglevel", "error",
            "-analyzeduration", "500000", "-f", "flv", "-i", "pipe:0",
            "-map", "0:v", "-map", "0:a?", "-c:v", "copy",
            "-c:a", "aac", "-ar", "16000", "-b:a", "32k",
            "-f", "mpegts", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as err:
        _LOGGER.warning("ffmpeg failed to start for the stream source: %s", err)
        return None


async def _copy_output(proc: asyncio.subprocess.Process, response: web.StreamResponse) -> None:
    try:
        while chunk := await proc.stdout.read(64 * 1024):
            await response.write(chunk)
    finally:
        # Unblock the writer when the viewer left or ffmpeg stopped.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


class _NativeStreamView(HomeAssistantView):
    """Serves the cameras' stream sources.

    Home Assistant's stream worker and go2rtc open ``stream_source()`` without
    credentials, so that URL carries a random per-camera token. Other tools can
    open the same stream by entity ID with a Home Assistant access token.
    """

    url = f"/api/{DOMAIN}/stream/{{key}}"
    name = f"api:{DOMAIN}:stream"
    requires_auth = False

    def __init__(self, cameras: dict[str, NikoCamera]) -> None:
        self._cameras = cameras

    async def get(self, request: web.Request, key: str) -> web.StreamResponse:
        camera = self._cameras.get(key)
        if camera is None and request.get(KEY_AUTHENTICATED):
            camera = next((c for c in self._cameras.values() if c.entity_id == key), None)
        if camera is None:
            return web.Response(status=404)
        return await camera.handle_native_stream(request)
