"""Sensor platform for Ocea dwelling and physical-meter consumption."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DWELLING_ID, CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator
from .pyocea import MeterData


@dataclass(frozen=True, kw_only=True)
class OceaSensorEntityDescription(SensorEntityDescription):
    """Describe one dwelling-level Ocea sensor."""

    data_key: str


SENSOR_TYPES: tuple[OceaSensorEntityDescription, ...] = (
    OceaSensorEntityDescription(
        key="eau_froide",
        data_key="eau_froide",
        translation_key="eau_froide",
        name="Eau froide",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:water",
        suggested_display_precision=2,
    ),
    OceaSensorEntityDescription(
        key="eau_chaude",
        data_key="eau_chaude",
        translation_key="eau_chaude",
        name="Eau chaude",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:water-thermometer",
        suggested_display_precision=2,
    ),
    OceaSensorEntityDescription(
        key="cetc",
        data_key="cetc",
        translation_key="cetc",
        name="Chauffage thermique",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:radiator",
        suggested_display_precision=2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up global and physical-meter sensors for one dwelling."""
    coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    dwelling_id = str(
        config_entry.data.get(
            CONF_DWELLING_ID, config_entry.data.get(CONF_LOCAL_ID, "")
        )
    )
    data = coordinator.data
    if data is None:
        return

    entities: list[SensorEntity] = [
        OceaWaterSensor(coordinator, description, dwelling_id)
        for description in SENSOR_TYPES
        if (
            description.data_key != "cetc"
            or data.heating_total is not None
        )
    ]
    entities.append(OceaLeakEstimateSensor(coordinator, dwelling_id))
    entities.extend(
        OceaMeterSensor(coordinator, dwelling_id, meter)
        for meter in data.dwelling.meters
    )
    async_add_entities(entities, update_before_add=True)


class OceaWaterSensor(
    CoordinatorEntity[OceaDataUpdateCoordinator], SensorEntity
):
    """Represent an established dwelling-level Ocea total."""

    entity_description: OceaSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OceaDataUpdateCoordinator,
        description: OceaSensorEntityDescription,
        dwelling_id: str,
    ) -> None:
        """Initialize the sensor without changing its established unique ID."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{dwelling_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, dwelling_id)},
            "name": f"Ocea - Local {dwelling_id}",
            "manufacturer": "Ocea Smart Building",
            "model": "Espace Résident",
        }

    @property
    def native_value(self) -> float | None:
        """Return the rounded dwelling-level total."""
        data = self.coordinator.data
        if data is None:
            return None
        if self.entity_description.data_key == "eau_froide":
            return data.cold_water_total
        if self.entity_description.data_key == "eau_chaude":
            return data.hot_water_total
        return data.heating_total


class OceaLeakEstimateSensor(
    CoordinatorEntity[OceaDataUpdateCoordinator], SensorEntity
):
    """Expose Ocea's aggregated estimated leak volume for the dwelling."""

    _attr_has_entity_name = True
    _attr_translation_key = "fuite_estimee"
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:water-alert"

    def __init__(
        self, coordinator: OceaDataUpdateCoordinator, dwelling_id: str
    ) -> None:
        """Initialize the estimated-leak sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{dwelling_id}_fuite_estimee"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, dwelling_id)},
            "name": f"Ocea - Local {dwelling_id}",
            "manufacturer": "Ocea Smart Building",
            "model": "Espace Résident",
        }

    @property
    def native_value(self) -> float | None:
        """Return the aggregated estimated leak volume."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.leak_estimate


class OceaMeterSensor(CoordinatorEntity[OceaDataUpdateCoordinator], SensorEntity):
    """Represent consumption reported by one physical meter."""

    _attr_has_entity_name = True
    _attr_translation_key = "meter_consumption"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: OceaDataUpdateCoordinator,
        dwelling_id: str,
        meter: MeterData,
    ) -> None:
        """Initialize an individual meter sensor."""
        super().__init__(coordinator)
        self._meter_id = meter.meter_id
        self._attr_unique_id = (
            f"{DOMAIN}_{dwelling_id}_{meter.meter_id}_consumption"
        )
        self._attr_device_class = self._device_class(meter.meter_type)
        self._attr_native_unit_of_measurement = self._unit(meter.meter_type)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{dwelling_id}_{meter.meter_id}")},
            via_device=(DOMAIN, dwelling_id),
            name=meter.serial_number or f"Compteur {meter.meter_id}",
            manufacturer="Ocea Smart Building",
            model=meter.meter_type,
            serial_number=meter.serial_number or None,
        )

    @staticmethod
    def _device_class(meter_type: str) -> SensorDeviceClass | None:
        """Map an Ocea meter fluid to its Home Assistant device class."""
        fluid = meter_type.lower()
        if "eau" in fluid or "water" in fluid:
            return SensorDeviceClass.WATER
        if "cetc" in fluid or "chauffage" in fluid or "heating" in fluid:
            return SensorDeviceClass.ENERGY
        return None

    @staticmethod
    def _unit(meter_type: str) -> str | None:
        """Map an Ocea meter fluid to a unit."""
        fluid = meter_type.lower()
        if "eau" in fluid or "water" in fluid:
            return UnitOfVolume.CUBIC_METERS
        if "cetc" in fluid or "chauffage" in fluid or "heating" in fluid:
            return UnitOfEnergy.KILO_WATT_HOUR
        return None

    @property
    def native_value(self) -> float | None:
        """Return the current-month usage for this meter."""
        data = self.coordinator.data
        if data is None:
            return None
        return next(
            (
                meter.current_value
                for meter in data.dwelling.meters
                if meter.meter_id == self._meter_id
            ),
            None,
        )
