"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, WATER_FLUID_SENSOR_KEYS
from .ocea import OceaApiClient, OceaApiError, OceaAuthError, OceaMeter


@dataclass(frozen=True, slots=True)
class OceaDailyConsumption:
    """One dated Ocea consumption value."""

    day: date
    value: Decimal
    consent: bool | None = None


@dataclass(frozen=True, slots=True)
class OceaMeterReading:
    """Current-month consumption for one Ocea meter."""

    meter: OceaMeter
    value: float | None
    latest_date: str | None
    unit: str | None = None
    estimated_leak: float | None = None
    consent: bool | None = None
    response_type: str | None = None
    daily_consumptions: tuple[OceaDailyConsumption, ...] | None = None


@dataclass(frozen=True, slots=True)
class OceaData:
    """Coordinator payload: dwelling totals and per-meter readings."""

    totals: dict[str, float]
    meters: dict[str, OceaMeterReading]


_LOGGER = logging.getLogger(__name__)


def _current_month_payload(today: date) -> dict[str, str]:
    """Build the daily consumption request for the current local month."""
    return {
        "debut": f"{today.replace(day=1).isoformat()}T00:00:00.000Z",
        "fin": f"{today.isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }


def _build_meter_data(meter: OceaMeter, response: dict[str, object]) -> OceaMeterReading:
    """Aggregate one PDS response while preserving its metadata."""
    if not isinstance(response, dict):
        raise OceaApiError("Invalid meter consumption response")

    consumptions = response.get("consommations", [])
    if not isinstance(consumptions, list):
        raise OceaApiError("Invalid meter consumption list")

    total = Decimal(0)
    latest_date: str | None = None
    latest_item: dict[str, object] | None = None
    daily_consumptions: list[OceaDailyConsumption] = []
    valid_daily_data = True
    for raw_item in consumptions:
        if not isinstance(raw_item, dict) or not all(
            isinstance(key, str) for key in raw_item
        ):
            raise OceaApiError("Invalid meter consumption item")
        item: dict[str, object] = {
            key: value for key, value in raw_item.items() if isinstance(key, str)
        }
        try:
            value = Decimal(str(item.get("valeur", 0)).replace(",", "."))
        except (InvalidOperation, TypeError, ValueError) as err:
            raise OceaApiError("Invalid meter consumption value") from err
        if not value.is_finite():
            raise OceaApiError("Invalid meter consumption value")
        total += value
        raw_date = item.get("date", "")
        item_date = str(raw_date)
        consent = item.get("consentement")
        if not isinstance(raw_date, str):
            valid_daily_data = False
        else:
            try:
                day = (
                    date.fromisoformat(raw_date)
                    if len(raw_date) == 10
                    else datetime.fromisoformat(raw_date).date()
                )
            except ValueError:
                valid_daily_data = False
            else:
                if "valeur" not in item or not isinstance(consent, (bool, type(None))):
                    valid_daily_data = False
                else:
                    daily_consumptions.append(OceaDailyConsumption(day, value, consent))
        if latest_date is None or item_date >= latest_date:
            latest_date = item_date
            latest_item = item
    latest_item = latest_item or {}
    estimated_leak = latest_item.get("fuiteEstimee")
    leak_value: float | None = None
    if estimated_leak is not None:
        try:
            leak_decimal = Decimal(str(estimated_leak).replace(",", "."))
        except (InvalidOperation, TypeError, ValueError) as err:
            raise OceaApiError("Invalid estimated leak value") from err
        if not leak_decimal.is_finite():
            raise OceaApiError("Invalid estimated leak value")
        leak_value = float(leak_decimal)

    unit = response.get("unite")
    consent = latest_item.get("consentement")
    response_type = latest_item.get("type")
    if (
        (unit is not None and not isinstance(unit, str))
        or (consent is not None and not isinstance(consent, bool))
        or (response_type is not None and not isinstance(response_type, str))
    ):
        raise OceaApiError("Invalid meter consumption metadata")

    return OceaMeterReading(
        meter=meter,
        value=float(total),
        latest_date=latest_date,
        unit=unit,
        estimated_leak=leak_value,
        consent=consent,
        response_type=response_type,
        daily_consumptions=tuple(daily_consumptions) if valid_daily_data else None,
    )


def _as_update_error(err: Exception) -> Exception:
    """Translate a client error into the matching coordinator exception."""
    if isinstance(err, OceaAuthError):
        return ConfigEntryAuthFailed(f"Authentication failed: {err}")
    if isinstance(err, OceaApiError):
        return UpdateFailed(f"Error fetching data: {err}")
    if isinstance(err, (ClientError, TimeoutError)):
        return UpdateFailed("Error connecting to Ocea")
    return UpdateFailed("Unexpected error fetching data")


class OceaDataUpdateCoordinator(DataUpdateCoordinator[OceaData]):
    """Manage fetching Ocea consumption data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OceaApiClient,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self._meters: dict[str, OceaMeter] = {}

    async def _async_setup(self) -> None:
        """Discover the dwelling's water meters once, before the first refresh."""
        try:
            self._meters = await self._discover_meters()
        except Exception as err:
            raise _as_update_error(err) from err

    async def _async_update_data(self) -> OceaData:
        """Fetch data from the Ocea API."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.client.get_consumptions()
            payload = _current_month_payload(dt_util.now().date())
            meters = await self._fetch_readings(payload)
            data = OceaData(totals=self._aggregate_totals(raw_data), meters=meters)
        except Exception as err:
            raise _as_update_error(err) from err

        _LOGGER.debug(
            "Ocea consumption data updated: %d aggregate values, %d individual meters",
            len(data.totals),
            len(data.meters),
        )
        return data

    async def _discover_meters(self) -> dict[str, OceaMeter]:
        """Discover the configured dwelling's water meters."""
        pds_data = [
            item
            for item in await self.client.get_pds()
            if item.get("id") and item.get("fluide") in WATER_FLUID_SENSOR_KEYS
        ]
        appareils = await self.client.get_appareils([str(item["id"]) for item in pds_data])
        appareils_by_pds = {
            str(item["pdsId"]): item for item in appareils if item.get("pdsId")
        }
        return {
            str(item["id"]): OceaMeter.from_api(
                item, appareils_by_pds.get(str(item["id"]))
            )
            for item in pds_data
        }

    async def _fetch_readings(self, payload: dict[str, str]) -> dict[str, OceaMeterReading]:
        """Fetch current-month consumption for each meter."""
        readings = {
            pds_id: OceaMeterReading(meter=meter, value=None, latest_date=None)
            for pds_id, meter in self._meters.items()
        }
        for pds_id, meter in self._meters.items():
            try:
                response = await self.client.get_pds_consumption(pds_id, dict(payload))
                readings[pds_id] = _build_meter_data(meter, response)
            except OceaAuthError:
                raise
            except (OceaApiError, ClientError, TimeoutError):
                _LOGGER.warning("Unable to update one water meter")
        return readings

    @staticmethod
    def _aggregate_totals(raw_data: list[dict[str, object]]) -> dict[str, float]:
        """Map the dwelling's dashboard consumption entries to sensor keys."""
        totals: dict[str, float] = {}
        for item in raw_data:
            fluide = item.get("fluide", "")
            raw_value = item.get("valeur", "0")
            if not isinstance(fluide, str) or not isinstance(raw_value, str):
                raise OceaApiError("Invalid dwelling consumption response")
            # Ocea uses comma as decimal separator
            valeur = float(raw_value.replace(",", "."))
            totals[WATER_FLUID_SENSOR_KEYS.get(fluide, fluide.lower())] = valeur
        return totals
