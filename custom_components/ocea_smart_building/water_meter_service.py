"""Business logic for Ocea water meters, decoupled from HA scheduling."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any

from requests import RequestException

from .api import OceaApiClient, OceaApiError, OceaAuthError
from .models import OceaMeter, OceaMeterReading

_LOGGER = logging.getLogger(__name__)

# Single source of truth mapping an Ocea fluid code to its sensor key.
WATER_FLUID_SENSOR_KEYS: dict[str, str] = {
    "EauFroide": "eau_froide",
    "EauChaude": "eau_chaude",
}


def _current_month_payload(today: date) -> dict[str, str]:
    """Build the daily consumption request for the current local month."""
    return {
        "debut": f"{today.replace(day=1).isoformat()}T00:00:00.000Z",
        "fin": f"{today.isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }


def _build_meter_data(meter: OceaMeter, response: dict[str, Any]) -> OceaMeterReading:
    """Aggregate one PDS response while preserving its metadata."""
    total = Decimal(0)
    latest_date: str | None = None
    latest_item: dict[str, Any] | None = None
    for item in response.get("consommations", []):
        value = Decimal(str(item.get("valeur", 0)).replace(",", "."))
        total += value
        item_date = str(item.get("date", ""))
        if latest_date is None or item_date >= latest_date:
            latest_date = item_date
            latest_item = item
    latest_item = latest_item or {}
    return OceaMeterReading(
        meter=meter,
        value=float(total),
        latest_date=latest_date,
        unit=response.get("unite"),
        estimated_leak=latest_item.get("fuiteEstimee"),
        consent=latest_item.get("consentement"),
        response_type=latest_item.get("type"),
    )


class WaterMeterService:
    """Water-meter business logic: discovery, aggregation and totals."""

    def __init__(self, client: OceaApiClient) -> None:
        """Initialize the service with the API client it delegates I/O to."""
        self._client = client

    def discover_meters(self) -> dict[str, OceaMeter]:
        """Discover the configured dwelling's water meters (sync, blocking I/O)."""
        pds_data = self._client.get_pds()
        pds_ids = [
            str(item["id"])
            for item in pds_data
            if item.get("id") and item.get("fluide") in WATER_FLUID_SENSOR_KEYS
        ]
        appareils = self._client.get_appareils(pds_ids)
        appareils_by_pds = {
            str(item["pdsId"]): item for item in appareils if item.get("pdsId")
        }
        return {
            str(item["id"]): OceaMeter.from_api(
                item,
                appareils_by_pds.get(str(item["id"])),
            )
            for item in pds_data
            if item.get("id") and item.get("fluide") in WATER_FLUID_SENSOR_KEYS
        }

    def fetch_readings(
        self,
        meters: dict[str, OceaMeter],
        payload: dict[str, str],
    ) -> dict[str, OceaMeterReading]:
        """Fetch current-month consumption for each discovered meter (sync, blocking I/O)."""
        meter_data = {
            pds_id: OceaMeterReading(meter=meter, value=None, latest_date=None)
            for pds_id, meter in meters.items()
        }
        for pds_id, meter in meters.items():
            try:
                response = self._client.get_pds_consumption(pds_id, payload)
            except OceaAuthError:
                raise
            except (OceaApiError, RequestException):
                _LOGGER.warning("Unable to update one water meter")
                continue
            meter_data[pds_id] = _build_meter_data(meter, response)
        return meter_data

    def aggregate_dashboard_totals(
        self, raw_data: list[dict[str, str]]
    ) -> dict[str, float]:
        """Map the dwelling's dashboard consumption entries to sensor keys."""
        totals: dict[str, float] = {}
        for item in raw_data:
            fluide = item.get("fluide", "")
            valeur_str = item.get("valeur", "0")
            # Ocea uses comma as decimal separator
            valeur = float(valeur_str.replace(",", "."))
            key = WATER_FLUID_SENSOR_KEYS.get(fluide, fluide.lower())
            totals[key] = valeur
        return totals
