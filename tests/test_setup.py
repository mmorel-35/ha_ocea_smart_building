"""Tests for integration setup and one-time history import."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building import async_setup_entry
from custom_components.ocea_smart_building.const import (
    CONF_DWELLING_ID,
    CONF_LOCAL_ID,
    DOMAIN,
)


async def test_history_flag_is_saved_only_after_import_succeeds(
    hass: HomeAssistant,
) -> None:
    """Successful history import sets the option while preserving other options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "stored",
            CONF_LOCAL_ID: "local-123",
            CONF_DWELLING_ID: "local-123",
        },
        options={"existing_option": True},
    )
    entry.add_to_hass(hass)
    coordinator = Mock()
    coordinator.data = None
    coordinator.async_config_entry_first_refresh = AsyncMock()
    with (
        patch(
            "custom_components.ocea_smart_building.async_create_clientsession",
            return_value=Mock(),
        ),
        patch(
            "custom_components.ocea_smart_building.OceaClient"
        ) as client_class,
        patch(
            "custom_components.ocea_smart_building.OceaDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.ocea_smart_building.async_import_history",
            new=AsyncMock(return_value=0),
        ) as import_history,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        client_class.return_value.authenticate = AsyncMock()
        assert await async_setup_entry(hass, entry)

    client_class.return_value.authenticate.assert_awaited_once_with(
        "resident@example.com", "stored"
    )
    assert import_history.await_count == 1
    assert entry.options == {
        "existing_option": True,
        "history_imported": True,
    }
    assert hass.data[DOMAIN][entry.entry_id] is coordinator
    client_class.assert_called_once()


async def test_history_import_failure_does_not_set_one_time_flag(
    hass: HomeAssistant,
) -> None:
    """Failed statistics imports remain eligible for retry on the next setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "stored",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)
    coordinator = Mock()
    coordinator.data = None
    coordinator.async_config_entry_first_refresh = AsyncMock()
    with (
        patch(
            "custom_components.ocea_smart_building.async_create_clientsession",
            return_value=Mock(),
        ),
        patch(
            "custom_components.ocea_smart_building.OceaClient"
        ) as client_class,
        patch(
            "custom_components.ocea_smart_building.OceaDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.ocea_smart_building.async_import_history",
            new=AsyncMock(side_effect=RuntimeError("recorder unavailable")),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        client_class.return_value.authenticate = AsyncMock()
        assert await async_setup_entry(hass, entry)

    assert entry.options.get("history_imported") is not True
