"""Asynchronous client for the Ocea resident API."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from json import JSONDecodeError
from typing import TypeGuard, TypeVar

from aiohttp import ClientSession, ClientTimeout

from .auth import OceaAuthentication
from .const import API_BASE, B2C_REDIRECT_URI
from .exceptions import OceaApiError
from .types import (
    OceaConsumptionRequest,
    OceaConsumptionResponse,
    OceaDashboardConsumption,
    OceaDeviceResponse,
    OceaDevicesRequest,
    OceaPdsResponse,
    OceaResidentResponse,
    is_consumption_response,
    is_dashboard_consumption,
    is_device_response,
    is_pds_response,
    is_resident_response,
)

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=30)
_ResponseT = TypeVar("_ResponseT", bound=Mapping[str, object])


class OceaApiClient:
    """Client for the resident API using an externally owned session."""

    def __init__(
        self,
        email: str,
        password: str,
        local_id: str = "",
        *,
        user_agent: str,
        session: ClientSession,
    ) -> None:
        """Initialize the API client without taking ownership of the session."""
        self._local_id = local_id
        self._user_agent = user_agent
        self._session = session
        self._auth = OceaAuthentication(session, email, password, user_agent)

    async def _api_request(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> object:
        """Make an authenticated API request, refreshing once on 401."""
        if not self._auth.access_token:
            await self._auth.authenticate()
        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._auth.access_token}",
                "Accept": "application/json",
                "Origin": B2C_REDIRECT_URI,
                "Referer": f"{B2C_REDIRECT_URI}/",
                "User-Agent": self._user_agent,
            }
            if method == "POST":
                headers["Content-Type"] = "application/json"
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                headers=headers,
                json=payload,
                timeout=_TIMEOUT,
            ) as response:
                status = response.status
                if status == 200:
                    try:
                        result: object = await response.json(content_type=None)
                    except (JSONDecodeError, UnicodeDecodeError) as err:
                        raise OceaApiError("Invalid API JSON response") from err
                    return result
            if status == 401 and attempt == 0:
                _LOGGER.debug("Token expired, refreshing")
                await self._auth.refresh_access_token()
                continue
            raise OceaApiError(f"API error: HTTP {status}")
        raise OceaApiError("API authentication retry exhausted")

    async def get_resident(self) -> OceaResidentResponse:
        """Get resident info including occupations (logementId)."""
        result = await self._api_request("GET", "/api/v1/resident")
        return _validated_response(_object_response(result), is_resident_response)

    async def get_consumptions(self) -> list[OceaDashboardConsumption]:
        """Get dwelling consumption data."""
        result = await self._api_request(
            "GET", f"/api/v1/local/{self._local_id}/dashboard/consos"
        )
        return [
            _validated_response(item, is_dashboard_consumption)
            for item in _list_response(result)
        ]

    async def get_pds(self) -> list[OceaPdsResponse]:
        """Get the configured dwelling's points of measurement."""
        result = await self._api_request("GET", f"/api/v1/local/{self._local_id}/pds")
        return [_validated_response(item, is_pds_response) for item in _list_response(result)]

    async def get_appareils(self, pds_ids: list[str]) -> list[OceaDeviceResponse]:
        """Get the devices associated with the given points of measurement."""
        payload: OceaDevicesRequest = {"pdsIds": pds_ids}
        result = await self._api_request("POST", "/api/v1/local/appareils", payload)
        return [_validated_response(item, is_device_response) for item in _list_response(result)]

    async def get_pds_consumption(
        self, pds_id: str, payload: OceaConsumptionRequest
    ) -> OceaConsumptionResponse:
        """Get consumption data for one point of measurement."""
        result = _object_response(
            await self._api_request(
                "POST", f"/api/v1/local/{self._local_id}/pds/{pds_id}/conso", payload
            )
        )
        return _validated_response(result, is_consumption_response)

    async def validate_credentials(self) -> OceaResidentResponse:
        """Authenticate and return the resident API response."""
        await self._auth.authenticate()
        return await self.get_resident()


def _object_response(response: object) -> dict[str, object]:
    """Validate the outer shape of an API object response."""
    if not isinstance(response, dict) or not all(isinstance(key, str) for key in response):
        raise OceaApiError("Invalid API object response")
    return {key: value for key, value in response.items() if isinstance(key, str)}


def _list_response(response: object) -> list[dict[str, object]]:
    """Validate the outer shape of a list of API objects."""
    if not isinstance(response, list):
        raise OceaApiError("Invalid API list response")
    return [_object_response(item) for item in response]


def _validated_response(
    response: dict[str, object],
    validate: Callable[[dict[str, object]], TypeGuard[_ResponseT]],
) -> _ResponseT:
    """Return a response only after all known fields have been validated."""
    if not validate(response):
        raise OceaApiError("Invalid API response")
    return response
