"""Tests for Ocea consumption sensors."""

from __future__ import annotations

from unittest.mock import Mock

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant

from custom_components.ocea_smart_building.coordinator import OceaDataUpdateCoordinator
from custom_components.ocea_smart_building.sensor import SENSOR_TYPES, OceaMeterSensor


def test_sensor_types_include_cetc_heating_energy_sensor() -> None:
    """Test CETC heating consumption is exposed as an energy sensor."""
    sensors = {description.key: description for description in SENSOR_TYPES}

    assert set(sensors) == {"eau_froide", "eau_chaude", "cetc"}
    assert sensors["eau_froide"].native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    assert sensors["eau_chaude"].native_unit_of_measurement == UnitOfVolume.CUBIC_METERS

    cetc = sensors["cetc"]
    assert cetc.data_key == "cetc"
    assert cetc.translation_key == "cetc"
    assert cetc.name == "Chauffage thermique"
    assert cetc.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert cetc.device_class is SensorDeviceClass.ENERGY
    assert cetc.state_class is SensorStateClass.TOTAL_INCREASING


async def test_coordinator_normalizes_cetc_consumption(hass: HomeAssistant) -> None:
    """Test raw CETC consumption is normalized for the heating sensor."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "12,34"},
        {"fluide": "EauChaude", "valeur": "5,67"},
        {"fluide": "CETC", "valeur": "89,01"},
    ]
    client.get_pds.return_value = []
    client.get_appareils.return_value = []
    coordinator = OceaDataUpdateCoordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data == {
        "eau_froide": 12.34,
        "eau_chaude": 5.67,
        "cetc": 89.01,
    }


async def test_coordinator_keeps_consumption_separate_per_pds(
    hass: HomeAssistant,
) -> None:
    """Test two points of the same fluid produce independent meter data."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "1,14"},
    ]
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide", "emplacement": "Cuisine"},
        {"id": "pds-cold-2", "fluide": "EauFroide", "emplacement": "Salle de bain"},
    ]
    client.get_appareils.return_value = [
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
        {"pdsId": "pds-cold-2", "numeroSerie": "SERIAL-2"},
    ]
    client.get_pds_consumption.side_effect = [
        {"unite": "m3", "consommations": [{"date": "2026-09-15", "valeur": 0.321}]},
        {"unite": "m3", "consommations": [{"date": "2026-09-15", "valeur": 0.654}]},
    ]
    coordinator = OceaDataUpdateCoordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data["eau_froide"] == 1.14
    assert data["meters"] == {
        "pds-cold-1": {
            "pds": {
                "id": "pds-cold-1",
                "fluide": "EauFroide",
                "emplacement": "Cuisine",
            },
            "appareil": {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
            "value": 0.321,
            "date": "2026-09-15",
        },
        "pds-cold-2": {
            "pds": {
                "id": "pds-cold-2",
                "fluide": "EauFroide",
                "emplacement": "Salle de bain",
            },
            "appareil": {"pdsId": "pds-cold-2", "numeroSerie": "SERIAL-2"},
            "value": 0.654,
            "date": "2026-09-15",
        },
    }


def test_meter_sensor_uses_pds_id_and_serial_for_identity() -> None:
    """Test individual meters get stable identities and readable names."""
    coordinator = Mock()
    description = next(
        description
        for description in SENSOR_TYPES
        if description.key == "eau_froide"
    )
    sensor = OceaMeterSensor(
        coordinator=coordinator,
        description=description,
        local_id="local-123",
        pds_id="pds-cold-1",
        pds={"id": "pds-cold-1", "fluide": "EauFroide"},
        appareil={"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    )

    assert sensor.unique_id == "ocea_smart_building_local-123_pds-cold-1_eau_froide"
    assert sensor.name == "Eau froide (SERIAL-1, PDS pds-cold-1)"
    assert sensor.device_info["identifiers"] == {
        ("ocea_smart_building", "local-123_pds-cold-1")
    }
