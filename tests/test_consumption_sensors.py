"""Tests for Ocea dwelling and meter sensors."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.binary_sensor import (
    OceaLeakBinarySensor,
)
from custom_components.ocea_smart_building.const import DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaDataUpdateCoordinator,
)
from custom_components.ocea_smart_building.pyocea import (
    ConsumptionRecord,
    DwellingData,
    MeterData,
    OccupancyData,
    OceaAPIError,
)
from custom_components.ocea_smart_building.sensor import (
    SENSOR_TYPES,
    OceaMeterSensor,
    OceaWaterSensor,
)


def _data() -> OccupancyData:
    """Create typed dwelling data for sensor and coordinator checks."""
    meter = MeterData(
        meter_id="pds-cold-1",
        serial_number="SERIAL-1",
        meter_type="EauFroide",
        current_value=1.235,
        history=[
            ConsumptionRecord(
                date=__import__("datetime").datetime.fromisoformat(
                    "2026-09-01T00:00:00+02:00"
                ),
                value=0.321,
            )
        ],
    )
    return OccupancyData(
        occupancy_id="occupancy-1",
        is_active=True,
        dwelling=DwellingData(
            dwelling_id="local-123",
            address="1 rue Exemple",
            meters=[meter],
        ),
        cold_water_total=12.34,
        hot_water_total=5.67,
        heating_total=89.01,
        has_leak=True,
        leak_estimate=0.015,
    )


def test_established_global_sensor_descriptions_are_unchanged() -> None:
    """Main total sensor keys and Energy dashboard metadata stay stable."""
    sensors = {description.key: description for description in SENSOR_TYPES}
    assert set(sensors) == {"eau_froide", "eau_chaude", "cetc"}

    cold = sensors["eau_froide"]
    assert cold.native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    assert cold.device_class is SensorDeviceClass.WATER
    assert cold.state_class is SensorStateClass.TOTAL_INCREASING

    hot = sensors["eau_chaude"]
    assert hot.native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    assert hot.state_class is SensorStateClass.TOTAL_INCREASING

    heating = sensors["cetc"]
    assert heating.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert heating.device_class is SensorDeviceClass.ENERGY
    assert heating.state_class is SensorStateClass.TOTAL_INCREASING


async def test_coordinator_returns_selected_dwelling_data(
    hass: HomeAssistant,
) -> None:
    """Coordinator delegates to the async client with the saved dwelling ID."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.get_dwelling_data.return_value = _data()
    coordinator = OceaDataUpdateCoordinator(hass, entry, client, "local-123")

    result = await coordinator._async_update_data()

    client.get_dwelling_data.assert_awaited_once_with("local-123")
    assert result.cold_water_total == 12.34
    assert result.dwelling.meters[0].current_value == 1.235


async def test_entity_states_and_existing_global_unique_id_are_stable(
    hass: HomeAssistant,
) -> None:
    """Global entity IDs are preserved and new meter/leak entities expose data."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = OceaDataUpdateCoordinator(hass, entry, AsyncMock(), "local-123")
    coordinator.data = _data()
    cold = OceaWaterSensor(
        coordinator,
        next(item for item in SENSOR_TYPES if item.key == "eau_froide"),
        "local-123",
    )
    meter = OceaMeterSensor(
        coordinator, "local-123", coordinator.data.dwelling.meters[0]
    )
    leak = OceaLeakBinarySensor(coordinator, "local-123")

    assert cold._attr_unique_id == f"{DOMAIN}_local-123_eau_froide"
    assert cold.native_value == 12.34
    assert meter.native_value == 1.235
    assert leak.is_on is True


async def test_coordinator_starts_native_reauth_after_http_403(
    hass: HomeAssistant,
) -> None:
    """A forbidden dwelling starts one native reauth and raises auth failure."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entry.async_start_reauth = Mock()
    client = AsyncMock()
    client.get_dwelling_data.side_effect = OceaAPIError(
        "forbidden", status_code=403
    )
    coordinator = OceaDataUpdateCoordinator(hass, entry, client, "local-123")

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    entry.async_start_reauth.assert_called_once_with(hass)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    entry.async_start_reauth.assert_called_once()
