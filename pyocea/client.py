"""Asynchronous, Home Assistant-independent Ocea API client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import aiohttp
from yarl import URL

from .exceptions import OceaAPIError, OceaAuthError
from .models import (
    ConsumptionRecord,
    DwellingData,
    MeterData,
    OccupancyData,
)

_LOGGER = logging.getLogger(__name__)

_B2C_TENANT = "osbespaceresident"
_B2C_CLIENT_ID = "1cacfb15-0b3c-42cc-a662-736e4737e7d9"
_B2C_SCOPE = (
    "https://osbespaceresident.onmicrosoft.com/"
    "app-imago-espace-resident-back-prod/user_impersonation "
    "openid profile offline_access"
)
_B2C_REDIRECT_URI = "https://espace-resident.ocea-sb.com"
_B2C_BASE = f"https://{_B2C_TENANT}.b2clogin.com"
_B2C_TENANT_PATH = f"{_B2C_BASE}/{_B2C_TENANT}.onmicrosoft.com"
_B2C_AUTHORIZE = (
    f"{_B2C_TENANT_PATH}/b2c_1a_signup_signin/oauth2/v2.0/authorize"
)
_B2C_TOKEN = f"{_B2C_TENANT_PATH}/b2c_1a_signup_signin/oauth2/v2.0/token"
_API_BASE = "https://espace-resident-api.ocea-sb.com"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36 "
    "Home-Assistant-Ocea-Smart-Building/1.1.0"
)
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_HISTORY_START_YEAR = 2000
_FLUIDS = ("EauFroide", "EauChaude", "Cetc")


def _generate_pkce() -> tuple[str, str]:
    """Create a PKCE verifier and S256 challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _rounded(value: Any) -> float:
    """Parse Ocea's decimal values and keep at most three decimal places."""
    try:
        decimal_value = Decimal(str(value).replace(",", "."))
        if not decimal_value.is_finite():
            raise InvalidOperation
        rounded = decimal_value.quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError) as err:
        raise OceaAPIError("Ocea returned an invalid numeric value") from err
    return float(rounded)


