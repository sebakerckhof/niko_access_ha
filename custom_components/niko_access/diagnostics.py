"""Diagnostics: dump raw cloud replies to help map unknown device behaviour."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant

from .api import NikoAccessError
from .const import CONF_FEATURE_CODE, CONF_PASSWORD_MD5
from .coordinator import NikoAccessConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD_MD5,
    CONF_FEATURE_CODE,
    "fullSerial",
    "netIp",
    "localIp",
    "wanIp",
    "casIp",
    "mac",
    "macAddress",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NikoAccessConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    client = coordinator.client
    devices: dict[str, Any] = {}
    for serial, state in (coordinator.data or {}).items():
        info: dict[str, Any] = {
            "device": state.device.raw,
            "call": state.call.raw if state.call else None,
        }
        # Live ISAPI probes: shows which lockIds exist and what the caller
        # endpoint returns on this firmware.
        for name, probe in (("locks", client.get_locks), ("caller_info", client.get_caller_info)):
            try:
                info[name] = await probe(serial)
            except NikoAccessError as err:
                info[name] = f"error: {err}"
        devices[serial] = info
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "api_url": client.api_url,
            "devices": devices,
        },
        TO_REDACT,
    )
