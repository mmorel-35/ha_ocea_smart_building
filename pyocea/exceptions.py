"""Exceptions raised by the Ocea API client."""

from __future__ import annotations


class OceaError(Exception):
    """Base exception for Ocea client errors."""


class OceaAuthError(OceaError):
    """Authentication failed or credentials have expired."""


class OceaAPIError(OceaError):
    """The API request failed or returned an invalid response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize the API error with an optional HTTP status."""
        super().__init__(message)
        self.status_code = status_code
