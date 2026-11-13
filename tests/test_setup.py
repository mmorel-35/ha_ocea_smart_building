"""Tests for integration setup lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building import async_remove_entry, async_setup_entry
from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.history import history_store


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_closes_client_when_first_refresh_fails(
    hass: HomeAssistant,
) -> None:
    """Test a failed initial refresh does not leak the HTTP session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch(
            "custom_components.ocea_smart_building.OceaDataUpdateCoordinator"
        ) as coordinator_class,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh.side_effect = (
            RuntimeError("refresh failed")
        )

        with pytest.raises(RuntimeError, match="refresh failed"):
            await async_setup_entry(hass, entry)

    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_detaches_session_when_cancelled(hass: HomeAssistant) -> None:
    """Cancellation during first refresh releases the isolated session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OceaDataUpdateCoordinator", autospec=True) as coordinator_class,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await async_setup_entry(hass, entry)
    assert client_class.call_args.kwargs["session"].closed


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_session_is_detached_after_entry_unload(hass: HomeAssistant) -> None:
    """The isolated session is alive during setup and detached after unload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local-123"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch("custom_components.ocea_smart_building.OceaDataUpdateCoordinator", autospec=True),
        patch("custom_components.ocea_smart_building.OceaHistorySynchronizer", autospec=True),
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock),
        patch.object(hass.config_entries, "async_unload_platforms", new_callable=AsyncMock, return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        session = client_class.call_args.kwargs["session"]
        assert not session.closed
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert session.closed


async def test_removing_entry_removes_cached_history(hass: HomeAssistant) -> None:
    """Removing an entry removes its cached readings."""
    entry = MockConfigEntry(domain=DOMAIN)
    store = history_store(hass, entry)
    await store.async_save({"meters": {}})
    await async_remove_entry(hass, entry)
    assert await history_store(hass, entry).async_load() is None


@pytest.mark.usefixtures("enable_custom_integrations", "real_recorder")
async def test_unload_cancels_history_task(hass: HomeAssistant) -> None:
    """A pending history request is cancelled before the entry is unloaded."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_EMAIL: "resident@example.com", CONF_PASSWORD: "secret", CONF_LOCAL_ID: "local"},
    )
    entry.add_to_hass(hass)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_resident() -> dict[str, object]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    with patch("custom_components.ocea_smart_building.OceaApiClient", autospec=True) as client:
        client.return_value.get_resident.side_effect = blocked_resident
        client.return_value.get_pds.return_value = []
        client.return_value.get_appareils.return_value = []
        client.return_value.get_consumptions.return_value = []
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(started.wait(), timeout=3)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert cancelled.is_set()
