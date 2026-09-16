"""Tests for Ocea consumption sensors."""

from __future__ import annotations

import logging
from datetime import date, datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.api import OceaApiError
from custom_components.ocea_smart_building.const import DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaDataUpdateCoordinator,
    _build_meter_data,
    _current_month_payload,
)
from custom_components.ocea_smart_building.sensor import SENSOR_TYPES, OceaMeterSensor


def _coordinator(hass: HomeAssistant, client: Mock) -> OceaDataUpdateCoordinator:
    """Create a coordinator bound to an actual config entry."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    assert coordinator.config_entry is entry
    return coordinator


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


def test_meter_data_builder_aggregates_one_meter_response() -> None:
    """Test meter response parsing is isolated from coordinator I/O."""
    metadata = {
        "pds": {"id": "pds-cold-1", "fluide": "EauFroide"},
        "appareil": {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    }

    assert _build_meter_data(
        metadata,
        {
            "consommations": [
                {"date": "2026-09-01", "valeur": "0,321"},
                {"date": "2026-09-15", "valeur": 0.654},
            ]
        },
    ) == {
        **metadata,
        "value": 0.975,
        "date": "2026-09-15",
    }


def test_current_month_payload_uses_requested_calendar_date() -> None:
    """Test the API payload represents the local calendar month."""
    assert _current_month_payload(date(2026, 9, 16)) == {
        "debut": "2026-09-01T00:00:00.000Z",
        "fin": "2026-09-16T23:59:59.999Z",
        "granularity": "Day",
    }


async def test_coordinator_uses_home_assistant_timezone(
    hass: HomeAssistant,
) -> None:
    """Test current-month boundaries follow Home Assistant's timezone."""
    client = Mock()
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.return_value = {"consommations": []}
    coordinator = _coordinator(hass, client)

    with patch(
        "custom_components.ocea_smart_building.coordinator.dt_util.now",
        return_value=datetime(2026, 9, 16, tzinfo=ZoneInfo("America/Montreal")),
    ):
        await coordinator._async_update_data()

    payload = client.get_pds_consumption.call_args.args[1]
    assert payload["debut"] == "2026-09-01T00:00:00.000Z"


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
    coordinator = _coordinator(hass, client)

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
    coordinator = _coordinator(hass, client)

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


async def test_coordinator_discovers_meters_only_once(
    hass: HomeAssistant,
) -> None:
    """Test PDS metadata is cached while values continue to refresh."""
    client = Mock()
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = [
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    ]
    client.get_pds_consumption.return_value = {
        "consommations": [{"date": "2026-09-15", "valeur": 0.321}],
    }
    coordinator = _coordinator(hass, client)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    client.get_pds.assert_called_once_with()
    client.get_appareils.assert_called_once_with(["pds-cold-1"])
    assert client.get_pds_consumption.call_count == 2


async def test_coordinator_keeps_other_meters_when_one_fails(
    hass: HomeAssistant,
) -> None:
    """Test one PDS failure does not discard other meter data or totals."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "1,14"},
    ]
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"id": "pds-cold-2", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.side_effect = [
        {"consommations": [{"date": "2026-09-15", "valeur": 0.321}]},
        OceaApiError("meter unavailable"),
    ]
    coordinator = _coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data["eau_froide"] == 1.14
    assert set(data["meters"]) == {"pds-cold-1", "pds-cold-2"}
    assert data["meters"]["pds-cold-2"]["value"] is None


async def test_coordinator_does_not_log_meter_details(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test coordinator logs do not contain PDS or serial identifiers."""
    client = Mock()
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        {"id": "pds-secret", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = [
        {"pdsId": "pds-secret", "numeroSerie": "serial-secret"},
    ]
    client.get_pds_consumption.return_value = {"consommations": []}
    coordinator = _coordinator(hass, client)

    with caplog.at_level(logging.DEBUG):
        await coordinator._async_update_data()

    assert "pds-secret" not in caplog.text
    assert "serial-secret" not in caplog.text


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
