"""Data update coordinator for Ocea Smart Building."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .pyocea import OccupancyData, OceaAPIError, OceaAuthError, OceaClient

_LOGGER = logging.getLogger(__name__)


class OceaDataUpdateCoordinator(DataUpdateCoordinator[OccupancyData]):
    """Fetch one configured dwelling and manage its authentication lifecycle."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OceaClient,
        dwelling_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.dwelling_id = dwelling_id
        self._reauth_started = False

    async def _async_update_data(self) -> OccupancyData:
        """Fetch the configured dwelling asynchronously."""
        try:
            data = await self.client.get_dwelling_data(self.dwelling_id)
        except OceaAuthError as err:
            await self._start_reauth()
            raise ConfigEntryAuthFailed("Ocea authentication has expired") from err
        except OceaAPIError as err:
            if err.status_code in {401, 403}:
                await self._start_reauth()
                raise ConfigEntryAuthFailed(
                    "Ocea no longer authorizes the configured dwelling"
                ) from err
            raise UpdateFailed("Unable to retrieve Ocea dwelling data") from err
        self._reauth_started = False
        return data

    async def _start_reauth(self) -> None:
        """Start Home Assistant's native reauthentication flow once per failure."""
        if self._reauth_started:
            return
        self._reauth_started = True
        self.config_entry.async_start_reauth(self.hass)
