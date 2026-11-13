"""Synchronize dated Ocea consumption as external statistics."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Final
from uuid import uuid4

from aiohttp import ClientError
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .const import CONF_LOCAL_ID, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import (
    OceaDailyConsumption,
    OceaDataUpdateCoordinator,
    OceaMeterReading,
)
from .ocea import OceaApiClient, OceaApiError, OceaAuthError, OceaMeter

_LOGGER = logging.getLogger(__name__)
_CHUNK_DAYS: Final = 30


def history_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, object]]:
    """Create the private store for an entry's historical readings."""
    return Store(
        hass, 1, f"{DOMAIN}_history.{entry.entry_id}", private=True, atomic_writes=True
    )


@dataclass(slots=True)
class MeterHistory:
    """Persisted consumption and next unprocessed date for a single meter."""

    start: date
    cursor: date
    days: dict[date, Decimal] = field(default_factory=dict)
    next_request: datetime | None = None


def _api_date(value: object) -> date:
    """Parse an Ocea calendar date without exposing the source value."""
    if not isinstance(value, str):
        raise OceaApiError("Missing or invalid Ocea date")
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value).date()
    except ValueError as err:
        raise OceaApiError("Missing or invalid Ocea date") from err


def _occupation_start(
    resident: dict[str, object], local_id: str, today: date
) -> date:
    """Find the unique current occupation for the configured dwelling."""
    occupations = resident.get("occupations")
    if not isinstance(occupations, list):
        raise OceaApiError("Invalid Ocea occupations")
    matching: list[date] = []
    for occupation in occupations:
        if not isinstance(occupation, dict):
            raise OceaApiError("Invalid Ocea occupation")
        if occupation.get("logementId") != local_id:
            continue
        start = _api_date(occupation.get("dateDebut"))
        raw_end = occupation.get("dateFin")
        end = _api_date(raw_end) if raw_end is not None else None
        if start <= today and (end is None or end >= today):
            matching.append(start)
    if len(matching) != 1:
        raise OceaApiError("No unique active Ocea occupation for the dwelling")
    return matching[0]


def _history_start(occupation_start: date, meter: OceaMeter) -> date:
    """Bound a meter's history by both occupation and device installation."""
    return max(occupation_start, _api_date(meter.installation_date))


def _history_id(
    local_id: str,
    meter: OceaMeter,
    start: date,
    time_zone: str,
    generation: str,
    account: str,
) -> str:
    """Build a stable, opaque identifier for a meter, account and time zone."""
    identity = (
        f"{local_id}\0{meter.identifier}\0{start.isoformat()}\0"
        f"{time_zone}\0{generation}\0{account}"
    )
    return sha256(identity.encode()).hexdigest()


