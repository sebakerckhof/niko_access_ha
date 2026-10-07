"""Doorbell event entity: fires `ring` when a call starts."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CallStatus
from .coordinator import NikoAccessConfigEntry
from .entity import NikoAccessEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NikoAccessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        NikoDoorbellEvent(coordinator, serial, "doorbell")
        for serial, state in coordinator.data.items()
        if state.call is not None
    )


class NikoDoorbellEvent(NikoAccessEntity, EventEntity):
    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_ring_listener(self._on_ring))

    @callback
    def _on_ring(self, serial: str, call: CallStatus) -> None:
        if serial != self.serial:
            return
        self._trigger_event("ring", {"caller": call.caller} if call.caller else None)
        self.async_write_ha_state()
