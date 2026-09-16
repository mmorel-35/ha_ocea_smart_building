"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from requests import RequestException

from .api import OceaApiClient, OceaApiError, OceaAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _current_month_payload(today: date) -> dict[str, str]:
    """Build the daily consumption request for the current local month."""
    return {
        "debut": f"{today.replace(day=1).isoformat()}T00:00:00.000Z",
        "fin": f"{today.isoformat()}T23:59:59.999Z",
        "granularity": "Day",
    }


def _build_meter_data(
    metadata: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate one PDS response while preserving its metadata."""
    total = Decimal(0)
    latest_date = ""
    for item in response.get("consommations", []):
        value = Decimal(str(item.get("valeur", 0)).replace(",", "."))
        total += value
        item_date = str(item.get("date", ""))
        latest_date = max(item_date, latest_date)
    return {
        **metadata,
        "value": float(total),
        "date": latest_date,
    }


class OceaDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manage fetching Ocea consumption data."""

    def __init__(self, hass: HomeAssistant, client: OceaApiClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self._meter_metadata: dict[str, dict[str, Any]] = {}
        self._meters_discovered = False

    async def _async_discover_meters(self) -> None:
        """Discover and cache the configured dwelling's water meters."""
        pds_data = await self.hass.async_add_executor_job(self.client.get_pds)
        pds_ids = [
            str(item["id"])
            for item in pds_data
            if item.get("id")
            and item.get("fluide") in ("EauFroide", "EauChaude")
        ]
        appareils = await self.hass.async_add_executor_job(
            self.client.get_appareils, pds_ids
        )
        appareils_by_pds = {
            str(item["pdsId"]): item
            for item in appareils
            if item.get("pdsId")
        }
        self._meter_metadata = {
            str(item["id"]): {
                "pds": item,
                "appareil": appareils_by_pds.get(str(item["id"])),
            }
            for item in pds_data
            if item.get("id")
            and item.get("fluide") in ("EauFroide", "EauChaude")
        }
        self._meters_discovered = True

    async def _async_update_meters(
        self, payload: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch current-month consumption for each discovered meter."""
        meter_data: dict[str, dict[str, Any]] = {}
        for pds_id, metadata in self._meter_metadata.items():
            try:
                response = await self.hass.async_add_executor_job(
                    self.client.get_pds_consumption,
                    pds_id,
                    payload,
                )
            except OceaAuthError:
                raise
            except (OceaApiError, RequestException):
                _LOGGER.warning("Unable to update one water meter")
                continue
            meter_data[pds_id] = _build_meter_data(metadata, response)
        return meter_data

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Ocea API (runs sync client in executor)."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.hass.async_add_executor_job(
                self.client.get_consumptions
            )
            if not self._meters_discovered:
                await self._async_discover_meters()
            payload = _current_month_payload(dt_util.now().date())
            meter_data = await self._async_update_meters(payload)
        except OceaAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except OceaApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        result: dict[str, Any] = {}
        if meter_data:
            result["meters"] = meter_data
        for item in raw_data:
            fluide = item.get("fluide", "")
            valeur_str = item.get("valeur", "0")
            # Ocea uses comma as decimal separator
            valeur = float(valeur_str.replace(",", "."))

            if fluide == "EauFroide":
                result["eau_froide"] = valeur
            elif fluide == "EauChaude":
                result["eau_chaude"] = valeur
            else:
                result[fluide.lower()] = valeur

        _LOGGER.debug(
            "Ocea consumption data updated: %d aggregate values, %d individual meters",
            len(result) - (1 if "meters" in result else 0),
            len(result.get("meters", {})),
        )
        return result
