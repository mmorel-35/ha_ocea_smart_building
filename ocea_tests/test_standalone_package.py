"""Tests for using the OCEA client package without Home Assistant."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from importlib.resources import files
from typing import is_typeddict
from unittest.mock import patch

import aiohttp
import ocea
from aiohttp import web
from aiohttp.test_utils import TestServer
from ocea import (
    OCEAApiClient,
    OCEAApiError,
    OCEAAuthError,
    OCEAConnectionError,
    OCEAConsumptionRequest,
    OCEAConsumptionSeries,
    OCEADashboardData,
    OCEAError,
    OCEAResponseError,
)


class StandalonePackageTests(unittest.IsolatedAsyncioTestCase):
    """Verify the installed package's public surface and API models."""

    def test_import_does_not_load_home_assistant(self) -> None:
        self.assertNotIn("homeassistant", sys.modules)
        self.assertIs(ocea.OCEAApiClient, OCEAApiClient)

    def test_public_client_and_errors_are_importable(self) -> None:
        self.assertTrue(callable(OCEAApiClient))
        self.assertTrue(is_typeddict(OCEAConsumptionRequest))
        self.assertTrue(callable(OCEAConsumptionSeries.from_api))
        self.assertTrue(files("ocea").joinpath("py.typed").is_file())
        self.assertTrue(issubclass(OCEAAuthError, OCEAError))
        self.assertTrue(issubclass(OCEAApiError, OCEAError))
        self.assertTrue(issubclass(OCEAConnectionError, OCEAApiError))
        self.assertTrue(issubclass(OCEAResponseError, OCEAApiError))

    def test_api_models_can_be_used_without_integration_models(self) -> None:
        dashboard = OCEADashboardData.from_api({"fluide": "EauFroide", "valeur": "1,25"})
        series = OCEAConsumptionSeries.from_api(
            {"consommations": [{"date": "2026-01-02", "valeur": "2.5"}]}
        )

        self.assertEqual(dashboard.value, Decimal("1.25"))
        self.assertEqual(series.items[0].value, Decimal("2.5"))

    async def test_client_fetches_typed_resident_with_caller_owned_session(self) -> None:
        app = web.Application()

        async def resident_response(_: web.Request) -> web.Response:
            return web.json_response({"resident": {}, "occupations": []})

        app.router.add_get("/api/v1/resident", resident_response)
        server = TestServer(app)
        await server.start_server()

        try:
            api_base = str(server.make_url("/")).rstrip("/")
            with patch("ocea.client.API_BASE", api_base):
                async with aiohttp.ClientSession() as session:
                    client = OCEAApiClient(
                        email="synthetic@example.test",
                        password="synthetic-password",
                        user_agent="OCEA standalone test",
                        session=session,
                    )
                    client._auth._access_token = "synthetic-token"

                    self.assertEqual(await client.get_resident(), ocea.OCEAResident("", ()))
                    self.assertFalse(session.closed)
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
