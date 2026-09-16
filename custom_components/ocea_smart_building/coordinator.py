"""DataUpdateCoordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OceaApiClient, OceaApiError, OceaAuthError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .models import OceaMeter
from .water_meter_service import WaterMeterService

_LOGGER = logging.getLogger(__name__)


class OceaDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
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
        self._service = WaterMeterService(client)
        self._meter_metadata: dict[str, OceaMeter] = {}
        self._meters_discovered = False

    async def _async_discover_meters(self) -> None:
        """Discover and cache the configured dwelling's water meters."""
        self._meter_metadata = await self.hass.async_add_executor_job(
            self._service.discover_meters
        )
        self._meters_discovered = True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Ocea API (runs sync client in executor)."""
        _LOGGER.debug("Ocea coordinator: starting data fetch")
        try:
            raw_data = await self.hass.async_add_executor_job(
                self.client.get_consumptions
            )
            if not self._meters_discovered:
                await self._async_discover_meters()
            payload = self._service.build_current_month_payload(dt_util.now().date())
            meter_data = await self.hass.async_add_executor_job(
                self._service.fetch_readings, self._meter_metadata, payload
            )
        except OceaAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except OceaApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        result = self._service.aggregate_dashboard_totals(raw_data)
        if meter_data:
            result["meters"] = meter_data

        _LOGGER.debug(
            "Ocea consumption data updated: %d aggregate values, %d individual meters",
            len(result) - (1 if "meters" in result else 0),
            len(result.get("meters", {})),
        )
        return result
