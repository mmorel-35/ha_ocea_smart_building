"""The Ocea Smart Building integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CONF_DWELLING_ID, CONF_LOCAL_ID, DOMAIN, UA
from .coordinator import OceaDataUpdateCoordinator
from .history import async_import_history
from .pyocea import OceaAPIError, OceaAuthError, OceaClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


def _entry_dwelling_id(entry: ConfigEntry) -> str:
    """Read the selected dwelling while supporting existing config entries."""
    return str(entry.data.get(CONF_DWELLING_ID) or entry.data[CONF_LOCAL_ID])


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the selected Ocea dwelling."""
    dwelling_id = _entry_dwelling_id(entry)
    client = OceaClient(
        async_create_clientsession(hass),
        user_agent=UA,
    )
    try:
        await client.authenticate(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    except OceaAuthError as err:
        entry.async_start_reauth(hass)
        raise ConfigEntryAuthFailed("Ocea authentication failed") from err
    except OceaAPIError as err:
        if err.status_code in {401, 403}:
            entry.async_start_reauth(hass)
            raise ConfigEntryAuthFailed(
                "Ocea no longer authorizes this account"
            ) from err
        raise ConfigEntryNotReady("Unable to connect to Ocea") from err
    coordinator = OceaDataUpdateCoordinator(hass, entry, client, dwelling_id)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if entry.options.get("history_imported") is not True:
        try:
            await async_import_history(hass, entry, client, coordinator.data)
        except Exception:
            _LOGGER.exception(
                "Ocea history import failed; it will be retried on the next setup"
            )
        else:
            hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, "history_imported": True},
            )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the integration platforms."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
