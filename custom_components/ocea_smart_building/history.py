"""One-time import of Ocea daily readings into Home Assistant Recorder."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import CONF_LOCAL_ID, DOMAIN
from .pyocea import ConsumptionRecord, OccupancyData, OceaClient

_LOGGER = logging.getLogger(__name__)

_HISTORY_SENSORS = {
    "EauFroide": ("eau_froide", "m³", "volume"),
    "EauChaude": ("eau_chaude", "m³", "volume"),
    "Cetc": ("cetc", "kWh", "energy"),
}


def _statistics(
    records: list[ConsumptionRecord],
) -> list[StatisticData]:
    """Convert daily deltas to local-day monthly state and cumulative sum."""
    by_day: dict[date, Decimal] = defaultdict(Decimal)
    for record in records:
        by_day[dt_util.as_local(record.date).date()] += Decimal(str(record.value))

    rows: list[StatisticData] = []
    total = Decimal(0)
    month_total = Decimal(0)
    current_month: tuple[int, int] | None = None
    for day, value in sorted(by_day.items()):
        month = (day.year, day.month)
        if month != current_month:
            current_month = month
            month_total = Decimal(0)
        month_total += value
        total += value
        local_start = dt_util.start_of_local_day(day)
        rows.append(
            StatisticData(
                start=dt_util.as_utc(local_start),
                state=float(month_total),
                sum=float(total),
            )
        )
    return rows


async def async_import_history(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: OceaClient,
    occupancy: OccupancyData | None,
) -> int:
    """Import all available daily readings once and return the statistics count."""
    local_id = str(entry.data.get("dwelling_id") or entry.data[CONF_LOCAL_ID])
    history = await client.get_daily_history(local_id)
    imported = 0

    for fluid, (key, unit, unit_class) in _HISTORY_SENSORS.items():
        if fluid == "Cetc" and occupancy is not None and occupancy.heating_total is None:
            continue
        records = history.get(fluid, [])
        if not records:
            continue
        unique_id = f"{DOMAIN}_{local_id}_{key}"
        statistic_id = f"recorder:{'_'.join(filter(None, slugify(unique_id).split('_')))}"
        rows = _statistics(records)
        if not rows:
            continue
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Ocea {key.replace('_', ' ').title()}",
            source="recorder",
            statistic_id=statistic_id,
            unit_of_measurement=unit,
            unit_class=unit_class,
        )
        async_add_external_statistics(hass, metadata, rows)
        imported += len(rows)

    return imported
