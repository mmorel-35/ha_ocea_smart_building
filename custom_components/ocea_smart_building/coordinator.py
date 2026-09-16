"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OceaApiClient, OceaApiError, OceaAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Ocea API (runs sync client in executor)."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.hass.async_add_executor_job(
                self.client.get_consumptions
            )
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
            today = datetime.now(ZoneInfo("Europe/Paris")).date()
            payload = {
                "debut": f"{today.replace(day=1).isoformat()}T00:00:00.000Z",
                "fin": f"{today.isoformat()}T23:59:59.999Z",
                "granularity": "Day",
            }
            meter_data: dict[str, dict[str, Any]] = {}
            for pds in pds_data:
                pds_id = str(pds.get("id", ""))
                if not pds_id or pds.get("fluide") not in (
                    "EauFroide",
                    "EauChaude",
                ):
                    continue
                response = await self.hass.async_add_executor_job(
                    self.client.get_pds_consumption,
                    pds_id,
                    payload,
                )
                total = 0.0
                latest_date = ""
                for item in response.get("consommations", []):
                    value = float(str(item.get("valeur", 0)).replace(",", "."))
                    total += value
                    item_date = str(item.get("date", ""))
                    latest_date = max(item_date, latest_date)
                meter_data[pds_id] = {
                    "pds": pds,
                    "appareil": appareils_by_pds.get(pds_id),
                    "value": total,
                    "date": latest_date,
                }
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

        _LOGGER.debug("Ocea consumption data updated: %s", result)
        return result
