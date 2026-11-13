"""Tests for dated Ocea external statistics."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaData,
    OceaDataUpdateCoordinator,
    _build_meter_data,
)
from custom_components.ocea_smart_building.history import (
    OceaHistorySynchronizer,
    _daily_statistics,
    _history_id,
    _history_start,
    _occupation_start,
    _parse_daily_response,
    history_store,
)
from custom_components.ocea_smart_building.ocea import (
    OceaApiClient,
    OceaApiError,
    OceaMeter,
)


def _meter(installation_date: str | None = "2024-01-03T00:00:00Z") -> OceaMeter:
    return OceaMeter.from_api(
        {"id": "pds-1", "fluide": "EauFroide"},
        {"datePose": installation_date, "numeroSerie": "example"},
    )


def test_occupation_and_meter_dates_are_distinct() -> None:
    resident: dict[str, object] = {
        "occupations": [
            {"logementId": "other", "dateDebut": "2022-01-01", "dateFin": None},
            {"logementId": "local", "dateDebut": "2024-01-01T00:00:00Z", "dateFin": None},
        ]
    }
    start = _occupation_start(resident, "local", date(2024, 3, 1))
    assert start == date(2024, 1, 1)
    assert _history_start(start, _meter()) == date(2024, 1, 3)
    assert _history_start(date(2024, 1, 4), _meter()) == date(2024, 1, 4)
    with pytest.raises(OceaApiError):
        _history_start(start, _meter(None))


@pytest.mark.parametrize(
    "response",
    [
        {"unite": "m3", "consommations": [{"date": "2024-01-03"}]},
        {"unite": "litres", "consommations": []},
        {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": -1}]},
        {
            "unite": "m3",
            "consommations": [
                {"date": "2024-01-03", "valeur": 1},
                {"date": "2024-01-03", "valeur": 1},
            ],
        },
    ],
)
def test_reject_invalid_history_response(response: dict[str, object]) -> None:
    with pytest.raises(OceaApiError):
        _parse_daily_response(response)


def test_daily_statistics_preserve_local_day_across_dst(hass: HomeAssistant) -> None:
    hass.config.time_zone = "Europe/Paris"
    records = _daily_statistics(
        hass,
        date(2024, 3, 30),
        {date(2024, 3, 30): Decimal("0.1"), date(2024, 3, 31): Decimal("0.2")},
    )
    assert records[1]["start"] == datetime(2024, 3, 30, 22, tzinfo=UTC)
    assert records[2]["start"] == datetime(2024, 3, 31, 21, tzinfo=UTC)
    assert records[2]["sum"] == pytest.approx(0.3)


@pytest.mark.usefixtures("real_recorder")
async def test_backfill_restart_and_correction_update_external_statistics(
    hass: HomeAssistant,
) -> None:
    """Keep recent readings correct when earlier chunks arrive and after restart."""
    hass.config.time_zone = "UTC"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = {
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01", "dateFin": None}]
    }
    client.get_pds_consumption.side_effect = [
        {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": 0.1}]},
        {"unite": "m3", "consommations": [{"date": "2024-02-02", "valeur": 0.2}]},
        {"unite": "m3", "consommations": [{"date": "2024-02-29", "valeur": 0.05}]},
    ]
    meter = _meter()
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-01", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with patch(
        "custom_components.ocea_smart_building.history.dt_util.now",
        side_effect=[
            datetime(2024, 3, 2, hour, tzinfo=UTC) for hour in (0, 5, 10)
        ],
    ):
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)
        coordinator.async_set_updated_data(
            OceaData(
                totals={},
                meters={
                    meter.identifier: _build_meter_data(
                        meter,
                        {"unite": "m3", "consommations": [
                            {"date": "2024-03-01", "valeur": 0.4}
                        ]},
                    )
                },
            )
        )
        await synchronizer._async_sync()
        await async_recorder_block_till_done(hass)

    calls = client.get_pds_consumption.call_args_list
    assert calls[0].args[1]["debut"].startswith("2024-01-03")
    assert calls[0].args[1]["fin"].startswith("2024-02-01")
    assert calls[1].args[1]["debut"].startswith("2024-02-02")
    assert calls[2].args[1]["debut"].startswith("2024-02-24")
    assert calls[2].args[1]["fin"].startswith("2024-02-29")
    assert synchronizer._generation is not None
    key = _history_id(
        "local", meter, date(2024, 1, 3), "UTC", synchronizer._generation, entry.entry_id
    )
    statistic_id = f"{DOMAIN}:water_{key}"
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum", "state"},
    )
    assert [row["sum"] for row in rows[statistic_id]] == pytest.approx(
        [0, 0.1, 0.3, 0.35, 0.75]
    )
    daily = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2024, 1, 1, tzinfo=UTC),
        None,
        {statistic_id},
        "day",
        None,
        {"change"},
    )
    assert [row["change"] for row in daily[statistic_id]] == pytest.approx(
        [0.1, 0.2, 0.05, 0.4]
    )


async def test_failed_history_is_retried_without_blocking_recent_data(
    hass: HomeAssistant,
) -> None:
    """Keep the backfill cursor and publish recent values after a period fails."""
    hass.config.time_zone = "UTC"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = {
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01", "dateFin": None}]
    }
    client.get_pds_consumption.side_effect = [
        OceaApiError("error"),
        {"unite": "m3", "consommations": [{"date": "2024-01-03", "valeur": 0.2}]},
    ]
    meter = _meter()
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            side_effect=[
                datetime(2024, 3, 12, hour, tzinfo=UTC) for hour in (0, 5)
            ],
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
        await synchronizer._async_sync()

    assert client.get_pds_consumption.call_args_list[0] == client.get_pds_consumption.call_args_list[1]
    assert [row["sum"] for row in add.call_args_list[0].args[2]] == pytest.approx([0, 0.3])
    assert [row["sum"] for row in add.call_args_list[1].args[2]] == pytest.approx([0.2, 0.5])


async def test_current_month_backfill_reuses_coordinator_readings(hass: HomeAssistant) -> None:
    """Do not repeat a current-month request already made by the coordinator."""
    hass.config.time_zone = "UTC"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = {
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01", "dateFin": None}]
    }
    meter = _meter("2024-03-01")
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
        await synchronizer._async_sync()
    client.get_pds_consumption.assert_not_awaited()
    add.assert_called_once()


async def test_invalid_meter_does_not_block_other_meters(hass: HomeAssistant) -> None:
    """A missing installation date must not prevent other PDS from syncing."""
    hass.config.time_zone = "UTC"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = {
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01", "dateFin": None}]
    }
    bad_meter = _meter(None)
    good_meter = OceaMeter.from_api(
        {"id": "pds-2", "fluide": "EauChaude"}, {"datePose": "2024-03-01"}
    )
    readings = {
        meter.identifier: _build_meter_data(
            meter, {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]}
        )
        for meter in (bad_meter, good_meter)
    }
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(OceaData(totals={}, meters=readings))
    synchronizer = OceaHistorySynchronizer(hass, entry, client, coordinator)
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await synchronizer._async_sync()
    add.assert_called_once()
    assert add.call_args.args[1]["name"].startswith("Ocea EauChaude")


async def test_missing_cache_starts_a_new_statistics_series(hass: HomeAssistant) -> None:
    """Losing the cache must not overwrite previously imported statistics."""
    hass.config.time_zone = "UTC"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = {
        "occupations": [{"logementId": "local", "dateDebut": "2024-03-01", "dateFin": None}]
    }
    meter = _meter("2024-03-01")
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            return_value=datetime(2024, 3, 12, tzinfo=UTC),
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics") as add,
    ):
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        await history_store(hass, entry).async_remove()
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
    assert add.call_args_list[0].args[1]["statistic_id"] != add.call_args_list[1].args[1]["statistic_id"]


async def test_backfill_request_limit_survives_restart(hass: HomeAssistant) -> None:
    """Manual refreshes and restarts do not bypass the five-hour request limit."""
    hass.config.time_zone = "UTC"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local"})
    entry.add_to_hass(hass)
    client = AsyncMock(spec=OceaApiClient)
    client.get_resident.return_value = {
        "occupations": [{"logementId": "local", "dateDebut": "2024-01-01", "dateFin": None}]
    }
    client.get_pds_consumption.return_value = {"unite": "m3", "consommations": []}
    meter = _meter()
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    coordinator.async_set_updated_data(
        OceaData(
            totals={},
            meters={
                meter.identifier: _build_meter_data(
                    meter,
                    {"unite": "m3", "consommations": [{"date": "2024-03-10", "valeur": 0.3}]},
                )
            },
        )
    )
    with (
        patch(
            "custom_components.ocea_smart_building.history.dt_util.now",
            side_effect=[
                datetime(2024, 3, 12, 0, 0, tzinfo=UTC),
                datetime(2024, 3, 12, 4, 59, tzinfo=UTC),
                datetime(2024, 3, 12, 5, 0, tzinfo=UTC),
            ],
        ),
        patch("custom_components.ocea_smart_building.history.async_add_external_statistics"),
    ):
        await OceaHistorySynchronizer(hass, entry, client, coordinator)._async_sync()
        restarted = OceaHistorySynchronizer(hass, entry, client, coordinator)
        await restarted._async_sync()
        assert client.get_pds_consumption.await_count == 1
        await restarted._async_sync()
    assert client.get_pds_consumption.await_count == 2
