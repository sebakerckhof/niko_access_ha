"""Base entity for Niko Access."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import DeviceState, NikoAccessCoordinator


class NikoAccessEntity(CoordinatorEntity[NikoAccessCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: NikoAccessCoordinator, serial: str, key: str) -> None:
        super().__init__(coordinator)
        self.serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        device = coordinator.data[serial].device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=device.device_type or None,
            serial_number=serial,
            sw_version=device.version,
        )

    @property
    def state_data(self) -> DeviceState | None:
        return (self.coordinator.data or {}).get(self.serial)

    @property
    def available(self) -> bool:
        return super().available and self.state_data is not None
