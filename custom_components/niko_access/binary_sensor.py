"""Binary sensors: doorbell ringing and cloud connectivity."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import NikoAccessConfigEntry
from .entity import NikoAccessEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NikoAccessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for serial, state in coordinator.data.items():
        entities.append(NikoOnlineSensor(coordinator, serial, "online"))
        if state.call is not None:
            entities.append(NikoRingingSensor(coordinator, serial, "ringing"))
    async_add_entities(entities)


class NikoRingingSensor(NikoAccessEntity, BinarySensorEntity):
    _attr_translation_key = "ringing"

    @property
    def is_on(self) -> bool | None:
        state = self.state_data
        if state is None or state.call is None:
            return None
        return state.call.ringing


class NikoOnlineSensor(NikoAccessEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool | None:
        state = self.state_data
        return None if state is None else state.device.online
