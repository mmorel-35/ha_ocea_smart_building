"""Binary sensor platform for Ocea Smart Building leak detection."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator
from .models import OceaMeter
from .sensor import _meter_device_info, _meter_entity_unique_id


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up leak binary sensors for discovered water meters."""
    coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    local_id = config_entry.data[CONF_LOCAL_ID]
    meters = (coordinator.data or {}).get("meters", {})

    async_add_entities(
        OceaLeakBinarySensor(coordinator, local_id, reading.meter)
        for reading in meters.values()
    )


class OceaLeakBinarySensor(
    CoordinatorEntity[OceaDataUpdateCoordinator], BinarySensorEntity
):
    """Represent whether Ocea estimates a leak for one water meter."""

    _attr_has_entity_name = True
    _attr_translation_key = "fuite"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(
        self,
        coordinator: OceaDataUpdateCoordinator,
        local_id: str,
        meter: OceaMeter,
    ) -> None:
        """Initialize a leak binary sensor."""
        super().__init__(coordinator)
        self._meter = meter
        self._attr_unique_id = _meter_entity_unique_id(
            local_id, meter, "fuite_detectee"
        )
        self._attr_device_info = _meter_device_info(local_id, meter)

    @property
    def _reading(self):
        """Return the current reading for this meter."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("meters", {}).get(self._meter.identifier)

    @property
    def available(self) -> bool:
        """Return whether a leak estimate is currently available."""
        reading = self._reading
        return super().available and reading is not None and reading.estimated_leak is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether the meter reports an estimated leak."""
        reading = self._reading
        if reading is None or reading.estimated_leak is None:
            return None
        return float(str(reading.estimated_leak).replace(",", ".")) > 0
