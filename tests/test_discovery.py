"""SADP discovery parsing and serial matching."""

from __future__ import annotations

import custom_components.niko_access.discovery as disc

_PROBE_MATCH = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b"<ProbeMatch><DeviceSN>510-310010120000000RRX12345678CLU</DeviceSN>"
    b"<IPv4Address>10.6.2.6</IPv4Address><DeviceDescription>510-31001</DeviceDescription>"
    b"<CommandPort>8000</CommandPort></ProbeMatch>"
)


def test_protocol_parses_probe_match():
    proto = disc._SadpProtocol()
    proto.datagram_received(_PROBE_MATCH, ("10.6.2.6", 37020))
    assert proto.devices == {"510-310010120000000RRX12345678CLU": "10.6.2.6"}


def test_protocol_ignores_garbage():
    proto = disc._SadpProtocol()
    proto.datagram_received(b"not xml", ("1.2.3.4", 37020))
    assert proto.devices == {}


async def test_find_ip_matches_cloud_serial_substring(monkeypatch):
    async def fake_discover(duration=0):
        return {"510-310010120000000RRX12345678CLU": "10.6.2.6", "OTHER": "10.6.2.3"}

    monkeypatch.setattr(disc, "async_discover", fake_discover)
    assert await disc.async_find_ip("X12345678") == "10.6.2.6"
    assert await disc.async_find_ip("NOPE") is None


async def test_find_ip_handles_oserror(monkeypatch):
    async def boom(duration=0):
        raise OSError("no network")

    monkeypatch.setattr(disc, "async_discover", boom)
    assert await disc.async_find_ip("X12345678") is None
