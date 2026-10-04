"""Tests for the standalone and HACS-vendored pyocea client."""

from __future__ import annotations

from decimal import Decimal
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Self
from unittest.mock import AsyncMock, Mock

import pytest

from pyocea import (
    ConsumptionRecord,
    DwellingData,
    MeterData,
    OccupancyData,
    OceaAPIError,
    OceaAuthError,
    OceaClient,
)


class _Response:
    """Minimal aiohttp response double."""

    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "",
        payload: object = None,
        headers: dict[str, str] | None = None,
        url: str = "https://login.example/",
    ) -> None:
        self.status = status
        self._text = text
        self._payload = payload
        self.headers = headers or {}
        self.url = url

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def text(self) -> str:
        return self._text

    async def json(self, **_kwargs: object) -> object:
        return self._payload


class _CookieJar:
    """Cookie jar double for the B2C CSRF cookie."""

    def filter_cookies(self, _url: object) -> SimpleCookie:
        cookies = SimpleCookie()
        cookies["x-ms-cpim-csrf"] = "csrf-placeholder"
        return cookies


def test_strict_models_use_requested_three_level_shape() -> None:
    """All data models expose the requested immutable API attributes."""
    record = ConsumptionRecord(
        date=__import__("datetime").datetime.fromisoformat(
            "2026-09-01T00:00:00+02:00"
        ),
        value=0.25,
    )
    meter = MeterData("pds-1", "serial-1", "EauFroide", 1.25, [record])
    dwelling = DwellingData("dwelling-1", "1 rue Exemple", [meter])
    occupancy = OccupancyData(
        "occupancy-1", True, dwelling, 2.0, 3.0, None, False, 0.0
    )

    assert occupancy.dwelling.meters[0].history == [record]
    assert occupancy.heating_total is None


async def test_http_401_after_token_refresh_is_an_auth_error() -> None:
    """A rejected refreshed token triggers native reauthentication."""
    client = OceaClient(Mock())

    with pytest.raises(OceaAuthError):
        await client._decode_response(_Response(status=401))


async def test_available_dwellings_skips_only_http_403() -> None:
    """Stale forbidden occupations are ignored, with other failures preserved."""
    client = OceaClient(Mock())
    client._resident = {
        "occupations": [
            {"logementId": "stale", "adresse": "Old address"},
            {"logementId": "active", "adresse": "1 rue Exemple"},
        ]
    }
    client._api_get = AsyncMock(
        side_effect=[
            OceaAPIError("forbidden", status_code=403),
            [{"fluide": "EauFroide", "valeur": "1"}],
        ]
    )

    assert await client.get_available_dwellings() == {
        "active": "1 rue Exemple"
    }

    client._api_get = AsyncMock(
        side_effect=OceaAPIError("server error", status_code=500)
    )
    with pytest.raises(OceaAPIError) as error:
        await client.get_available_dwellings()
    assert error.value.status_code == 500


async def test_b2c_auth_preserves_browser_request_flow_and_pkce() -> None:
    """B2C auth retains CSRF, exact transaction query, redirect parsing, and PKCE."""
    session = Mock()
    session.cookie_jar = _CookieJar()
    session.get = Mock(
        side_effect=[
            _Response(
                text='<script>{"transId":"StateProperties=eyJhbGci=="}</script>',
                url="https://login.example/final",
            ),
            _Response(
                headers={
                    "Location": (
                        "https://espace-resident.ocea-sb.com/"
                        "#code=authorization-code&state=state"
                    )
                }
            ),
            _Response(payload={"resident": {}, "occupations": []}),
        ]
    )
    session.post = Mock(
        side_effect=[
            _Response(payload={"status": "200"}),
            _Response(
                payload={
                    "access_token": "access-token-placeholder",
                    "refresh_token": "refresh-token-placeholder",
                }
            ),
        ]
    )
    client = OceaClient(session)

    resident = await client.authenticate(
        "resident@example.com", "placeholder-password"
    )

    assert resident["occupations"] == []
    authorize_call = session.get.call_args_list[0]
    assert str(authorize_call.args[0]).endswith(
        "/oauth2/v2.0/authorize"
    )
    assert authorize_call.kwargs["headers"]["User-Agent"].startswith("Mozilla/")
    authorize_params = authorize_call.kwargs["params"]
    assert authorize_params["code_challenge_method"] == "S256"
    assert authorize_params["code_challenge"]
    self_asserted_call = session.post.call_args_list[0]
    self_asserted_url = str(self_asserted_call.args[0])
    assert "tx=StateProperties=eyJhbGci==&p=B2C_1A_SIGNUP_SIGNIN" in (
        self_asserted_url
    )
    assert self_asserted_call.kwargs["headers"]["X-CSRF-TOKEN"] == "csrf-placeholder"
    assert self_asserted_call.kwargs["data"]["email"] == "resident@example.com"
    token_call = session.post.call_args_list[1]
    assert token_call.kwargs["data"]["grant_type"] == "authorization_code"
    assert token_call.kwargs["data"]["code"] == "authorization-code"
    assert client._access_token == "access-token-placeholder"


async def test_dwelling_data_parses_rounded_totals_and_meter_history() -> None:
    """Only the requested dwelling is parsed into aggregate and meter models."""
    client = OceaClient(Mock())
    client._resident = {
        "occupations": [
            {
                "id": "occupancy-1",
                "logementId": "dwelling-1",
                "adresse": "1 rue Exemple",
                "actif": True,
            },
            {"logementId": "dwelling-2", "adresse": "2 rue Exemple"},
        ]
    }
    client._api_get = AsyncMock(
        side_effect=[
            [
                {"fluide": "EauFroide", "valeur": "12,34567"},
                {"fluide": "EauChaude", "valeur": "5,6789"},
                {"fluide": "Cetc", "valeur": "42,12345"},
            ],
            [{"id": "pds-1", "fluide": "EauFroide"}],
        ]
    )
    client._api_post = AsyncMock(
        return_value=[{"pdsId": "pds-1", "numeroSerie": "SERIAL-1"}]
    )
    history = [
        ConsumptionRecord(
            date=__import__("datetime").datetime.fromisoformat(
                "2026-09-01T00:00:00+02:00"
            ),
            value=0.124,
        )
    ]
    client._get_meter_history = AsyncMock(return_value=(history, Decimal("0.0154")))

    data = await client.get_dwelling_data("dwelling-1")

    assert data.occupancy_id == "occupancy-1"
    assert data.is_active
    assert data.dwelling.address == "1 rue Exemple"
    assert data.cold_water_total == 12.346
    assert data.hot_water_total == 5.679
    assert data.heating_total == 42.123
    assert data.has_leak
    assert data.leak_estimate == 0.015
    assert data.dwelling.meters == [
        MeterData("pds-1", "SERIAL-1", "EauFroide", 0.0, history)
    ]
    assert client._api_get.await_args_list[0].args[0].endswith(
        "/local/dwelling-1/dashboard/consos"
    )
    assert client._api_get.await_args_list[1].args[0].endswith(
        "/local/dwelling-1/pds"
    )


def test_hacs_vendor_matches_standalone_library() -> None:
    """HACS includes a byte-identical copy of the standalone pyocea package."""
    root = Path(__file__).parents[1] / "pyocea"
    vendored = (
        Path(__file__).parents[1]
        / "custom_components"
        / "ocea_smart_building"
        / "pyocea"
    )
    assert {path.name for path in root.glob("*.py")} == {
        path.name for path in vendored.glob("*.py")
    }
    for source in root.glob("*.py"):
        assert (vendored / source.name).read_bytes() == source.read_bytes()
