"""Door open buttons."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import NikoAccessError
from .const import CONF_LOCK_IDS, DEFAULT_LOCK_IDS, FALLBACK_LOCK_IDS
from .coordinator import NikoAccessConfigEntry, NikoAccessCoordinator
from .entity import NikoAccessEntity

_LOGGER = logging.getLogger(__name__)


def parse_lock_ids(value: str) -> list[int]:
    """'1, 3' -> [1, 3]; invalid parts are ignored."""
    ids = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    return ids


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NikoAccessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    configured = parse_lock_ids(entry.options.get(CONF_LOCK_IDS, DEFAULT_LOCK_IDS))
    # Doors hang off the intercom; if no device answered the call endpoint we
    # cannot tell which one that is, so offer the buttons on all of them.
    serials = [s for s, st in coordinator.data.items() if st.call is not None]
    serials = serials or list(coordinator.data)

    entities = []
    for serial in serials:
        names: dict[int, str] = {}
        try:
            locks = await coordinator.client.list_locks(serial)
            names = {lock.lock_id: lock.name for lock in locks}
            enabled = [lock.lock_id for lock in locks if lock.enabled]
        except NikoAccessError as err:
            _LOGGER.warning("Could not read locks of %s: %s", serial, err)
            enabled = []
        lock_ids = configured or enabled or FALLBACK_LOCK_IDS
        entities.extend(
            NikoDoorButton(coordinator, serial, lock_id, names.get(lock_id))
            for lock_id in lock_ids
        )
    async_add_entities(entities)


class NikoDoorButton(NikoAccessEntity, ButtonEntity):
    _attr_translation_key = "open_door"

    def __init__(
        self,
        coordinator: NikoAccessCoordinator,
        serial: str,
        lock_id: int,
        lock_name: str | None,
    ) -> None:
        super().__init__(coordinator, serial, f"unlock_{lock_id}")
        self.lock_id = lock_id
        self._attr_translation_placeholders = {"door": str(lock_id)}
        self._attr_extra_state_attributes = {"lock_id": lock_id, "lock_name": lock_name}

    async def async_press(self) -> None:
        client = self.coordinator.client
        try:
            await client.unlock(self.serial, self.lock_id)
            return
        except NikoAccessError as err:
            first = err
            _LOGGER.debug("ISAPI unlock failed (%s), trying v3 remote unlock", err)
        try:
            await client.unlock_v3(self.serial, 1, self.lock_id)
        except NikoAccessError as err:
            raise HomeAssistantError(
                f"Unlock of lock {self.lock_id} failed: {first}; fallback: {err}"
            ) from err
