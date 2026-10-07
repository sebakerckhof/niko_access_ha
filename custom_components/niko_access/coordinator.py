"""Polling coordinator for Niko Access."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CallStatus,
    NikoAccessAuthError,
    NikoAccessClient,
    NikoAccessError,
    NikoAccessVerificationRequired,
    NikoDevice,
)
from .const import DEVICE_REFRESH_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type NikoAccessConfigEntry = ConfigEntry[NikoAccessCoordinator]


@dataclass
class DeviceState:
    device: NikoDevice
    call: CallStatus | None


class NikoAccessCoordinator(DataUpdateCoordinator[dict[str, DeviceState]]):
    """Polls the cloud for device list and doorbell call state."""

    config_entry: NikoAccessConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NikoAccessConfigEntry,
        client: NikoAccessClient,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self._devices: list[NikoDevice] = []
        self._devices_at = None
        # Serials whose call endpoint is unsupported; stop polling them.
        self._no_call: set[str] = set()
        self._ring_listeners: list = []

    def async_add_ring_listener(self, callback) -> callable:
        """Register `callback(serial, CallStatus)` for idle->ringing transitions."""
        self._ring_listeners.append(callback)
        return lambda: self._ring_listeners.remove(callback)

    async def _async_update_data(self) -> dict[str, DeviceState]:
        try:
            now = dt_util.utcnow()
            if not self._devices or now - self._devices_at > DEVICE_REFRESH_INTERVAL:
                self._devices = await self.client.get_devices()
                self._devices_at = now

            result: dict[str, DeviceState] = {}
            for device in self._devices:
                call = None
                if device.serial not in self._no_call:
                    call = await self._poll_call(device)
                result[device.serial] = DeviceState(device, call)
        except (NikoAccessAuthError, NikoAccessVerificationRequired) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NikoAccessError as err:
            raise UpdateFailed(str(err)) from err

        self._fire_ring_transitions(result)
        return result

    async def _poll_call(self, device: NikoDevice) -> CallStatus | None:
        try:
            return await self.client.get_call_status(device.serial)
        except (NikoAccessAuthError, NikoAccessVerificationRequired):
            raise
        except NikoAccessError as err:
            # Non-intercom devices (or unsupported firmware) reject this call.
            # Keep the device but stop polling its call state if it is online.
            if device.online:
                _LOGGER.info(
                    "Call status not available for %s (%s); not polling it further",
                    device.serial,
                    err,
                )
                self._no_call.add(device.serial)
            return None

    def _fire_ring_transitions(self, new: dict[str, DeviceState]) -> None:
        old = self.data or {}
        for serial, state in new.items():
            if not state.call or not state.call.ringing:
                continue
            prev = old.get(serial)
            if prev and prev.call and prev.call.ringing:
                continue
            for listener in list(self._ring_listeners):
                listener(serial, state.call)
