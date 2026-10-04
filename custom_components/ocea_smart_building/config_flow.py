"""Config flow for Ocea Smart Building."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DWELLING_ID,
    CONF_LOCAL_ID,
    DOMAIN,
    PASSWORD_NOT_CHANGED,
    UA,
)
from .pyocea import OceaAPIError, OceaAuthError, OceaClient

_LOGGER = logging.getLogger(__name__)


class OceaSmartBuildingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup, dwelling selection, reauthentication, and reconfiguration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize transient flow state."""
        super().__init__()
        self._pending_account: dict[str, str] = {}
        self._pending_resident: dict[str, Any] = {}
        self._pending_dwellings: dict[str, str] = {}

    async def _authenticate(
        self, email: str, password: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Validate credentials and list dwellings accessible to the account."""
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            client = OceaClient(session, user_agent=UA)
            resident = await client.validate_credentials(email, password)
            dwellings = await client.get_available_dwellings()
            return resident, dwellings
        finally:
            session.detach()

    @staticmethod
    def _entry_title(resident: dict[str, Any], dwelling_id: str) -> str:
        """Build the established title from validated resident data."""
        resident_info = resident.get("resident", {})
        first_name = (
            resident_info.get("prenom", "") if isinstance(resident_info, dict) else ""
        )
        return f"Ocea - {str(first_name).strip() or dwelling_id}"

    async def _async_create_dwelling_entry(
        self,
        email: str,
        password: str,
        resident: dict[str, Any],
        dwelling_id: str,
    ) -> FlowResult:
        """Create a unique config entry for the selected dwelling."""
        normalized_email = email.strip().lower()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            current_id = entry.data.get(
                CONF_DWELLING_ID, entry.data.get(CONF_LOCAL_ID)
            )
            if (
                entry.data.get(CONF_EMAIL, "").strip().lower() == normalized_email
                and current_id == dwelling_id
            ):
                return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(f"{normalized_email}:{dwelling_id}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._entry_title(resident, dwelling_id),
            data={
                CONF_EMAIL: email,
                CONF_PASSWORD: password,
                CONF_LOCAL_ID: dwelling_id,
                CONF_DWELLING_ID: dwelling_id,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Authenticate an account and choose one of its accessible dwellings."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip()
            password = str(user_input[CONF_PASSWORD])
            try:
                resident, dwellings = await self._authenticate(email, password)
            except OceaAuthError:
                errors["base"] = "invalid_auth"
            except OceaAPIError as err:
                errors["base"] = (
                    "no_occupation" if err.status_code == 403 else "cannot_connect"
                )
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.error("Unexpected error during Ocea account validation")
                errors["base"] = "unknown"
            else:
                if not dwellings:
                    errors["base"] = "no_occupation"
                elif len(dwellings) == 1:
                    dwelling_id, _address = next(iter(dwellings.items()))
                    return await self._async_create_dwelling_entry(
                        email, password, resident, dwelling_id
                    )
                else:
                    self._pending_account = {
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                    }
                    self._pending_resident = resident
                    self._pending_dwellings = dwellings
                    return await self.async_step_dwelling()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.EMAIL,
                            autocomplete="email",
                        )
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_dwelling(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user select one dwelling when the account has several."""
        if user_input is not None:
            dwelling_id = str(user_input[CONF_DWELLING_ID])
            return await self._async_create_dwelling_entry(
                self._pending_account[CONF_EMAIL],
                self._pending_account[CONF_PASSWORD],
                self._pending_resident,
                dwelling_id,
            )
        return self.async_show_form(
            step_id="dwelling",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DWELLING_ID): vol.In(
                        self._pending_dwellings
                    )
                }
            ),
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start the native reauthentication flow for an existing entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Validate replacement credentials against the entry's existing dwelling."""
        entry = self._get_reauth_entry()
        dwelling_id = str(
            entry.data.get(CONF_DWELLING_ID) or entry.data[CONF_LOCAL_ID]
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip()
            password = str(user_input[CONF_PASSWORD])
            try:
                resident, dwellings = await self._authenticate(email, password)
            except OceaAuthError:
                errors["base"] = "invalid_auth"
            except OceaAPIError as err:
                errors["base"] = (
                    "no_occupation" if err.status_code == 403 else "cannot_connect"
                )
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.error("Unexpected error during Ocea reauthentication")
                errors["base"] = "unknown"
            else:
                if dwelling_id not in dwellings:
                    errors["base"] = "no_occupation"
                else:
                    normalized_email = email.lower()
                    for configured_entry in self.hass.config_entries.async_entries(
                        DOMAIN
                    ):
                        configured_dwelling = configured_entry.data.get(
                            CONF_DWELLING_ID,
                            configured_entry.data.get(CONF_LOCAL_ID),
                        )
                        if (
                            configured_entry.entry_id != entry.entry_id
                            and configured_entry.data.get(CONF_EMAIL, "").lower()
                            == normalized_email
                            and configured_dwelling == dwelling_id
                        ):
                            return self.async_abort(reason="already_configured")
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=f"{normalized_email}:{dwelling_id}",
                        title=self._entry_title(resident, dwelling_id),
                        data_updates={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                            CONF_LOCAL_ID: dwelling_id,
                            CONF_DWELLING_ID: dwelling_id,
                        },
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL,
                        default=entry.data[CONF_EMAIL],
                    ): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.EMAIL,
                            autocomplete="email",
                        )
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update credentials while retaining the selected dwelling and hidden data."""
        entry = self._get_reconfigure_entry()
        dwelling_id = str(
            entry.data.get(CONF_DWELLING_ID) or entry.data[CONF_LOCAL_ID]
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip()
            password = str(user_input[CONF_PASSWORD])
            if password == PASSWORD_NOT_CHANGED:
                password = entry.data[CONF_PASSWORD]
            try:
                resident, dwellings = await self._authenticate(email, password)
            except OceaAuthError:
                errors["base"] = "invalid_auth"
            except OceaAPIError as err:
                errors["base"] = (
                    "no_occupation" if err.status_code == 403 else "cannot_connect"
                )
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.error("Unexpected error during Ocea reconfiguration")
                errors["base"] = "unknown"
            else:
                if dwelling_id not in dwellings:
                    errors["base"] = "no_occupation"
                else:
                    normalized_email = email.lower()
                    for configured_entry in self.hass.config_entries.async_entries(
                        DOMAIN
                    ):
                        configured_dwelling = configured_entry.data.get(
                            CONF_DWELLING_ID,
                            configured_entry.data.get(CONF_LOCAL_ID),
                        )
                        if (
                            configured_entry.entry_id != entry.entry_id
                            and configured_entry.data.get(CONF_EMAIL, "").lower()
                            == normalized_email
                            and configured_dwelling == dwelling_id
                        ):
                            return self.async_abort(reason="already_configured")
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=f"{normalized_email}:{dwelling_id}",
                        title=self._entry_title(resident, dwelling_id),
                        data_updates={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                            CONF_LOCAL_ID: dwelling_id,
                            CONF_DWELLING_ID: dwelling_id,
                        },
                        reload_even_if_entry_is_unchanged=False,
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL,
                        default=entry.data[CONF_EMAIL],
                    ): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.EMAIL,
                            autocomplete="email",
                        )
                    ),
                    vol.Required(
                        CONF_PASSWORD,
                        default=PASSWORD_NOT_CHANGED,
                    ): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    ),
                }
            ),
            errors=errors,
        )
