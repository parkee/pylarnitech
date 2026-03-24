"""Larnitech WebSocket and HTTP API client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as _url_quote

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Callable

from .const import DEFAULT_HTTP_PORT, DEFAULT_WS_PORT
from .exceptions import (
    LarnitechApiError,
    LarnitechAuthError,
    LarnitechConnectionError,
    LarnitechTimeoutError,
)
from .models import LarnitechDevice, LarnitechDeviceStatus

_LOGGER = logging.getLogger(__name__)

# Reconnection backoff schedule (seconds)
_RECONNECT_BACKOFF = [5, 10, 30, 60, 120, 300]


class LarnitechClient:
    """Client for communicating with a Larnitech controller.

    Supports WebSocket (primary, real-time push) and HTTP (fallback).
    All command methods use HTTP for reliable request-response semantics.
    WebSocket is used for receiving real-time status push updates.
    """

    def __init__(
        self,
        host: str,
        api_key: str,
        ws_port: int = DEFAULT_WS_PORT,
        http_port: int = DEFAULT_HTTP_PORT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the client."""
        self._host = host
        self._api_key = api_key
        self._ws_port = ws_port
        self._http_port = http_port
        self._session = session
        self._own_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._status_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []
        self._connected = False
        self._closing = False
        self._reconnect_attempts = 0
        self._auto_reconnect = False

    @property
    def host(self) -> str:
        """Return the controller host."""
        return self._host

    @property
    def connected(self) -> bool:
        """Return True if WebSocket is connected."""
        return self._connected and self._ws is not None and not self._ws.closed

    def on_status_update(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Register a callback for status updates.

        Returns unsubscribe callable.
        """
        self._status_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._status_callbacks:
                self._status_callbacks.remove(callback)

        return unsubscribe

    def on_disconnect(
        self,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        """Register a callback for disconnect events.

        Returns unsubscribe callable.
        """
        self._disconnect_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._disconnect_callbacks:
                self._disconnect_callbacks.remove(callback)

        return unsubscribe

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
            )
            self._own_session = True
        return self._session

    # ---- WebSocket connection management ----

    async def connect(self, auto_reconnect: bool = True) -> None:
        """Establish WebSocket connection and start listening.

        If auto_reconnect is True, automatically reconnects on disconnect
        with exponential backoff.
        """
        self._auto_reconnect = auto_reconnect
        self._closing = False
        await self._ws_connect()
        self._start_listening()

    async def _ws_connect(self) -> None:
        """Establish the WebSocket connection."""
        session = await self._ensure_session()
        ws_url = f"http://{self._host}:{self._ws_port}/"
        try:
            self._ws = await session.ws_connect(
                ws_url,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            )
        except (
            aiohttp.ClientError,
            aiohttp.WSServerHandshakeError,
            OSError,
        ) as err:
            raise LarnitechConnectionError(
                f"Cannot connect to {ws_url}: {err}"
            ) from err
        self._connected = True
        self._reconnect_attempts = 0
        _LOGGER.debug("WebSocket connected to %s", ws_url)

    async def disconnect(self) -> None:
        """Close WebSocket connection and stop all background tasks."""
        self._closing = True
        self._connected = False
        self._auto_reconnect = False
        # Cancel reconnect task
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        # Cancel listener task
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        # Close WebSocket
        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None
        # Close session if we own it
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def ws_send_json(self, data: dict[str, Any]) -> None:
        """Send a JSON message over WebSocket.

        Used to send an initial request that triggers the server
        to start pushing deviceStatusChange events.
        """
        if self._ws is None or self._ws.closed:
            raise LarnitechConnectionError("WebSocket not connected")
        data["key"] = self._api_key
        await self._ws.send_json(data)

    def _start_listening(self) -> None:
        """Start background task to listen for WebSocket messages."""
        if self._ws_task is not None and not self._ws_task.done():
            return
        self._ws_task = asyncio.create_task(self._ws_listener())

    async def _ws_listener(self) -> None:
        """Listen for WebSocket messages and dispatch to callbacks."""
        if self._ws is None:
            return
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        _LOGGER.warning(
                            "Invalid JSON from WebSocket: %s",
                            msg.data[:100],
                        )
                        continue
                    self._dispatch_status(data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error(
                        "WebSocket error: %s",
                        self._ws.exception(),
                    )
                    break
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Unexpected error in WebSocket listener")
        finally:
            self._connected = False
            self._ws_task = None
            if not self._closing:
                _LOGGER.warning("WebSocket disconnected")
                self._notify_disconnect()
                if self._auto_reconnect:
                    self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt with exponential backoff."""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        backoff_idx = min(
            self._reconnect_attempts,
            len(_RECONNECT_BACKOFF) - 1,
        )
        delay = _RECONNECT_BACKOFF[backoff_idx]
        self._reconnect_attempts += 1
        _LOGGER.debug(
            "Scheduling reconnect in %ds (attempt %d)",
            delay,
            self._reconnect_attempts,
        )
        self._reconnect_task = asyncio.create_task(self._reconnect_with_delay(delay))

    async def _reconnect_with_delay(self, delay: float) -> None:
        """Wait and then attempt to reconnect."""
        await asyncio.sleep(delay)
        if self._closing:
            return
        try:
            # Close stale WebSocket if any
            if self._ws and not self._ws.closed:
                await self._ws.close()
                self._ws = None
            await self._ws_connect()
            self._start_listening()
            _LOGGER.info("WebSocket reconnected successfully")
        except LarnitechConnectionError:
            _LOGGER.debug("Reconnect failed, will retry")
            if self._auto_reconnect and not self._closing:
                self._schedule_reconnect()

    def _dispatch_status(self, data: dict[str, Any]) -> None:
        """Dispatch a status update to all registered callbacks."""
        for callback in list(self._status_callbacks):
            try:
                callback(data)
            except Exception:
                _LOGGER.exception("Error in status callback")

    def _notify_disconnect(self) -> None:
        """Notify all disconnect callbacks."""
        for callback in list(self._disconnect_callbacks):
            try:
                callback()
            except Exception:
                _LOGGER.exception("Error in disconnect callback")

    # ---- HTTP API methods ----

    async def _http_request(
        self,
        request: dict[str, Any],
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Send a JSON request via HTTP GET and return the response.

        Uses Connection: close header and force_close connector because
        the Larnitech controller sends 'Connection: Closed' (capital C)
        which can confuse aiohttp's keep-alive handling.
        """
        session = await self._ensure_session()
        request["key"] = self._api_key
        json_str = json.dumps(request)
        url = f"http://{self._host}:{self._http_port}/?json={_url_quote(json_str)}"
        headers = {"Connection": "close"}
        try:
            async with asyncio.timeout(timeout):
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        raise LarnitechApiError(
                            f"HTTP {resp.status} from controller"
                        )
                    # Read raw text first, then parse JSON
                    # (resp.json can return None if body is empty)
                    text = await resp.text()
        except TimeoutError as err:
            raise LarnitechTimeoutError(
                f"Timeout connecting to {self._host}"
            ) from err
        except aiohttp.ClientError as err:
            raise LarnitechConnectionError(
                f"Connection error to {self._host}: {err}"
            ) from err

        if not text:
            raise LarnitechConnectionError("Empty response from controller")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise LarnitechConnectionError(
                f"Invalid JSON from controller: {text[:100]}"
            ) from err

        if isinstance(data, dict) and (error := data.get("error")):
            raise LarnitechApiError(error, error_type=error)

        return data

    # ---- Public API methods ----

    async def get_devices(self) -> list[LarnitechDevice]:
        """Get list of all devices."""
        data = await self._http_request({"requestType": "getDevicesList"})
        if not isinstance(data, dict):
            return []
        devices_raw = data.get("devices") or []
        return [LarnitechDevice.from_dict(d) for d in devices_raw]

    async def get_device_status(
        self,
        addr: str,
    ) -> LarnitechDeviceStatus:
        """Get status of a single device."""
        data = await self._http_request(
            {"requestType": "getDeviceStatus", "addr": addr}
        )
        if not isinstance(data, dict):
            return LarnitechDeviceStatus(addr=addr, type="", state="")
        return LarnitechDeviceStatus.from_dict(data.get("status") or {})

    async def get_all_statuses(self) -> list[LarnitechDeviceStatus]:
        """Get status of all devices."""
        data = await self._http_request({"requestType": "getAllDevicesStatus"})
        if not isinstance(data, dict):
            return []
        statuses_raw = data.get("statuses") or []
        return [LarnitechDeviceStatus.from_dict(s) for s in statuses_raw]

    async def set_device_status(
        self,
        addr: str,
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Set device status using named fields.

        Works for: lamp (on/off), dimmer (on/off/brightness),
        valve (open/closed), light-scheme (on/off),
        valve-heating (on/off).
        """
        return await self._http_request(
            {
                "requestType": "setDeviceStatus",
                "addr": addr,
                "status": status,
            }
        )

    async def set_device_status_raw(
        self,
        addr: str,
        state: str,
    ) -> dict[str, Any]:
        """Set device status using raw hex string.

        Works for ALL controllable device types including:
        AC, blinds, IR transmitter, scripts, and everything
        setDeviceStatus supports.
        """
        return await self._http_request(
            {
                "requestType": "setDeviceStatusRaw",
                "addr": addr,
                "status": {"state": state},
            }
        )

    async def validate_connection(self) -> int:
        """Validate we can connect and authenticate.

        Returns the number of devices found.
        Raises LarnitechAuthError if the API key is wrong.
        Raises LarnitechConnectionError if we can't reach the controller.
        Raises LarnitechTimeoutError on timeout.
        """
        try:
            devices = await self.get_devices()
        except LarnitechApiError as err:
            if "key" in str(err).lower() or "auth" in str(err).lower():
                raise LarnitechAuthError("Invalid API key") from err
            raise
        if not devices:
            # Empty device list with no error usually means wrong key
            # (the controller returns 0 devices for invalid keys)
            _LOGGER.warning("No devices returned; API key may be incorrect")
        return len(devices)

    async def send_ir_signal(
        self,
        transmitter_addr: str,
        signal_hex: str,
    ) -> dict[str, Any]:
        """Send an IR signal through a specific transmitter.

        This is a convenience method around set_device_status_raw
        that targets the IR transmitter hardware module.
        """
        return await self.set_device_status_raw(transmitter_addr, signal_hex)