def _decimal_value(value: object) -> Decimal:
    """Validate a non-negative, finite consumption value."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise OceaApiError("Invalid daily consumption value")
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation as err:
        raise OceaApiError("Invalid daily consumption value") from err
    if not result.is_finite() or result < 0:
        raise OceaApiError("Invalid daily consumption value")
    return result


def _parse_daily_response(response: dict[str, object]) -> tuple[OceaDailyConsumption, ...]:
    """Validate a daily PDS response without assuming missing values are zero."""
    if response.get("unite") != "m3":
        raise OceaApiError("Unsupported daily consumption unit")
    items = response.get("consommations")
    if not isinstance(items, list):
        raise OceaApiError("Invalid daily consumption list")
    readings: list[OceaDailyConsumption] = []
    seen: set[date] = set()
    for item in items:
        if not isinstance(item, dict):
            raise OceaApiError("Invalid daily consumption item")
        if item.get("consentement") is False:
            raise OceaApiError("Ocea does not provide consent for daily consumption")
        day = _api_date(item.get("date"))
        if day in seen:
            raise OceaApiError("Duplicate daily consumption date")
        seen.add(day)
        readings.append(OceaDailyConsumption(day, _decimal_value(item.get("valeur"))))
    return tuple(readings)


def _load_histories(data: dict[str, object] | None) -> tuple[str, dict[str, MeterHistory]]:
    """Validate persisted history before using it."""
    if data is None:
        return uuid4().hex, {}
    generation = data.get("generation")
    meters = data.get("meters")
    if not isinstance(generation, str) or not generation or not isinstance(meters, dict):
        raise OceaApiError("Invalid persisted consumption history")
    histories: dict[str, MeterHistory] = {}
    for key, raw in meters.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise OceaApiError("Invalid persisted consumption history")
        start = _api_date(raw.get("start"))
        cursor = _api_date(raw.get("cursor"))
        raw_next_request = raw.get("next_request")
        next_request: datetime | None = None
        if raw_next_request is not None:
            if not isinstance(raw_next_request, str):
                raise OceaApiError("Invalid persisted consumption history")
            try:
                next_request = datetime.fromisoformat(raw_next_request)
            except ValueError as err:
                raise OceaApiError("Invalid persisted consumption history") from err
            if next_request.utcoffset() is None:
                raise OceaApiError("Invalid persisted consumption history")
        raw_days = raw.get("days")
        if cursor < start or not isinstance(raw_days, dict):
            raise OceaApiError("Invalid persisted consumption history")
        days: dict[date, Decimal] = {}
        for day, value in raw_days.items():
            parsed_day = _api_date(day)
            if parsed_day < start:
                raise OceaApiError("Invalid persisted consumption history")
            days[parsed_day] = _decimal_value(value)
        histories[key] = MeterHistory(start, cursor, days, next_request)
    return generation, histories


def _serialize_histories(
    generation: str, histories: dict[str, MeterHistory]
) -> dict[str, object]:
    """Serialize histories without storing account identifiers or credentials."""
    return {
        "generation": generation,
        "meters": {
            key: {
                "start": history.start.isoformat(),
                "cursor": history.cursor.isoformat(),
                "days": {day.isoformat(): str(value) for day, value in history.days.items()},
                "next_request": (
                    history.next_request.isoformat() if history.next_request else None
                ),
            }
            for key, history in histories.items()
        }
    }


def _daily_statistics(
    hass: HomeAssistant,
    start_day: date,
    days: dict[date, Decimal],
    changed_since: date | None = None,
) -> list[StatisticData]:
    """Build cumulative hourly statistics, assigning each daily value to its day."""
    if not days:
        return []
    zone = dt_util.get_time_zone(hass.config.time_zone)
    if zone is None:
        raise OceaApiError("Invalid Home Assistant time zone")

    def day_hour(day: date) -> datetime:
        next_midnight = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
        return (dt_util.as_utc(next_midnight) - timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )

    result: list[StatisticData] = []
    if changed_since is None:
        result.append(
            StatisticData(start=day_hour(start_day) - timedelta(hours=1), state=0, sum=0)
        )
    total = Decimal(0)
    for day, value in sorted(days.items()):
        total += value
        if changed_since is None or day >= changed_since:
            result.append(
                StatisticData(start=day_hour(day), state=float(value), sum=float(total))
            )
    return result


class OceaHistorySynchronizer:
    """Backfill and update one external statistic per water PDS."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OceaApiClient,
        coordinator: OceaDataUpdateCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.coordinator = coordinator
        self.local_id: str = entry.data[CONF_LOCAL_ID]
        self.store = history_store(hass, entry)
        self._task: asyncio.Task[None] | None = None
        self._histories: dict[str, MeterHistory] | None = None
        self._generation: str | None = None
        self._published: set[str] = set()

    @callback
    def async_schedule(self) -> None:
        """Schedule one synchronization without overlapping previous cycles."""
        if self._task is not None and not self._task.done():
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_sync(), "Ocea consumption history"
        )

    async def _async_sync(self) -> None:
        """Synchronize each meter independently."""
        try:
            if self._histories is None:
                self._generation, self._histories = _load_histories(
                    await self.store.async_load()
                )
            local_now = dt_util.now()
            today = local_now.date()
            now = dt_util.as_utc(local_now)
            occupation_start = _occupation_start(
                await self.client.get_resident(), self.local_id, today
            )
        except OceaApiError as err:
            _LOGGER.warning("Unable to synchronize Ocea consumption history: %s", err)
            return
        except (OceaAuthError, ClientError, TimeoutError, OSError):
            _LOGGER.warning("Unable to synchronize Ocea consumption history")
            return

        data = self.coordinator.data
        if data is None:
            return
        for reading in data.meters.values():
            try:
                await self._async_sync_meter(reading, occupation_start, today, now)
            except OceaAuthError:
                _LOGGER.warning("Unable to authenticate while synchronizing Ocea history")
                return
            except OceaApiError as err:
                _LOGGER.warning("Unable to synchronize one Ocea meter history: %s", err)
            except (ClientError, TimeoutError, OSError):
                _LOGGER.warning("Unable to synchronize one Ocea meter history")

    async def _async_sync_meter(
        self, reading: OceaMeterReading, occupation_start: date, today: date, now: datetime
    ) -> None:
        """Update cached readings and publish their recalculated cumulative sums."""
        assert self._histories is not None
        assert self._generation is not None
        meter = reading.meter
        start = _history_start(occupation_start, meter)
        if start > today:
            raise OceaApiError("Ocea meter history begins in the future")
        key = _history_id(
            self.local_id,
            meter,
            start,
            self.hass.config.time_zone,
            self._generation,
            self.entry.unique_id or self.entry.entry_id,
        )
        previous = self._histories.get(key, MeterHistory(start, start))
        history = MeterHistory(
            previous.start, previous.cursor, previous.days.copy(), previous.next_request
        )

        if history.cursor <= today:
            request_start = history.cursor
            end = min(request_start + timedelta(days=_CHUNK_DAYS - 1), today)
            advance_cursor = True
        elif today.day <= 7 and (today.replace(day=1) - timedelta(days=1)) >= start:
            request_start = max(start, today - timedelta(days=7))
            end = today.replace(day=1) - timedelta(days=1)
            advance_cursor = False
        else:
            request_start = None
            end = None
            advance_cursor = False

        if request_start is not None and end is not None:
            try:
                if (
                    advance_cursor
                    and request_start >= today.replace(day=1)
                    and reading.daily_consumptions is not None
                    and reading.unit == "m3"
                ):
                    items = tuple(
                        item
                        for item in reading.daily_consumptions
                        if request_start <= item.day <= end
                    )
                elif history.next_request is None or now >= history.next_request:
                    history.next_request = now + timedelta(seconds=DEFAULT_SCAN_INTERVAL)
                    reserved = MeterHistory(
                        history.start,
                        history.cursor,
                        history.days.copy(),
                        history.next_request,
                    )
                    self._histories = {**self._histories, key: reserved}
                    await self.store.async_save(
                        _serialize_histories(self._generation, self._histories)
                    )
                    response = await self.client.get_pds_consumption(
                        meter.identifier,
                        {
                            "debut": f"{request_start.isoformat()}T00:00:00.000Z",
                            "fin": f"{end.isoformat()}T23:59:59.999Z",
                            "granularity": "Day",
                        },
                    )
                    items = _parse_daily_response(response)
                else:
                    items = None
                if items is not None:
                    if any(not request_start <= item.day <= end for item in items):
                        raise OceaApiError("Daily consumption lies outside the requested period")
                    for item in items:
                        history.days[item.day] = item.value
                    if advance_cursor:
                        history.cursor = end + timedelta(days=1)
            except OceaApiError as err:
                _LOGGER.warning("Unable to retrieve one Ocea meter history period: %s", err)
            except (ClientError, TimeoutError):
                _LOGGER.warning("Unable to retrieve one Ocea meter history period")

        if reading.daily_consumptions is None or reading.unit != "m3":
            _LOGGER.warning("Unable to use current daily readings for one Ocea meter")
        else:
            seen: set[date] = set()
            for item in reading.daily_consumptions:
                if item.consent is False or item.day in seen:
                    raise OceaApiError("Invalid current daily consumption")
                seen.add(item.day)
                if start <= item.day <= today:
                    history.days[item.day] = _decimal_value(str(item.value))

        if key not in self._histories or history != previous:
            updated = {**self._histories, key: history}
            await self.store.async_save(_serialize_histories(self._generation, updated))
            self._histories = updated
        changed_days = [
            day for day, value in history.days.items() if previous.days.get(day) != value
        ]
        if not changed_days and key in self._published:
            return
        changed_since = min(changed_days) if key in self._published else None
        statistics = _daily_statistics(self.hass, start, history.days, changed_since)
        if not statistics:
            return
        name = meter.serial_number or key[:8]
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Ocea {meter.pds.fluid} {name}",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:water_{key}",
            unit_class=VolumeConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )
        async_add_external_statistics(self.hass, metadata, statistics)
        self._published.add(key)
