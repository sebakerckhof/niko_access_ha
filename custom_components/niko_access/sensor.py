"""Call state sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NikoAccessConfigEntry
from .entity import NikoAccessEntity

CALL_STATES = ["idle", "ringing", "in_call", "unknown"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NikoAccessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        NikoCallStateSensor(coordinator, serial, "call_state")
        for serial, state in coordinator.data.items()
        if state.call is not None
    )


class NikoCallStateSensor(NikoAccessEntity, SensorEntity):
    _attr_translation_key = "call_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALL_STATES

    @property
    def native_value(self) -> str | None:
        state = self.state_data
        if state is None or state.call is None:
            return None
        return state.call.state

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        state = self.state_data
        if state is None or state.call is None or not state.call.caller:
            return None
        return {"caller": state.call.caller}
