"""Niko Access Control (2-wire) via the Niko/Hik-Connect cloud."""

from __future__ import annotations

from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    NikoAccessAuthError,
    NikoAccessClient,
    NikoAccessError,
    NikoAccessVerificationRequired,
)
from .const import (
    CONF_API_URL,
    CONF_FEATURE_CODE,
    CONF_PASSWORD_MD5,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
)
from .coordinator import NikoAccessConfigEntry, NikoAccessCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: NikoAccessConfigEntry) -> bool:
    client = NikoAccessClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD_MD5],
        entry.data[CONF_FEATURE_CODE],
        api_url=entry.data.get(CONF_API_URL),
    )
    try:
        await client.login()
    except (NikoAccessAuthError, NikoAccessVerificationRequired) as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except NikoAccessError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = NikoAccessCoordinator(
        hass,
        entry,
        client,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NikoAccessConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
