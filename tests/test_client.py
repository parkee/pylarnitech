"""Tests for the client module."""

from __future__ import annotations

import re
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses

from pylarnitech.client import LarnitechClient
from pylarnitech.exceptions import (
    LarnitechApiError,
    LarnitechConnectionError,
    LarnitechTimeoutError,
)

# aioresponses needs a regex to match URLs with query params
API_URL = re.compile(r"http://192\.168\.4\.100:8888/\?json=.*")


@pytest.fixture
def client() -> LarnitechClient:
    """Create a test client."""
    return LarnitechClient(
        host="192.168.4.100",
        api_key="testkey123",
    )


class TestGetDevices:
    """Tests for get_devices."""

    @pytest.mark.asyncio
    async def test_get_devices_success(self, client: LarnitechClient) -> None:
        """Test successful device list retrieval."""
        response_data = {
            "requestType": "devicesList",
            "devices": [
                {
                    "addr": "388:3",
                    "type": "lamp",
                    "name": "Kitchen Light",
                    "nAddr": 99331,
                    "area": "Kitchen",
                    "system": "no",
                },
                {
                    "addr": "407:1",
                    "type": "AC",
                    "name": "Office AC",
                    "nAddr": 104193,
                    "area": "Office",
                    "system": "no",
                },
            ],
        }
        with aioresponses() as mocked:
            mocked.get(API_URL, payload=response_data)
            devices = await client.get_devices()
            assert len(devices) == 2
            assert devices[0].type == "lamp"
            assert devices[1].type == "AC"
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_devices_empty(self, client: LarnitechClient) -> None:
        """Test empty device list."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={"requestType": "devicesList", "devices": []},
            )
            devices = await client.get_devices()
            assert len(devices) == 0
        await client.disconnect()


class TestGetDeviceStatus:
    """Tests for get_device_status."""

    @pytest.mark.asyncio
    async def test_get_lamp_status(self, client: LarnitechClient) -> None:
        """Test getting lamp status."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={
                    "requestType": "deviceStatus",
                    "status": {
                        "addr": "388:3",
                        "type": "lamp",
                        "state": "on",
                        "nAddr": 99331,
                    },
                },
            )
            status = await client.get_device_status("388:3")
            assert status.state == "on"
            assert status.type == "lamp"
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_ac_status(self, client: LarnitechClient) -> None:
        """Test getting AC status with hex state."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={
                    "requestType": "deviceStatus",
                    "status": {
                        "addr": "407:1",
                        "type": "AC",
                        "state": "39001C620431100000",
                        "nAddr": 104193,
                    },
                },
            )
            status = await client.get_device_status("407:1")
            assert status.state == "39001C620431100000"
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_get_dimmer_status(self, client: LarnitechClient) -> None:
        """Test getting dimmer status with brightness."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={
                    "requestType": "deviceStatus",
                    "status": {
                        "addr": "298:3",
                        "type": "dimmer-lamp",
                        "state": "on",
                        "nAddr": 76291,
                        "brightness": 50,
                    },
                },
            )
            status = await client.get_device_status("298:3")
            assert status.brightness == 50
        await client.disconnect()


class TestSetDeviceStatus:
    """Tests for set_device_status and set_device_status_raw."""

    @pytest.mark.asyncio
    async def test_set_lamp_on(self, client: LarnitechClient) -> None:
        """Test turning on a lamp."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={"status": {"state": "on"}},
            )
            result = await client.set_device_status("388:3", {"state": "on"})
            assert result["status"]["state"] == "on"
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_set_ac_raw(self, client: LarnitechClient) -> None:
        """Test setting AC state via raw hex."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={
                    "status": {"state": "39001B620431100000"},
                },
            )
            result = await client.set_device_status_raw("407:1", "39001B620431100000")
            assert result["status"]["state"] == "39001B620431100000"
        await client.disconnect()


class TestSendIRSignal:
    """Tests for send_ir_signal."""

    @pytest.mark.asyncio
    async def test_send_ir(self, client: LarnitechClient) -> None:
        """Test sending an IR signal."""
        signal = "196407000200A706"
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={"status": {"state": signal}},
            )
            result = await client.send_ir_signal("288:11", signal)
            assert result["status"]["state"] == signal
        await client.disconnect()


class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_api_error(self, client: LarnitechClient) -> None:
        """Test API error response."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={"error": "request not supported"},
            )
            with pytest.raises(LarnitechApiError, match="request not supported"):
                await client.set_device_status("339:250", {"state": "on"})
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_connection_error(self, client: LarnitechClient) -> None:
        """Test connection error."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                exception=aiohttp.ClientConnectionError("refused"),
            )
            with pytest.raises(LarnitechConnectionError):
                await client.get_devices()
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_timeout_error(self, client: LarnitechClient) -> None:
        """Test timeout error."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                exception=TimeoutError(),
            )
            with pytest.raises(LarnitechTimeoutError):
                await client.get_devices()
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_http_500_error(self, client: LarnitechClient) -> None:
        """Test HTTP 500 error."""
        with aioresponses() as mocked:
            mocked.get(API_URL, status=500, body="Server Error")
            with pytest.raises(LarnitechApiError, match="HTTP 500"):
                await client.get_devices()
        await client.disconnect()


class TestValidateConnection:
    """Tests for validate_connection."""

    @pytest.mark.asyncio
    async def test_validate_success(self, client: LarnitechClient) -> None:
        """Test successful connection validation."""
        with aioresponses() as mocked:
            mocked.get(
                API_URL,
                payload={
                    "devices": [
                        {"addr": "1:1", "type": "lamp", "name": "L"},
                    ]
                },
            )
            count = await client.validate_connection()
            assert count == 1
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_validate_empty_warns(self, client: LarnitechClient) -> None:
        """Test validation with empty device list."""
        with aioresponses() as mocked:
            mocked.get(API_URL, payload={"devices": []})
            count = await client.validate_connection()
            assert count == 0
        await client.disconnect()


class TestCallbacks:
    """Tests for callback registration."""

    def test_on_status_update(self, client: LarnitechClient) -> None:
        """Test registering and unregistering status callbacks."""
        received: list[dict[str, Any]] = []
        unsub = client.on_status_update(received.append)

        client._dispatch_status({"addr": "1:1", "state": "on"})
        assert len(received) == 1

        unsub()
        client._dispatch_status({"addr": "1:1", "state": "off"})
        assert len(received) == 1

    def test_on_disconnect(self, client: LarnitechClient) -> None:
        """Test registering and unregistering disconnect callbacks."""
        called: list[bool] = []
        unsub = client.on_disconnect(lambda: called.append(True))

        client._notify_disconnect()
        assert len(called) == 1

        unsub()
        client._notify_disconnect()
        assert len(called) == 1

    def test_double_unsubscribe(self, client: LarnitechClient) -> None:
        """Test that double unsubscribe is safe."""
        unsub = client.on_status_update(lambda d: None)
        unsub()
        unsub()  # Should not raise

    def test_callback_error_doesnt_break_others(self, client: LarnitechClient) -> None:
        """Test that a failing callback doesn't prevent others."""
        results: list[str] = []

        def bad_callback(data: dict[str, Any]) -> None:
            raise ValueError("oops")

        def good_callback(data: dict[str, Any]) -> None:
            results.append("ok")

        client.on_status_update(bad_callback)
        client.on_status_update(good_callback)

        client._dispatch_status({"test": True})
        assert results == ["ok"]