def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Extract a list from an Ocea response, rejecting unknown structures."""
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list) and all(
                isinstance(item, dict) for item in value
            ):
                return value
    raise OceaAPIError("Ocea returned an invalid response structure")


def _dwelling_id(occupation: Mapping[str, Any]) -> str:
    """Get the stable dwelling ID from an occupation row."""
    value = occupation.get("logementId") or occupation.get("dwellingId")
    return str(value).strip() if value else ""


def _address(occupation: Mapping[str, Any]) -> str | None:
    """Extract a displayable dwelling address from an occupation row."""
    for key in ("adresse", "adresseLogement", "address", "libelleAdresse"):
        value = occupation.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            parts = [
                str(value.get(part, "")).strip()
                for part in ("ligne1", "ligne2", "codePostal", "ville")
            ]
            formatted = " ".join(part for part in parts if part)
            if formatted:
                return formatted
    return None


class OceaClient:
    """Client for Ocea resident and consumption APIs using an aiohttp session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        user_agent: str = _BROWSER_USER_AGENT,
    ) -> None:
        """Initialize the client without taking ownership of the session."""
        self._session = session
        self._user_agent = user_agent
        self._email: str | None = None
        self._password: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._resident: dict[str, Any] | None = None
        self._meter_history_cache: dict[
            tuple[str, str], tuple[list[ConsumptionRecord], date | None, Decimal]
        ] = {}
        self._meter_types: dict[tuple[str, str], str] = {}
        self._timeout = _TIMEOUT

    async def authenticate(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate with Azure AD B2C and return the resident record."""
        self._email = email
        self._password = password
        self._access_token = None
        self._refresh_token = None
        self._resident = None
        await self._authenticate_b2c()
        self._resident = await self._api_get("/api/v1/resident")
        if not isinstance(self._resident, dict):
            raise OceaAPIError("Ocea returned an invalid resident response")
        return self._resident

    async def validate_credentials(
        self, email: str, password: str
    ) -> dict[str, Any]:
        """Validate credentials against Ocea before configuration is saved."""
        return await self.authenticate(email, password)

    async def get_available_dwellings(self) -> dict[str, str]:
        """Return accessible dwelling IDs and display addresses."""
        if self._resident is None:
            raise OceaAuthError("Authentication is required before listing dwellings")
        occupations = self._resident.get("occupations", [])
        if not isinstance(occupations, list):
            raise OceaAPIError("Ocea returned an invalid occupation list")

        dwellings: dict[str, str] = {}
        for occupation in occupations:
            if not isinstance(occupation, dict):
                raise OceaAPIError("Ocea returned an invalid occupation")
            dwelling_id = _dwelling_id(occupation)
            if not dwelling_id or dwelling_id in dwellings:
                continue
            try:
                await self._api_get(
                    f"/api/v1/local/{quote(dwelling_id, safe='')}/dashboard/consos"
                )
            except OceaAPIError as err:
                if err.status_code == 403:
                    _LOGGER.debug("Skipping an inaccessible Ocea dwelling")
                    continue
                raise
            dwellings[dwelling_id] = _address(occupation) or dwelling_id
        return dwellings

    async def get_dwelling_data(self, dwelling_id: str) -> OccupancyData:
        """Fetch totals, meters, leak estimates, and daily history for one dwelling."""
        if not dwelling_id.strip():
            raise OceaAPIError("A dwelling ID is required")
        resident = self._resident
        if resident is None:
            raise OceaAuthError("Authentication is required before reading a dwelling")

        occupation = self._find_occupation(resident, dwelling_id)
        encoded_id = quote(dwelling_id, safe="")
        totals = self._parse_totals(
            await self._api_get(
                f"/api/v1/local/{encoded_id}/dashboard/consos"
            )
        )
        pds_payload = await self._api_get(f"/api/v1/local/{encoded_id}/pds")
        raw_pds_rows = _items(pds_payload, "pds", "data")
        pds_by_id = {
            str(row["id"]): row for row in raw_pds_rows if row.get("id")
        }
        pds_rows = list(pds_by_id.values())

        device_rows: list[dict[str, Any]] = []
        if pds_rows:
            device_payload = await self._api_post(
                "/api/v1/local/appareils",
                {"pdsIds": [str(row["id"]) for row in pds_rows]},
            )
            device_rows = _items(device_payload, "appareils", "data")
        devices = {
            str(row["pdsId"]): row for row in device_rows if row.get("pdsId")
        }

        meters: list[MeterData] = []
        leak_estimate = Decimal(0)
        for pds in pds_rows:
            meter_id = str(pds["id"])
            meter_type = str(pds.get("fluide", pds.get("type", "unknown")))
            self._meter_types[(dwelling_id, meter_id)] = meter_type
            history, meter_leak = await self._get_meter_history(dwelling_id, meter_id)
            meters.append(
                MeterData(
                    meter_id=meter_id,
                    serial_number=str(
                        devices.get(meter_id, {}).get("numeroSerie", "")
                    ),
                    meter_type=meter_type,
                    current_value=self._current_month_total(history),
                    history=history,
                )
            )
            if self._normalize_fluid(meter_type) in {"eaufroide", "eauchaude"}:
                leak_estimate += meter_leak

        return OccupancyData(
            occupancy_id=self._occupancy_id(occupation, dwelling_id),
            is_active=self._is_active(occupation),
            dwelling=DwellingData(
                dwelling_id=dwelling_id,
                address=_address(occupation),
                meters=meters,
            ),
            cold_water_total=totals.get("eau_froide", 0.0),
            hot_water_total=totals.get("eau_chaude", 0.0),
            heating_total=totals.get("cetc"),
            has_leak=leak_estimate > 0,
            leak_estimate=_rounded(leak_estimate),
        )

    async def get_daily_history(
        self, dwelling_id: str
    ) -> dict[str, list[ConsumptionRecord]]:
        """Fetch complete dwelling-level daily histories for each supported fluid."""
        if self._resident is None:
            raise OceaAuthError("Authentication is required before reading history")
        self._find_occupation(self._resident, dwelling_id)
        today = datetime.now(ZoneInfo("Europe/Paris")).date()
        result: dict[str, list[ConsumptionRecord]] = {}
        encoded_id = quote(dwelling_id, safe="")
        for fluid in _FLUIDS:
            records: dict[date, float] = {}
            fluid_key = self._normalize_fluid(fluid)
            for (cached_dwelling, _meter_id), (meter_history, _leak_day, _leak) in (
                self._meter_history_cache.items()
            ):
                if (
                    cached_dwelling == dwelling_id
                    and self._normalize_fluid(
                        self._meter_types.get((cached_dwelling, _meter_id), "")
                    ) == fluid_key
                ):
                    for record in meter_history:
                        day = record.date.date()
                        records[day] = records.get(day, 0.0) + record.value
            if records:
                result[fluid] = [
                    ConsumptionRecord(
                        date=datetime.combine(
                            day,
                            datetime.min.time(),
                            tzinfo=ZoneInfo("Europe/Paris"),
                        ),
                        value=_rounded(value),
                    )
                    for day, value in sorted(records.items())
                ]
                continue
            empty_years = 0
            for year in range(today.year, _HISTORY_START_YEAR - 1, -1):
                start = date(year, 1, 1)
                end = min(date(year, 12, 31), today)
                payload = await self._api_post(
                    f"/api/v1/local/{encoded_id}/conso/{fluid}",
                    {
                        "debut": f"{start.isoformat()}T00:00:00.000Z",
                        "fin": f"{end.isoformat()}T23:59:59.999Z",
                        "granularity": "Day",
                    },
                )
                if not isinstance(payload, dict):
                    raise OceaAPIError("Ocea returned an invalid daily history response")
                rows = payload.get("consommations", [])
                if not isinstance(rows, list):
                    raise OceaAPIError("Ocea returned an invalid daily history list")
                empty_years = empty_years + 1 if not rows else 0
                for row in rows:
                    if not isinstance(row, dict):
                        raise OceaAPIError("Ocea returned an invalid history record")
                    day_text = str(row.get("date", ""))[:10]
                    try:
                        day = date.fromisoformat(day_text)
                    except ValueError as err:
                        raise OceaAPIError("Ocea returned an invalid history date") from err
                    records[day] = _rounded(row.get("valeur", 0))
                if records and empty_years >= 2:
                    break
            result[fluid] = [
                ConsumptionRecord(
                    date=datetime.combine(
                        day, datetime.min.time(), tzinfo=ZoneInfo("Europe/Paris")
                    ),
                    value=value,
                )
                for day, value in sorted(records.items())
            ]
        return result

    @staticmethod
    def _normalize_fluid(fluid: str) -> str:
        """Normalize Ocea fluid names for comparisons and history aggregation."""
        normalized = "".join(
            character for character in fluid.lower() if character.isalnum()
        )
        return {
            "eaufroide": "eaufroide",
            "eauchaude": "eauchaude",
            "cetc": "cetc",
        }.get(normalized, normalized)

    async def _authenticate_b2c(self) -> None:
        """Perform the browser-compatible B2C authorization-code PKCE flow."""
        verifier, challenge = _generate_pkce()
        nonce = secrets.token_hex(16)
        state = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "id": secrets.token_hex(16),
                    "meta": {"interactionType": "redirect"},
                }
            ).encode()
        ).decode()
        headers = {"User-Agent": self._user_agent}
        params = {
            "client_id": _B2C_CLIENT_ID,
            "scope": _B2C_SCOPE,
            "redirect_uri": _B2C_REDIRECT_URI,
            "response_mode": "fragment",
            "response_type": "code",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "3.10.0",
            "client_info": "1",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
            "state": state,
        }

        try:
            async with self._session.get(
                _B2C_AUTHORIZE,
                params=params,
                headers=headers,
                allow_redirects=True,
                timeout=self._timeout,
            ) as response:
                self._raise_for_status(response, auth=True)
                login_html = await response.text()
                final_url = str(response.url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OceaAPIError("Unable to reach the Ocea sign-in service") from err

        csrf = self._session.cookie_jar.filter_cookies(URL(_B2C_BASE)).get(
            "x-ms-cpim-csrf"
        )
        if csrf is None:
            raise OceaAuthError("The sign-in response did not include a CSRF cookie")
        csrf_value = csrf.value
        trans_match = re.search(r'"transId"\s*:\s*"([^"]+)"', login_html)
        if trans_match is None:
            raise OceaAuthError("The sign-in response did not include a transaction ID")
        trans_id = trans_match.group(1)

        self_asserted_url = (
            f"{_B2C_BASE}/{_B2C_TENANT}.onmicrosoft.com"
            f"/B2C_1A_SIGNUP_SIGNIN/SelfAsserted"
            f"?tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN"
        )
        self_asserted_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": _B2C_BASE,
            "Referer": final_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": self._user_agent,
            "X-CSRF-TOKEN": csrf_value,
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            async with self._session.post(
                URL(self_asserted_url, encoded=True),
                headers=self_asserted_headers,
                data={
                    "request_type": "RESPONSE",
                    "email": self._email,
                    "password": self._password,
                },
                allow_redirects=False,
                timeout=self._timeout,
            ) as response:
                if response.status in {400, 401, 403}:
                    raise OceaAuthError(
                        f"Ocea sign-in rejected the credentials (HTTP {response.status})"
                    )
                if response.status != 200:
                    raise OceaAPIError(
                        f"Ocea sign-in service failed (HTTP {response.status})",
                        status_code=response.status,
                    )
                try:
                    result = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                    result = {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OceaAPIError("Unable to complete Ocea sign-in") from err
        if isinstance(result, dict) and result.get("status") not in (None, "", "200", 200):
            raise OceaAuthError("Ocea rejected the supplied credentials")

        confirmed_url = (
            f"{_B2C_BASE}/{_B2C_TENANT}.onmicrosoft.com"
            f"/B2C_1A_SIGNUP_SIGNIN/api/CombinedSigninAndSignup/confirmed"
            f"?rememberMe=false&csrf_token={csrf_value}"
            f"&tx={trans_id}&p=B2C_1A_SIGNUP_SIGNIN"
        )
        try:
            async with self._session.get(
                URL(confirmed_url, encoded=True),
                headers={"Referer": final_url, "User-Agent": self._user_agent},
                allow_redirects=False,
                timeout=self._timeout,
            ) as response:
                location = response.headers.get("Location", "")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OceaAPIError("Unable to complete Ocea sign-in") from err

        code = self._authorization_code(location)
        token_data = await self._token_request(
            {
                "client_id": _B2C_CLIENT_ID,
                "redirect_uri": _B2C_REDIRECT_URI,
                "scope": _B2C_SCOPE,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "client_info": "1",
            }
        )
        self._store_tokens(token_data)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        """Exchange a B2C authorization or refresh token."""
        try:
            async with self._session.post(
                _B2C_TOKEN,
                data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                    "Origin": _B2C_REDIRECT_URI,
                    "Referer": f"{_B2C_REDIRECT_URI}/",
                    "User-Agent": self._user_agent,
                },
                timeout=self._timeout,
            ) as response:
                if response.status in {400, 401, 403}:
                    raise OceaAuthError(
                        f"Ocea token request failed (HTTP {response.status})"
                    )
                if response.status != 200:
                    raise OceaAPIError(
                        f"Ocea token service failed (HTTP {response.status})",
                        status_code=response.status,
                    )
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OceaAPIError("Unable to reach the Ocea token service") from err
        except ValueError as err:
            raise OceaAuthError("Ocea returned an invalid token response") from err
        if not isinstance(payload, dict):
            raise OceaAuthError("Ocea returned an invalid token response")
        return payload

    async def _refresh_access_token(self) -> None:
        """Refresh an expired token, falling back to interactive authentication."""
        if self._refresh_token is not None:
            try:
                payload = await self._token_request(
                    {
                        "client_id": _B2C_CLIENT_ID,
                        "scope": _B2C_SCOPE,
                        "refresh_token": self._refresh_token,
                        "grant_type": "refresh_token",
                        "client_info": "1",
                    }
                )
                self._store_tokens(payload)
                return
            except OceaAuthError:
                _LOGGER.debug("Ocea refresh token was rejected; signing in again")
                self._refresh_token = None
        if self._email is None or self._password is None:
            raise OceaAuthError("Authentication is required again")
        await self._authenticate_b2c()

    async def _api_get(self, path: str, *, retry_auth: bool = True) -> Any:
        """Send an authenticated GET request and decode its JSON response."""
        if self._access_token is None:
            if self._email is None or self._password is None:
                raise OceaAuthError("Authentication is required before API requests")
            await self._authenticate_b2c()
        try:
            async with self._session.get(
                f"{_API_BASE}{path}",
                headers=self._api_headers(),
                timeout=self._timeout,
            ) as response:
                if response.status == 401 and retry_auth:
                    await self._refresh_access_token()
                    return await self._api_get(path, retry_auth=False)
                return await self._decode_response(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OceaAPIError("Unable to connect to the Ocea API") from err

    async def _api_post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        retry_auth: bool = True,
    ) -> Any:
        """Send an authenticated JSON POST request and decode its response."""
        if self._access_token is None:
            if self._email is None or self._password is None:
                raise OceaAuthError("Authentication is required before API requests")
            await self._authenticate_b2c()
        try:
            async with self._session.post(
                f"{_API_BASE}{path}",
                headers={**self._api_headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=self._timeout,
            ) as response:
                if response.status == 401 and retry_auth:
                    await self._refresh_access_token()
                    return await self._api_post(path, payload, retry_auth=False)
                return await self._decode_response(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise OceaAPIError("Unable to connect to the Ocea API") from err

    async def _decode_response(self, response: aiohttp.ClientResponse) -> Any:
        """Decode an API response and surface status/JSON errors safely."""
        if response.status == 401:
            raise OceaAuthError("Ocea rejected the refreshed authentication token")
        self._raise_for_status(response)
        try:
            return await response.json(content_type=None)
        except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as err:
            raise OceaAPIError("Ocea returned invalid JSON") from err

    @staticmethod
    def _raise_for_status(
        response: aiohttp.ClientResponse, *, auth: bool = False
    ) -> None:
        """Raise a sanitized exception for non-success HTTP responses."""
        if 200 <= response.status < 300:
            return
        message = f"Ocea request failed (HTTP {response.status})"
        if auth and response.status in {400, 401, 403}:
            raise OceaAuthError(message)
        raise OceaAPIError(message, status_code=response.status)

    def _api_headers(self) -> dict[str, str]:
        """Build the browser-compatible API headers."""
        if self._access_token is None:
            raise OceaAuthError("Authentication is required before API requests")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Origin": _B2C_REDIRECT_URI,
            "Referer": f"{_B2C_REDIRECT_URI}/",
            "User-Agent": self._user_agent,
        }

    def _store_tokens(self, payload: Mapping[str, Any]) -> None:
        """Save tokens without exposing them in logs."""
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OceaAuthError("Ocea did not return an access token")
        self._access_token = access_token
        refresh_token = payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            self._refresh_token = refresh_token

    @staticmethod
    def _authorization_code(location: str) -> str:
        """Extract the authorization code from either redirect query or fragment."""
        if not location:
            raise OceaAuthError("Ocea sign-in did not return an authorization redirect")
        code = None
        if "#" in location:
            fragment = parse_qs(location.split("#", 1)[1])
            code = fragment.get("code", [None])[0]
            if fragment.get("error"):
                raise OceaAuthError("Ocea sign-in was rejected")
        if not code:
            code = parse_qs(urlparse(location).query).get("code", [None])[0]
        if not code:
            raise OceaAuthError("Ocea sign-in did not return an authorization code")
        return code

    def _find_occupation(
        self, resident: Mapping[str, Any], dwelling_id: str
    ) -> dict[str, Any]:
        """Find the selected dwelling's resident occupancy."""
        occupations = resident.get("occupations", [])
        if not isinstance(occupations, list):
            raise OceaAPIError("Ocea returned an invalid occupation list")
        for occupation in occupations:
            if isinstance(occupation, dict) and _dwelling_id(occupation) == dwelling_id:
                return occupation
        raise OceaAPIError("The selected dwelling is not associated with this account")

    @staticmethod
    def _occupancy_id(occupation: Mapping[str, Any], dwelling_id: str) -> str:
        """Extract a contract identifier, falling back to the dwelling ID."""
        for key in ("occupancyId", "idOccupation", "occupationId", "id"):
            value = occupation.get(key)
            if value:
                return str(value)
        return dwelling_id

    @staticmethod
    def _is_active(occupation: Mapping[str, Any]) -> bool:
        """Read an explicit active flag when the resident response provides it."""
        for key in ("isActive", "active", "actif"):
            if key in occupation:
                value = occupation[key]
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.strip().lower() in {"true", "1", "oui"}
                if isinstance(value, int):
                    return value != 0
                raise OceaAPIError("Ocea returned an invalid occupancy status")
        return True

    @staticmethod
    def _parse_totals(payload: Any) -> dict[str, float | None]:
        """Map dashboard totals to stable fluid keys."""
        rows = _items(payload, "consommations", "consos", "data")
        result: dict[str, float | None] = {}
        for item in rows:
            fluid = str(item.get("fluide", "")).lower()
            value = _rounded(item.get("valeur", 0))
            if fluid in {"eaufroide", "eau_froide"}:
                result["eau_froide"] = value
            elif fluid in {"eauchaude", "eau_chaude"}:
                result["eau_chaude"] = value
            elif fluid in {"cetc", "chauffage", "chauffagecetc"}:
                result["cetc"] = value
        return result

    async def _get_meter_history(
        self,         dwelling_id: str, meter_id: str
    ) -> tuple[list[ConsumptionRecord], Decimal]:
        """Fetch daily meter records by year and return its latest leak estimate."""
        today = datetime.now(ZoneInfo("Europe/Paris")).date()
        cache_key = (dwelling_id, meter_id)
        cached = self._meter_history_cache.get(cache_key)
        records = (
            {record.date.date(): Decimal(str(record.value)) for record in cached[0]}
            if cached is not None
            else {}
        )
        latest_leak_day = cached[1] if cached is not None else None
        latest_leak = cached[2] if cached is not None else Decimal(0)
        empty_years = 0
        periods = (
            [(today.replace(day=1), today)]
            if cached is not None
            else [
                (date(year, 1, 1), min(date(year, 12, 31), today))
                for year in range(today.year, _HISTORY_START_YEAR - 1, -1)
            ]
        )
        encoded_dwelling_id = quote(dwelling_id, safe="")
        encoded_meter_id = quote(meter_id, safe="")

        for start, end in periods:
            response = await self._api_post(
                f"/api/v1/local/{encoded_dwelling_id}/pds/"
                f"{encoded_meter_id}/conso",
                {
                    "debut": f"{start.isoformat()}T00:00:00.000Z",
                    "fin": f"{end.isoformat()}T23:59:59.999Z",
                    "granularity": "Day",
                },
            )
            if not isinstance(response, dict):
                raise OceaAPIError("Ocea returned an invalid meter history response")
            rows = response.get("consommations", [])
            if not isinstance(rows, list):
                raise OceaAPIError("Ocea returned an invalid meter history list")
            empty_years = empty_years + 1 if not rows else 0
            for row in rows:
                if not isinstance(row, dict):
                    raise OceaAPIError("Ocea returned an invalid meter history record")
                day_text = str(row.get("date", ""))[:10]
                try:
                    day = date.fromisoformat(day_text)
                    value = Decimal(str(row.get("valeur", 0)).replace(",", "."))
                except (InvalidOperation, TypeError, ValueError) as err:
                    raise OceaAPIError("Ocea returned an invalid meter history record") from err
                if not value.is_finite():
                    raise OceaAPIError("Ocea returned an invalid meter history value")
                records[day] = value
                leak = row.get("fuiteEstimee")
                if latest_leak_day is None or day >= latest_leak_day:
                    latest_leak_day = day
                    latest_leak = Decimal(0)
                if leak is not None:
                    try:
                        leak_value = Decimal(str(leak).replace(",", "."))
                    except (InvalidOperation, TypeError, ValueError) as err:
                        raise OceaAPIError("Ocea returned an invalid leak estimate") from err
                    if not leak_value.is_finite():
                        raise OceaAPIError("Ocea returned an invalid leak estimate")
                    if day >= latest_leak_day:
                        latest_leak_day = day
                        latest_leak = leak_value
            if records and empty_years >= 2:
                break

        history = [
            ConsumptionRecord(
                date=datetime.combine(
                    day, datetime.min.time(), tzinfo=ZoneInfo("Europe/Paris")
                ),
                value=_rounded(value),
            )
            for day, value in sorted(records.items())
        ]
        self._meter_history_cache[cache_key] = (
            history,
            latest_leak_day,
            latest_leak,
        )
        return history, latest_leak

    @staticmethod
    def _current_month_total(history: list[ConsumptionRecord]) -> float:
        """Sum the daily meter consumption for the current Paris calendar month."""
        today = datetime.now(ZoneInfo("Europe/Paris")).date()
        total = sum(
            (Decimal(str(record.value)) for record in history if (
                record.date.year == today.year and record.date.month == today.month
            )),
            Decimal(0),
        )
        return _rounded(total)
