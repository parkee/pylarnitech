"""Tests for the admin panel client module."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from pylarnitech.admin import LarnitechAdminClient
from pylarnitech.exceptions import LarnitechAuthError, LarnitechConnectionError


@pytest.fixture
def admin() -> LarnitechAdminClient:
    """Create a test admin client."""
    return LarnitechAdminClient(host="192.168.4.100")


class TestLogin:
    """Tests for admin login."""

    @pytest.mark.asyncio
    async def test_login_success(self, admin: LarnitechAdminClient) -> None:
        """Test successful admin login."""
        with aioresponses() as mocked:
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=Account.login",
                payload={
                    "result": True,
                    "data": {"success": True},
                },
            )
            result = await admin.login("admin", "admin")
            assert result is True
        await admin.close()

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, admin: LarnitechAdminClient) -> None:
        """Test login with wrong credentials."""
        with aioresponses() as mocked:
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=Account.login",
                payload={
                    "result": True,
                    "data": {
                        "success": False,
                        "message": "This login does not exist.",
                        "fieldName": "login_name",
                    },
                },
            )
            with pytest.raises(LarnitechAuthError, match="login failed"):
                await admin.login("wrong", "wrong")
        await admin.close()

    @pytest.mark.asyncio
    async def test_login_connection_error(self, admin: LarnitechAdminClient) -> None:
        """Test login when controller unreachable."""
        with aioresponses() as mocked:
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=Account.login",
                status=500,
            )
            with pytest.raises(LarnitechConnectionError):
                await admin.login()
        await admin.close()


class TestGetWSData:
    """Tests for get_ws_data."""

    @pytest.mark.asyncio
    async def test_get_ws_data(self, admin: LarnitechAdminClient) -> None:
        """Test getting WebSocket connection data."""
        with aioresponses() as mocked:
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=AccessKeys.getWSData",
                payload={
                    "result": True,
                    "data": {
                        "success": True,
                        "port": 2041,
                        "key": "0383982796169537",
                        "ip": "192.168.4.100",
                        "apiOne": {
                            "websocket-port": "8080",
                            "secretKey": "7555054131",
                        },
                    },
                },
            )
            data = await admin.get_ws_data()
            assert data["port"] == 2041
            assert data["apiOne"]["secretKey"] == "7555054131"
        await admin.close()


class TestGetControllerInfo:
    """Tests for get_controller_info."""

    @pytest.mark.asyncio
    async def test_get_controller_info(self, admin: LarnitechAdminClient) -> None:
        """Test getting complete controller info."""
        with aioresponses() as mocked:
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=AccessKeys.getWSData",
                payload={
                    "result": True,
                    "data": {
                        "port": 2041,
                        "key": "0383982796169537",
                        "ip": "192.168.4.100",
                        "apiOne": {
                            "websocket-port": "8080",
                            "secretKey": "7555054131",
                        },
                    },
                },
            )
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=Account.getPanelVersion",
                payload={
                    "result": True,
                    "data": {"version": "Larnitech 2.05 release"},
                },
            )
            info = await admin.get_controller_info()
            assert info.host == "192.168.4.100"
            assert info.port == 8080
            assert info.api_key == "7555054131"
            assert info.serial == "0383982796169537"
            assert info.version == "Larnitech 2.05 release"
        await admin.close()


class TestGetPanelVersion:
    """Tests for get_panel_version."""

    @pytest.mark.asyncio
    async def test_get_version(self, admin: LarnitechAdminClient) -> None:
        """Test getting firmware version."""
        with aioresponses() as mocked:
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=Account.getPanelVersion",
                payload={
                    "result": True,
                    "data": {"version": "Larnitech 2.05 release"},
                },
            )
            version = await admin.get_panel_version()
            assert version == "Larnitech 2.05 release"
        await admin.close()

    @pytest.mark.asyncio
    async def test_api_failure(self, admin: LarnitechAdminClient) -> None:
        """Test handling API failure."""
        with aioresponses() as mocked:
            mocked.post(
                "http://192.168.4.100:80/api/api.php?api=Account.getPanelVersion",
                payload={"result": False, "data": False},
            )
            with pytest.raises(LarnitechConnectionError):
                await admin.get_panel_version()
        await admin.close()
