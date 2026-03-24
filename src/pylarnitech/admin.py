"""Client for the Larnitech admin panel API (port 80)."""

from __future__ import annotations

import logging
import re
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
        """Get or create an aiohttp session.

        Uses unsafe=True on the cookie jar because the controller
        is accessed by IP address, and the default cookie jar rejects
        cookies for IP-based hosts.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
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

    async def get_modules(self) -> dict[str, dict[str, str]]:
        """Get module info mapping.

        Calls Modules.getModules to retrieve the real hardware model
        names, serial numbers, and firmware versions for all CAN modules.

        Returns dict mapping module_id (str) to info dict with keys:
        model, serial, firmware, serial_num (hex serial as str).
        E.g., {"471": {"model": "DW-010.C", "serial": "0xfc4a21f6", ...}}
        """
        data = await self._api_call(
            "Modules.getModules",
            ["", "", "", "", "-1", "", "id_asc"],
        )
        if not isinstance(data, dict):
            return {}
        result: dict[str, dict[str, str]] = {}
        for m in data.get("modules", []):
            mid = m.get("module_id", "")
            if not mid:
                continue
            model_full = m.get("model_name", "")
            model_short = model_full.split(" ")[0] if model_full else ""
            sn_hex = m.get("module_sn", "")
            # Convert hex serial to decimal for reboot command
            try:
                sn_dec = str(int(sn_hex, 16)) if sn_hex else ""
            except ValueError:
                sn_dec = ""
            # Firmware version contains HTML tags — strip them
            fw_raw = m.get("module_fw_ver", "")
            fw_clean = re.sub(r"<[^>]+>", "", fw_raw).strip()

            result[str(mid)] = {
                "model": model_short,
                "serial": sn_hex,
                "serial_dec": sn_dec,
                "firmware": fw_clean,
            }
        return result

    async def get_modules_extra_data(self) -> dict[str, dict[str, Any]]:
        """Get extra data for modules (locations, hw config).

        Returns dict with keys:
        - locations: {module_id: {"name": primary_area, "all": [areas]}}
        - hw: {module_id: hw_config_string}
        """
        return await self._api_call(
            "Modules.getModulesExtraData",
            ["", "", "0", "0", "-1", "0", "id_asc"],
        )

    async def get_module_filters(self) -> dict[str, str]:
        """Get module type descriptions.

        Returns dict mapping mm_id to full model description.
        E.g., {"144": "DW-010.C", "120": "CW-MLI.B Multi sensor..."}
        """
        data = await self._api_call("Modules.getFilters")
        if not isinstance(data, dict):
            return {}
        result: dict[str, str] = {}
        for item in data.get("type", []):
            mm_id = str(item.get("mm_id", ""))
            model = item.get("model_name", "")
            if mm_id and model:
                result[mm_id] = model
        return result

    async def get_module_hw_config(
        self,
        module_id: str,
    ) -> dict[str, Any]:
        """Get hardware configuration for a specific module.

        Returns per-pin configuration including device types,
        dimmer min/max/runtime, leak sensor settings, etc.
        This is the data behind the admin panel's "Hardware Configuration".
        """
        return await self._api_call(
            "Modules.getModuleHWConfig",
            [module_id],
        )

    async def get_module_info(self, module_id: str) -> dict[str, Any]:
        """Get pin-level info for a module (types and paths).

        Returns dict with 'data' (pin→type/path) and 'types' (type codes).
        """
        return await self._api_call(
            "Modules.getModuleInfo",
            [module_id],
        )

    async def get_module_detail(
        self, module_id: str, serial_dec: str
    ) -> dict[str, Any]:
        """Get detailed single module info (status, temp, uptime, etc).

        Args:
            module_id: e.g., "339"
            serial_dec: decimal serial, e.g., "200115670"
        """
        return await self._api_call(
            "Modules.getModule",
            [f"{module_id}_{serial_dec}", serial_dec],
        )

    async def get_module_params(
        self, module_id: str, serial_dec: str
    ) -> dict[str, Any]:
        """Get runtime parameters for a module (time, temp, etc)."""
        return await self._api_call(
            "Modules.getMainModuleParams",
            [f"{module_id}_{serial_dec}"],
        )

    async def get_module_logs(self, module_id: str) -> list[dict[str, Any]]:
        """Get event logs for a module."""
        result = await self._api_call(
            "Logs.getLogsByModuleId",
            [module_id],
        )
        if isinstance(result, list):
            return result
        return []

    async def set_module_hw(
        self,
        module_id: str,
        hw_config: str,
    ) -> bool:
        """Set hardware configuration for a module.

        Args:
            module_id: e.g., "339"
            hw_config: URL-encoded hw config string, e.g.,
                "hw[IN][1]=K&hw[IN][2]=G&hw[IN][3]=K..."

        Returns True on success.
        """
        from urllib.parse import quote

        data = await self._api_call(
            "Modules.setModuleHW",
            [module_id, quote(hw_config, safe="")],
        )
        return bool(data)

    async def reboot_module(self, module_id: str, serial_dec: str) -> bool:
        """Reboot a CAN bus module.

        Args:
            module_id: The module ID (e.g., "319")
            serial_dec: The module serial number in decimal (e.g., "3196289470")

        Returns True on success.
        """
        data = await self._api_call(
            "Modules.rebootModule",
            [module_id, serial_dec],
        )
        return bool(data)

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
