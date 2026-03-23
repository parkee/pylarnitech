"""Exceptions for the Larnitech client library."""


class LarnitechError(Exception):
    """Base exception for Larnitech errors."""


class LarnitechConnectionError(LarnitechError):
    """Error indicating a connection failure."""


class LarnitechAuthError(LarnitechError):
    """Error indicating an authentication failure."""


class LarnitechTimeoutError(LarnitechError):
    """Error indicating a timeout."""


class LarnitechApiError(LarnitechError):
    """Error indicating an API-level error."""

    def __init__(self, message: str, error_type: str | None = None) -> None:
        """Initialize with optional error type."""
        super().__init__(message)
        self.error_type = error_type
