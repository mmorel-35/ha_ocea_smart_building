"""Tests for the Ocea Smart Building config flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import (
    CONF_DWELLING_ID,
    CONF_LOCAL_ID,
    DOMAIN,
    PASSWORD_NOT_CHANGED,
)
from custom_components.ocea_smart_building.pyocea import OceaAPIError, OceaAuthError

EMAIL = "resident@example.com"
PASSWORD = "not-a-real-password"
DWELLING = "dwelling-123"
RESIDENT = {
    "resident": {"prenom": "Alice"},
    "occupations": [{"logementId": DWELLING, "adresse": "1 rue Exemple"}],
}


def _mock_client(
    client_class: Any, resident: dict[str, Any], dwellings: dict[str, str]
) -> Any:
    """Prepare async client methods with deterministic account data."""
    client = client_class.return_value
    client.validate_credentials = AsyncMock(return_value=resident)
    client.get_available_dwellings = AsyncMock(return_value=dwellings)
    return client


@pytest.mark.usefixtures("recorder_component_loaded", "enable_custom_integrations")
async def test_user_step_creates_one_dwelling_entry(hass: HomeAssistant) -> None:
    """A single accessible dwelling skips the selection step."""
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaClient"
    ) as client_class:
        client = _mock_client(client_class, RESIDENT, {DWELLING: "1 rue Exemple"})
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ocea - Alice"
    assert result["data"][CONF_DWELLING_ID] == DWELLING
    assert result["data"][CONF_LOCAL_ID] == DWELLING
    client.validate_credentials.assert_awaited_once_with(EMAIL, PASSWORD)
    client.get_available_dwellings.assert_awaited_once_with()


@pytest.mark.usefixtures("recorder_component_loaded", "enable_custom_integrations")
async def test_user_step_selects_one_of_multiple_dwellings(
    hass: HomeAssistant,
) -> None:
    """Multiple dwellings are offered in a dropdown before entry creation."""
    dwellings = {DWELLING: "1 rue Exemple", "dwelling-456": "2 rue Exemple"}
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaClient"
    ) as client_class:
        _mock_client(client_class, RESIDENT, dwellings)
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "dwelling"
        assert result["data_schema"]({CONF_DWELLING_ID: "dwelling-456"}) == {
            CONF_DWELLING_ID: "dwelling-456"
        }
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DWELLING_ID: "dwelling-456"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DWELLING_ID] == "dwelling-456"


@pytest.mark.usefixtures("recorder_component_loaded", "enable_custom_integrations")
async def test_user_step_reports_no_accessible_dwelling(hass: HomeAssistant) -> None:
    """An account without accessible dwellings receives a translated error."""
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaClient"
    ) as client_class:
        _mock_client(client_class, RESIDENT, {})
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_occupation"}


@pytest.mark.usefixtures("recorder_component_loaded", "enable_custom_integrations")
async def test_user_step_maps_authentication_errors(hass: HomeAssistant) -> None:
    """Invalid credentials are distinguishable from API connection failures."""
    for exception, error in (
        (OceaAuthError("bad credentials"), "invalid_auth"),
        (OceaAPIError("server unavailable"), "cannot_connect"),
    ):
        with patch(
            "custom_components.ocea_smart_building.config_flow.OceaClient"
        ) as client_class:
            client_class.return_value.validate_credentials = AsyncMock(
                side_effect=exception
            )
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
            )
        assert result["errors"] == {"base": error}


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a legacy entry containing additional hidden data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ocea - Alice",
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_LOCAL_ID: DWELLING,
            "access_token": "not-exposed-by-flow",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("recorder_component_loaded", "enable_custom_integrations")
async def test_reconfigure_keeps_password_hidden_and_preserves_other_data(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Reconfiguration retains unchanged credentials and hidden entry data."""
    with (
        patch(
            "custom_components.ocea_smart_building.config_flow.OceaClient"
        ) as client_class,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        client = _mock_client(
            client_class, RESIDENT, {DWELLING: "1 rue Exemple"}
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_RECONFIGURE,
                "entry_id": config_entry.entry_id,
            },
        )
        defaults = result["data_schema"]({})
        assert defaults[CONF_PASSWORD] == PASSWORD_NOT_CHANGED
        assert PASSWORD not in repr(result["data_schema"])
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD_NOT_CHANGED},
        )

    assert result["type"] is FlowResultType.ABORT
    assert config_entry.data[CONF_PASSWORD] == PASSWORD
    assert config_entry.data["access_token"] == "not-exposed-by-flow"
    client.validate_credentials.assert_awaited_once_with(EMAIL, PASSWORD)


@pytest.mark.usefixtures("recorder_component_loaded", "enable_custom_integrations")
async def test_reauth_keeps_the_existing_dwelling(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Reauthentication only succeeds when the configured dwelling is accessible."""
    with patch(
        "custom_components.ocea_smart_building.config_flow.OceaClient"
    ) as client_class:
        client = _mock_client(
            client_class, RESIDENT, {DWELLING: "1 rue Exemple", "other": "Other"}
        )
        config_entry.async_start_reauth(hass)
        await hass.async_block_till_done()
        [flow] = hass.config_entries.flow.async_progress()
        assert flow["context"]["source"] == SOURCE_REAUTH
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {CONF_EMAIL: EMAIL, CONF_PASSWORD: "replacement"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert config_entry.data[CONF_LOCAL_ID] == DWELLING
    assert config_entry.data[CONF_DWELLING_ID] == DWELLING
    assert config_entry.data["access_token"] == "not-exposed-by-flow"
    client.validate_credentials.assert_awaited_once_with(EMAIL, "replacement")


def _translation_data(language: str) -> dict[str, Any]:
    """Load one Home Assistant translation file."""
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / DOMAIN
        / "translations"
        / f"{language}.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_dwelling_and_leak_translations_are_aligned() -> None:
    """Source strings and both translations expose all new flow/entity labels."""
    source = json.loads(
        (Path(__file__).parents[1] / "custom_components" / DOMAIN / "strings.json")
        .read_text(encoding="utf-8")
    )
    for language in ("en", "fr"):
        translated = _translation_data(language)
        assert "dwelling" in translated["config"]["step"]
        assert "no_occupation" in translated["config"]["error"]
        assert "fuite_estimee" in translated["entity"]["sensor"]
        assert "fuite" in translated["entity"]["binary_sensor"]
    assert "dwelling" in source["config"]["step"]
    assert "fuite" in source["entity"]["binary_sensor"]
