"""Client for the Larnitech admin panel API (port 80)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import DEFAULT_ADMIN_PORT
from .exceptions import LarnitechAuthError, LarnitechConnectionError
from .models import LarnitechControllerInfo

_LOGGER = logging.getLogger(__name__)


class LarnitechAdminClient:
    """Client for the Larnitech admin panel (LT Setup) web API.

    Used for controller discovery: retrieving serial number, API keys,
    WebSocket port, and firmware version.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_ADMIN_PORT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the admin client."""
        self._host = host
        self._port = port
        self._session = session
        self._own_session = session is None
        self._cookies: dict[str, str] = {}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self) -> None:
        """Close the session if we own it."""
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _api_call(
        self,
        method: str,
        params: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Make an admin panel API call.

        The admin API uses POST with positional parameters:
        POST /api/api.php?api={method}
        Body: param0=val0&param1=val1&...
        """
        session = await self._ensure_session()
        url = f"http://{self._host}:{self._port}/api/api.php"
        form_data = {}
        if params:
            for i, val in enumerate(params):
                form_data[f"param{i}"] = str(val)
        try:
            async with session.post(
                url,
                params={"api": method},
                data=form_data,
            ) as resp:
                if resp.status != 200:
                    raise LarnitechConnectionError(f"Admin API HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise LarnitechConnectionError(
                f"Admin API connection error: {err}"
            ) from err

        if not data.get("result"):
            raise LarnitechConnectionError("Admin API returned failure")
        return data.get("data", {})

    async def login(
        self,
        username: str = "admin",
        password: str = "admin",
    ) -> bool:
        """Log in to the admin panel.

        Returns True on success.
        Raises LarnitechAuthError on authentication failure.
        """
        data = await self._api_call("Account.login", [username, password, 0])
        if isinstance(data, dict) and data.get("success"):
            return True
        message = ""
        if isinstance(data, dict):
            message = data.get("message", "")
        raise LarnitechAuthError(f"Admin login failed: {message}")

    async def get_panel_version(self) -> str:
        """Get the admin panel firmware version."""
        data = await self._api_call("Account.getPanelVersion")
        if isinstance(data, dict):
            return data.get("version", "")
        return ""

    async def get_ws_data(self) -> dict[str, Any]:
        """Get WebSocket connection data including API keys and ports.

        Returns dict with keys:
        - port: native protocol port (e.g., 2041)
        - key: native access key
        - ip: controller IP
        - apiOne.websocket-port: WebSocket port (e.g., "8080")
        - apiOne.secretKey: API key for WebSocket/HTTP API
        """
        return await self._api_call("AccessKeys.getWSData")

    async def get_security_settings(self) -> dict[str, Any]:
        """Get default security settings.

        Returns dict with keys:
        - defaultPassword: bool
        - defaultAccessKey: bool
        - registeredInCloud: bool
        - defaultAccessKeyName: str
        """
        return await self._api_call("AccessKeys.getDefaultSecuritySettings")

    async def get_controller_info(self) -> LarnitechControllerInfo:
        """Get complete controller info by calling multiple admin APIs.

        Requires being logged in first.
        """
        ws_data = await self.get_ws_data()
        version = await self.get_panel_version()

        api_one = ws_data.get("apiOne", {})
        ws_port = int(api_one.get("websocket-port", 8080))
        api_key = api_one.get("secretKey", "")

        return LarnitechControllerInfo(
            host=ws_data.get("ip", self._host),
            port=ws_port,
            api_key=api_key,
            serial=ws_data.get("key", ""),
            version=version,
        )
