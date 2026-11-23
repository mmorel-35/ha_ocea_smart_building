"""Errors raised by the OCEA API client."""


class OCEAError(Exception):
    """Base exception for OCEA client errors."""


class OCEAAuthError(OCEAError):
    """Authentication error."""


class OCEAApiError(OCEAError):
    """API communication error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Keep the HTTP status separately from the error message."""
        super().__init__(message)
        self.status = status


class OCEAConnectionError(OCEAApiError):
    """Unable to connect to an OCEA service."""


class OCEAResponseError(OCEAApiError):
    """OCEA returned an invalid response."""
