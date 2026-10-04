"""Binary sensor platform for Ocea leak alerts."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DWELLING_ID, CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the dwelling leak alert."""
    coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    dwelling_id = str(
        config_entry.data.get(
            CONF_DWELLING_ID, config_entry.data.get(CONF_LOCAL_ID, "")
        )
    )
    async_add_entities([OceaLeakBinarySensor(coordinator, dwelling_id)])


class OceaLeakBinarySensor(
    CoordinatorEntity[OceaDataUpdateCoordinator], BinarySensorEntity
):
    """Represent Ocea's dwelling-level leak alert."""

    _attr_has_entity_name = True
    _attr_translation_key = "fuite"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(
        self, coordinator: OceaDataUpdateCoordinator, dwelling_id: str
    ) -> None:
        """Initialize the leak alert."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{dwelling_id}_fuite"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, dwelling_id)},
            "name": f"Ocea - Local {dwelling_id}",
            "manufacturer": "Ocea Smart Building",
            "model": "Espace Résident",
        }

    @property
    def is_on(self) -> bool | None:
        """Return whether Ocea currently estimates a leak."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.has_leak
