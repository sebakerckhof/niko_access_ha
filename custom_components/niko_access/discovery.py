"""SADP discovery — find a door station's LAN IP by serial, with no admin access.

Hikvision devices answer a SADP inquiry multicast on UDP 239.255.255.250:37020
with an XML <ProbeMatch> carrying their IPv4 address and serial. The cloud does
not report the station's LAN IP, so this fills it in (and can track DHCP changes).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import uuid
import xml.etree.ElementTree as ET

_LOGGER = logging.getLogger(__name__)

SADP_MCAST = "239.255.255.250"
SADP_PORT = 37020
_LISTEN = 4.0


def _probe() -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<Probe><Uuid>{uuid.uuid4()}</Uuid><Types>inquiry</Types></Probe>"
    ).encode()


class _SadpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.devices: dict[str, str] = {}  # full serial -> IPv4

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            root = ET.fromstring(data.decode("utf-8", "replace"))
        except ET.ParseError:
            return
        fields = {child.tag: child.text for child in root}
        serial = fields.get("DeviceSN")
        ip = fields.get("IPv4Address")
        if serial and ip:
            self.devices[serial] = ip


async def async_discover(duration: float = _LISTEN) -> dict[str, str]:
    """Return {full_serial: ipv4} for every Hikvision device that answers."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.bind(("", 0))
    transport, protocol = await loop.create_datagram_endpoint(
        _SadpProtocol, sock=sock
    )
    try:
        transport.sendto(_probe(), (SADP_MCAST, SADP_PORT))
        await asyncio.sleep(duration)
        return dict(protocol.devices)
    finally:
        transport.close()


async def async_find_ip(serial: str, duration: float = _LISTEN) -> str | None:
    """LAN IP of the device whose (full) serial contains `serial`, or None.

    The cloud serial (e.g. ``X12345678``) is a substring of the SADP serial
    (e.g. ``510-310010...RRX12345678CLU``).
    """
    try:
        devices = await async_discover(duration)
    except OSError as err:
        _LOGGER.debug("SADP discovery failed: %s", err)
        return None
    for full_serial, ip in devices.items():
        if serial and serial in full_serial:
            return ip
    return None

