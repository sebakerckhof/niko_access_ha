"""Local live video for the Niko door station over the CPD7 LAN protocol.

The cloud only brokers a per-device control key (via CAS); the H.264 video is
pulled directly from the station on ports 9010/9020, de-framed in pure Python
(vendored `cpd7/`), and transcoded to MJPEG by ffmpeg for Home Assistant.

The CPD7 pipeline (shared ffmpeg encoder, keyframe-aware feed) is adapted from
rtammekivi/hikconnect_intercom (MIT); see THIRD_PARTY_LICENSES.md. It is enabled
only when a camera IP is set in the integration options, since the cloud does
not report the station's LAN address.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_CAMERA_CHANNEL,
    CONF_CAMERA_IP,
    CONF_CAMERA_QUALITY,
    DEFAULT_CAMERA_CHANNEL,
    DEFAULT_CAMERA_QUALITY,
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

_LOGGER = logging.getLogger(__name__)

_SC = b"\x00\x00\x00\x01"
_EOI = b"\xff\xd9"
_FEED_QUEUE_MAX = 240
_LINGER_SEC = 8.0
_FRAME_TIMEOUT = 10.0
_FIRST_FRAME_TIMEOUT = 15.0
_MAX_EMPTY_READS = 40
_MJPEG_BOUNDARY = "frame"


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
    # One camera per device that answered the call endpoint (the door station).
    serials = [s for s, st in coordinator.data.items() if st.call is not None]
    serials = serials or list(coordinator.data)
    async_add_entities(
        NikoCamera(coordinator, serial, ip, channel, quality) for serial in serials
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
    ) -> None:
        NikoAccessEntity.__init__(self, coordinator, serial, "camera")
        Camera.__init__(self)
        self._source = _Cpd7Stream(
            coordinator.hass, coordinator, serial, ip, channel, quality
        )
        self._last: bytes | None = None

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
        seq = 0
        try:
            while True:
                got = await self._source.frame_after(seq, _FRAME_TIMEOUT)
                if got is None:
                    break
                seq, jpeg = got
                self._last = jpeg
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
