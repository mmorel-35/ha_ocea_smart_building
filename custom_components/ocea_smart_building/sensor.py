"""Sensor platform for Ocea Smart Building water consumption."""

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator
from .models import OceaMeter
from .water_meter_service import WATER_FLUID_SENSOR_KEYS


@dataclass(frozen=True, kw_only=True)
class OceaSensorEntityDescription(SensorEntityDescription):
    """Describe an Ocea sensor entity."""

    data_key: str


def _local_device_info(local_id: str) -> DeviceInfo:
    """Build the Home Assistant device descriptor for dwelling totals."""
    return DeviceInfo(
        identifiers={(DOMAIN, local_id)},
        manufacturer="Ocea Smart Building",
        translation_key="local",
        translation_placeholders={"local_id": local_id},
    )


def _meter_device_info(local_id: str, meter: OceaMeter) -> DeviceInfo:
    """Build the Home Assistant device descriptor for one meter."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{local_id}_{meter.identifier}")},
        manufacturer="Ocea Smart Building",
        serial_number=meter.serial_number,
        translation_key="meter",
        translation_placeholders={"meter_identity": meter.display_name},
    )


def _local_entity_unique_id(local_id: str, key: str) -> str:
    """Build the established unique ID for a dwelling-level sensor."""
    return f"{DOMAIN}_{local_id}_{key}"


def _meter_entity_unique_id(local_id: str, meter: OceaMeter, key: str) -> str:
    """Build the established unique ID for an individual meter sensor."""
    return f"{DOMAIN}_{local_id}_{meter.identifier}_{key}"


SENSOR_TYPES: tuple[OceaSensorEntityDescription, ...] = (
    OceaSensorEntityDescription(
        key="eau_froide",
        data_key="eau_froide",
        translation_key="eau_froide",
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
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:radiator",
        suggested_display_precision=2,
    ),
    OceaSensorEntityDescription(
        key="fuite",
        data_key="fuite",
        translation_key="fuite",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:pipe-leak",
        suggested_display_precision=3,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ocea Smart Building sensors from a config entry."""
    coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    local_id = config_entry.data[CONF_LOCAL_ID]
    descriptions = {description.key: description for description in SENSOR_TYPES}

    entities: list[OceaWaterSensor] = []
    for description in SENSOR_TYPES:
        if description.key == "fuite":
            continue
        if coordinator.data and description.data_key in coordinator.data:
            entities.append(
                OceaWaterSensor(
                    coordinator=coordinator,
                    description=description,
                    local_id=local_id,
                )
            )

    if coordinator.data:
        fluid_descriptions = {
            fluide: descriptions[key]
            for fluide, key in WATER_FLUID_SENSOR_KEYS.items()
        }
        for meter in coordinator.data.get("meters", {}).values():
            pds = meter.meter.pds
            description = fluid_descriptions.get(pds.fluid)
            if description is None:
                continue
            entities.append(
                OceaMeterSensor(
                    coordinator=coordinator,
                    description=description,
                    local_id=local_id,
                    meter=meter.meter,
                )
            )
            if meter.estimated_leak is not None:
                entities.append(
                    OceaMeterSensor(
                        coordinator=coordinator,
                        description=descriptions["fuite"],
                        local_id=local_id,
                        meter=meter.meter,
                    )
                )

    async_add_entities(entities, update_before_add=True)

    leak_entity_keys = {
        meter.meter.identifier
        for meter in (coordinator.data or {}).get("meters", {}).values()
        if meter.estimated_leak is not None
    }

    @callback
    def _async_add_new_leak_entities() -> None:
        """Add leak entities when Ocea starts reporting an estimate."""
        new_entities: list[OceaMeterSensor] = []
        for reading in (coordinator.data or {}).get("meters", {}).values():
            if reading.estimated_leak is None:
                continue
            meter = reading.meter
            if meter.identifier in leak_entity_keys:
                continue
            leak_entity_keys.add(meter.identifier)
            new_entities.append(
                OceaMeterSensor(
                    coordinator=coordinator,
                    description=descriptions["fuite"],
                    local_id=local_id,
                    meter=meter,
                )
            )
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    coordinator.async_add_listener(_async_add_new_leak_entities)


class OceaWaterSensor(
    CoordinatorEntity[OceaDataUpdateCoordinator], SensorEntity
):
    """Representation of an Ocea consumption sensor."""

    entity_description: OceaSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OceaDataUpdateCoordinator,
        description: OceaSensorEntityDescription,
        local_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._local_id = local_id
        self._attr_unique_id = _local_entity_unique_id(local_id, description.key)
        self._attr_device_info = _local_device_info(local_id)

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.data_key)


class OceaMeterSensor(OceaWaterSensor):
    """Representation of one PDS consumption sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OceaDataUpdateCoordinator,
        description: OceaSensorEntityDescription,
        local_id: str,
        meter: OceaMeter,
    ) -> None:
        """Initialize an individual meter sensor."""
        super().__init__(coordinator, description, local_id)
        self._meter = meter
        self._attr_unique_id = _meter_entity_unique_id(
            local_id, meter, description.key
        )
        self._attr_device_info = _meter_device_info(local_id, meter)

    @property
    def native_value(self) -> float | None:
        """Return this meter's current-month consumption."""
        if self.coordinator.data is None:
            return None
        reading = self.coordinator.data.get("meters", {}).get(self._meter.identifier)
        if reading is None:
            return None
        return reading.estimated_leak if self.entity_description.key == "fuite" else reading.value
