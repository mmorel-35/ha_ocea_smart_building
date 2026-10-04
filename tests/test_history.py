"""Tests for importing Ocea history into Recorder statistics."""

from __future__ import annotations

from datetime import datetime, time, timezone
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.history import (
    _statistics,
    async_import_history,
)
from custom_components.ocea_smart_building.pyocea import (
    ConsumptionRecord,
    DwellingData,
    OccupancyData,
)


def test_daily_history_statistics_keep_month_state_and_cumulative_sum() -> None:
    """History rows use local midnight, monthly state, and continuous sums."""
    records = [
        ConsumptionRecord(
            datetime(2026, 9, 30, 8, tzinfo=ZoneInfo("Europe/Paris")), 1.25
        ),
        ConsumptionRecord(
            datetime(2026, 10, 1, 8, tzinfo=ZoneInfo("Europe/Paris")), 0.75
        ),
    ]

    rows = _statistics(records)

    assert len(rows) == 2
    assert rows[0]["state"] == 1.25
    assert rows[0]["sum"] == 1.25
    assert rows[1]["state"] == 0.75
    assert rows[1]["sum"] == 2.0
    assert rows[0]["start"].hour == 0


def test_history_groups_records_by_home_assistant_local_day(monkeypatch) -> None:
    """UTC records near midnight are assigned to the configured local day."""
    local_zone = ZoneInfo("Europe/Paris")
    monkeypatch.setattr(
        dt_util,
        "as_local",
        lambda value: value.astimezone(local_zone),
    )
    monkeypatch.setattr(
        dt_util,
        "start_of_local_day",
        lambda day: datetime.combine(day, time.min, tzinfo=local_zone),
    )
    records = [
        ConsumptionRecord(datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc), 0.5),
        ConsumptionRecord(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc), 0.75),
    ]

    rows = _statistics(records)

    assert len(rows) == 1
    assert rows[0]["start"].day == 30
    assert rows[0]["state"] == 1.25
    assert rows[0]["sum"] == 1.25


async def test_history_uses_external_recorder_statistics(
    hass: HomeAssistant,
) -> None:
    """Imported statistics include the required recorder source and sum."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)
    records = [
        ConsumptionRecord(
            datetime(2026, 9, 1, tzinfo=ZoneInfo("Europe/Paris")), 1.25
        )
    ]
    history = {
        "EauFroide": records,
        "EauChaude": records,
        "Cetc": records,
    }
    client = Mock()
    client.get_daily_history = AsyncMock(return_value=history)
    occupancy = OccupancyData(
        occupancy_id="occupancy",
        is_active=True,
        dwelling=DwellingData("local-123", None),
        cold_water_total=1.25,
        hot_water_total=1.25,
        heating_total=2.0,
        has_leak=False,
        leak_estimate=0,
    )
    with patch(
        "custom_components.ocea_smart_building.history.async_add_external_statistics"
    ) as add_statistics:
        count = await async_import_history(hass, entry, client, occupancy)

    assert count == 3
    assert add_statistics.call_count == 3
    for call in add_statistics.call_args_list:
        metadata = call.args[1]
        assert metadata["source"] == "recorder"
        assert metadata["has_sum"] is True
        assert metadata["statistic_id"].startswith("recorder:")
