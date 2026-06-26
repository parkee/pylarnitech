"""Native Larnitech protocol (TCP 55555) — full control + telemetry channel.

Why this exists: native writes are treated by the controller's automation engine as a
"manual action" (~10-min automation backoff), whereas HTTP/WS writes are overridden by
automations within ~1s. See ``NATIVE_PROTOCOL_AUTH_APPROACH.md``.

``LarnitechNativeClient`` is a drop-in replacement for
:class:`pylarnitech.LarnitechClient` (same public surface) so the Home Assistant
integration can swap transports with no other change. Reads are enriched to match the
HTTP API (see :mod:`pylarnitech.native_codec`); real-time updates arrive via the native
``status-subscribe`` push; the connection auto-reconnects with exponential backoff.

Reverse-engineered and verified live. Needs only the controller IP + the 16-char Access
key — no controller public key, no cloud, no pinning, no app.

Handshake (LIVE-VERIFIED):
  1. recv 16 bytes = ``b"AES"`` + 13-byte per-connection nonce.
  2. resp = AES-128-ECB(access_key, greeting16) (challenge-response; byte-matched app).
  3. gen a fresh client RSA-1024 keypair (e=65537).
  4. send ``[LE32 len]`` + ``b"AES"`` + resp(16) + client_pubkey_DER(140, PKCS1).
  5. recv ``[LE32 len]`` + ``b"useaes"`` + RSA_ct(128).
  6. session_key = RSA-PKCS1v15-decrypt(ct, client_priv) (16-byte AES-128 key).

Data phase (LIVE-VERIFIED): a custom AES-128-ECB channel keyed by ``session_key``. Each
frame is ``ECB( LE16(msglen+4) ‖ LE32(msglen) ‖ msg )`` padded to 16 (length is INSIDE
the encryption). ``msg`` = a 6-char command code + payload (``xmlcmd`` XML, ``-JSON-``
JSON-RPC). Unsolicited ``-JSON- {"event":"statuses",...}`` frames are server push (after
``status-subscribe``); everything else is a reply to a request (FIFO).

Requires the optional ``cryptography`` dep (``pip install 'pylarnitech[native]'``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import xml.etree.ElementTree as ET
from collections import deque
from typing import TYPE_CHECKING, Any

from . import native_codec
from .const import DEFAULT_NATIVE_PORT
from .exceptions import (
    LarnitechAuthError,
    LarnitechConnectionError,
    LarnitechError,
    LarnitechTimeoutError,
)
from .models import LarnitechDevice, LarnitechDeviceStatus

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

GREETING_LEN = 16
GREETING_MAGIC = b"AES"  # VERIFIED @ libshi3a.so 0x14ad89c + live
ACCESS_KEY_LEN = 16
CLIENT_RSA_BITS = 1024  # VERIFIED: RSA_generate_key_ex(0x400) @0x14ada50 (140-byte DER)
USEAES_TAG = b"useaes"  # controller's session-key reply tag (6 chars, plaintext-framed)
RSA_CT_LEN = 128  # RSA-1024 ciphertext length
PKFAIL = b"pkfail"
CMD_XML = "xmlcmd"  # 6-char code carrying an XML command (VERIFIED live)
CMD_JSON = "-JSON-"  # 6-char code carrying a JSON-RPC request (VERIFIED live)

# Reconnection backoff schedule (seconds) — mirrors LarnitechClient.
_RECONNECT_BACKOFF = [5, 10, 30, 60, 120, 300]


def _require_crypto() -> Any:
    """Import cryptography lazily so the base library has no hard dependency on it."""
    try:
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    except ImportError as err:  # pragma: no cover - environment dependent
        raise LarnitechError(
            "native protocol requires 'cryptography': pip install 'pylarnitech[native]'"
        ) from err
    return rsa, padding, Cipher, algorithms, modes, Encoding, PublicFormat


class LarnitechNativeClient:
    """Async drop-in client for the native protocol on TCP 55555.

    Construct with ``host`` + ``access_key`` (the controller's 16-char Access key —
    settings ``pass-key`` / admin ``AccessKeys.getWSData.key``). No controller public
    key is needed.
    """

    def __init__(
        self,
        host: str,
        access_key: str,
        port: int = DEFAULT_NATIVE_PORT,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 15.0,
    ) -> None:
        """Initialize."""
        self._host = host
        self._port = port
        key = access_key.encode() + b"0" * ACCESS_KEY_LEN
        self._access_key = key[:ACCESS_KEY_LEN]
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._client_key: Any = None
        self._session_key: bytes | None = None
        self._cipher: Any = None  # () -> cryptography Cipher bound to the session key
        self._authenticated = False

        # request/response correlation (FIFO; serialized by _req_lock)
        self._pending: deque[asyncio.Future[bytes]] = deque()
        self._req_lock = asyncio.Lock()
        self._conn_lock = asyncio.Lock()  # serialize (re)connect attempts

        # background tasks / lifecycle
        self._read_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closing = False
        self._auto_reconnect = False
        self._reconnect_attempts = 0
        self._subscribed = False

        # callbacks + device cache (device type needed to decode pushed statuses)
        self._status_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []
        self._devices: dict[str, LarnitechDevice] = {}

    # ---- properties --------------------------------------------------------

    @property
    def host(self) -> str:
        """Return the controller host."""
        return self._host

    @property
    def connected(self) -> bool:
        """True if the authenticated stream is open."""
        return (
            self._authenticated
            and self._writer is not None
            and not self._writer.is_closing()
        )

    @property
    def authenticated(self) -> bool:
        """True once the session key was received (handshake complete)."""
        return self._authenticated

    @property
    def session_key(self) -> bytes | None:
        """The 16-byte AES session key the controller delivered (after connect())."""
        return self._session_key

    # ---- callbacks ---------------------------------------------------------

    def on_status_update(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a status-update callback; returns an unsubscribe callable.

        The callback receives the same shape as the HTTP/WS push: ``{"status": {...}}``.
        """
        self._status_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._status_callbacks:
                self._status_callbacks.remove(callback)

        return unsubscribe

    def on_disconnect(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a disconnect callback; returns an unsubscribe callable."""
        self._disconnect_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._disconnect_callbacks:
                self._disconnect_callbacks.remove(callback)

        return unsubscribe

    # ---- lifecycle ---------------------------------------------------------

    async def connect(self, auto_reconnect: bool = True) -> None:
        """Open the socket, run the handshake, and start the background read loop.

        Idempotent: if already connected this only updates the auto-reconnect flag.
        """
        self._auto_reconnect = auto_reconnect
        self._closing = False
        if self.connected:
            return
        async with self._conn_lock:
            if self.connected:
                return
            await self._open_and_handshake()
            self._start_read_loop()

    async def disconnect(self) -> None:
        """Close the connection and stop all background tasks (no reconnect)."""
        self._closing = True
        self._auto_reconnect = False
        self._subscribed = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
        self._reconnect_task = None
        await self._teardown_stream(notify=False)

    # back-compat alias
    async def close(self) -> None:
        """Alias for :meth:`disconnect`."""
        await self.disconnect()

    async def __aenter__(self) -> LarnitechNativeClient:
        await self.connect(auto_reconnect=False)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    async def _open_and_handshake(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._connect_timeout,
            )
        except TimeoutError as err:
            raise LarnitechTimeoutError(
                f"timeout connecting to {self._host}:{self._port}"
            ) from err
        except OSError as err:
            raise LarnitechConnectionError(
                f"cannot connect to {self._host}:{self._port}: {err}"
            ) from err
        await self._handshake()

    async def _handshake(self) -> None:
        rsa, padding, Cipher, algorithms, modes, Encoding, PublicFormat = (
            _require_crypto()
        )

        greeting = await self._read_exactly(GREETING_LEN)
        if greeting[:3] != GREETING_MAGIC:
            raise LarnitechConnectionError(f"bad greeting: {greeting!r}")

        enc = Cipher(algorithms.AES(self._access_key), modes.ECB()).encryptor()
        resp = enc.update(greeting)
        self._client_key = rsa.generate_private_key(
            public_exponent=65537, key_size=CLIENT_RSA_BITS
        )
        client_pub_der = self._client_key.public_key().public_bytes(
            Encoding.DER, PublicFormat.PKCS1
        )
        await self._send_frame_plain(GREETING_MAGIC + resp + client_pub_der)

        reply = await self._read_frame_plain()
        if reply.rstrip(b"\x00") == PKFAIL:
            raise LarnitechAuthError("pkfail — wrong access key or malformed handshake")
        if reply[:6] != USEAES_TAG:
            raise LarnitechAuthError(f"unexpected handshake reply: {reply[:32]!r}")
        self._session_key = self._client_key.decrypt(
            reply[6 : 6 + RSA_CT_LEN], padding.PKCS1v15()
        )
        if len(self._session_key) != ACCESS_KEY_LEN:
            raise LarnitechAuthError(
                f"session key wrong length: {len(self._session_key)}"
            )

        self._cipher = lambda: Cipher(algorithms.AES(self._session_key), modes.ECB())
        self._authenticated = True
        _LOGGER.info("native: handshake complete with %s:%s", self._host, self._port)

    # ---- read loop + reconnect --------------------------------------------

    def _start_read_loop(self) -> None:
        if self._read_task is None or self._read_task.done():
            self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Continuously read frames; dispatch push events, resolve pending requests
        (FIFO)."""
        try:
            while True:
                frame = await self._read_frame_data()
                self._dispatch_frame(frame)
        except asyncio.CancelledError:
            raise
        except (LarnitechConnectionError, OSError, asyncio.IncompleteReadError):
            if not self._closing:
                _LOGGER.warning("native: read loop lost connection to %s", self._host)
        except Exception:  # noqa: BLE001 - never let the loop die silently
            if not self._closing:
                _LOGGER.exception("native: unexpected error in read loop")
        finally:
            await self._on_stream_lost()

    def _dispatch_frame(self, frame: bytes) -> None:
        code = frame[:6]
        data = frame[6:]
        if code == CMD_JSON.encode():
            start = data.find(b"{")
            if start >= 0:
                try:
                    parsed = json.loads(data[start:].decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("event") == "statuses":
                    self._dispatch_statuses(parsed.get("devices") or [])
                    return
        # otherwise it's a reply to the oldest outstanding request
        while self._pending:
            fut = self._pending.popleft()
            if not fut.done():
                fut.set_result(frame)
                return

    def _dispatch_statuses(self, devices: list[dict[str, Any]]) -> None:
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            addr = dev.get("addr")
            if not addr:
                continue
            decoded = self._decode_device(addr, dev.get("status"))
            self._notify_status(decoded)

    def _decode_device(self, addr: str, raw: str | None) -> dict[str, Any]:
        device = self._devices.get(addr)
        dtype = device.type if device else ""
        return native_codec.decode_status(addr, dtype, raw, device)

    def _notify_status(self, status_dict: dict[str, Any]) -> None:
        payload = {"status": status_dict}
        for callback in list(self._status_callbacks):
            try:
                callback(payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("native: error in status callback")

    def _notify_disconnect(self) -> None:
        for callback in list(self._disconnect_callbacks):
            try:
                callback()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("native: error in disconnect callback")

    async def _on_stream_lost(self) -> None:
        """Teardown when the read loop exits; schedule reconnect if appropriate."""
        was_auth = self._authenticated
        await self._teardown_stream(notify=was_auth and not self._closing)
        if self._auto_reconnect and not self._closing:
            self._schedule_reconnect()

    async def _teardown_stream(self, *, notify: bool) -> None:
        self._authenticated = False
        self._read_task = None
        # fail any in-flight requests so callers don't hang
        while self._pending:
            fut = self._pending.popleft()
            if not fut.done():
                fut.set_exception(LarnitechConnectionError("connection lost"))
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(self._writer.wait_closed(), timeout=5)
        self._reader = self._writer = None
        if notify:
            self._notify_disconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        idx = min(self._reconnect_attempts, len(_RECONNECT_BACKOFF) - 1)
        delay = _RECONNECT_BACKOFF[idx]
        self._reconnect_attempts += 1
        _LOGGER.debug(
            "native: reconnect in %ds (attempt %d)", delay, self._reconnect_attempts
        )
        self._reconnect_task = asyncio.create_task(self._reconnect_after(delay))

    async def _reconnect_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        if self._closing:
            return
        try:
            async with self._conn_lock:
                if self.connected or self._closing:
                    return
                await self._open_and_handshake()
                self._start_read_loop()
            self._reconnect_attempts = 0
            _LOGGER.info("native: reconnected to %s", self._host)
            if self._subscribed:
                with contextlib.suppress(LarnitechError):
                    await self._subscribe_all()
        except (LarnitechError, OSError) as err:
            _LOGGER.debug("native: reconnect failed (%s), will retry", err)
            if self._auto_reconnect and not self._closing:
                self._schedule_reconnect()

    # ---- request / response (data phase) -----------------------------------

    async def _ensure_connected(self) -> None:
        if not self.connected and not self._closing:
            await self.connect(self._auto_reconnect)

    async def _request(self, code: str, payload: bytes) -> tuple[str, bytes]:
        """Send one data-phase message; return ``(reply_code, data)`` via the read loop
        (FIFO)."""
        if not self._authenticated:
            raise LarnitechConnectionError("not connected/authenticated")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        async with self._req_lock:
            self._pending.append(fut)
            try:
                await self._send_frame_data(code.encode() + payload)
                reply = await asyncio.wait_for(fut, timeout=self._read_timeout)
            except TimeoutError as err:
                self._fail_and_reset(fut)
                raise LarnitechTimeoutError(
                    f"native request timed out ({code})"
                ) from err
            except (OSError, LarnitechConnectionError) as err:
                self._fail_and_reset(fut)
                raise LarnitechConnectionError(f"native request failed: {err}") from err
        return reply[:6].decode("ascii", "replace"), reply[6:]

    def _fail_and_reset(self, fut: asyncio.Future[bytes]) -> None:
        """Drop a failed request and reset the stream (a desynced stream must not be
        reused)."""
        with contextlib.suppress(ValueError):
            self._pending.remove(fut)
        # A timeout/desync means we can't trust frame ordering anymore — drop the
        # connection;
        # the read loop's teardown will reconnect if auto_reconnect is on.
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()

    async def request(self, xml: str) -> tuple[str, bytes]:
        """Send an ``xmlcmd`` XML request; return ``(reply_code, reply_data)``."""
        return await self._request(CMD_XML, xml.encode())

    async def json_call(self, obj: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request over ``-JSON-`` and return the parsed reply."""
        _code, data = await self._request(CMD_JSON, json.dumps(obj).encode())
        start = data.find(b"{")
        text = data[start:].decode("utf-8", "replace")
        parsed = json.loads(text) if start >= 0 else {}
        if isinstance(parsed.get("error"), dict):
            err = parsed["error"]
            raise LarnitechError(
                f"native JSON error {err.get('code')}: {err.get('description')}"
            )
        return parsed

    # ---- high-level reads (drop-in for LarnitechClient) --------------------

    async def get_id(self) -> int:
        """Return the controller's server id (``<get-id/>``)."""
        _code, data = await self.request("<get-id/>")
        return struct.unpack("<I", data[:4])[0] if len(data) >= 4 else -1

    async def get_shc_xml(self) -> str:
        """Return the full smart-house config (device tree) XML (``<get-shc/>``)."""
        await self._ensure_connected()
        _code, data = await self.request("<get-shc/>")
        start = data.find(b"<?xml")
        if start < 0:
            start = data.find(b"<smart-house")
        if start < 0:
            raise LarnitechError("no XML in get-shc reply")
        return data[start:].decode("utf-8", "replace")

    async def get_devices(self) -> list[LarnitechDevice]:
        """Get the device list (from ``<get-shc/>``), reshaped to match the HTTP API.

        Also caches device types so pushed/bulk statuses can be decoded correctly.
        """
        await self._ensure_connected()
        root = ET.fromstring(await self.get_shc_xml())
        devices: list[LarnitechDevice] = []

        def walk(el: ET.Element, area: str) -> None:
            a = el.get("name", area) if el.tag == "area" else area
            if el.tag == "item":
                d = native_codec.reshape_shc_item(el.attrib, list(el), a)
                devices.append(LarnitechDevice.from_dict(d))
            for child in el:
                walk(child, a)

        walk(root, "")
        self._devices = {dev.addr: dev for dev in devices}
        return devices

    async def get_all_statuses(self) -> list[LarnitechDeviceStatus]:
        """Get every device's current status in one call (``status-get`` ``all=1``),
        enriched."""
        await self._ensure_connected()
        if not self._devices:
            await self.get_devices()
        reply = await self.json_call({"request": "status-get", "all": 1})
        out: list[LarnitechDeviceStatus] = []
        for dev in reply.get("devices") or []:
            if not isinstance(dev, dict):
                continue
            addr = dev.get("addr")
            if not addr:
                continue
            decoded = self._decode_device(addr, dev.get("status"))
            out.append(LarnitechDeviceStatus.from_dict(decoded))
        return out

    async def get_device_status(self, addr: str) -> LarnitechDeviceStatus:
        """Get one device's current status (enriched to match the HTTP API)."""
        await self._ensure_connected()
        raw = await self.get_status_raw(addr)
        return LarnitechDeviceStatus.from_dict(self._decode_device(addr, raw))

    async def get_status_raw(self, addr: str) -> str:
        """Read one device's RAW status string (``status-get``), e.g. ``"0x017D"``."""
        reply = await self.json_call({"request": "status-get", "addr": addr})
        devs = reply.get("devices") or []
        if devs and isinstance(devs[0], dict):
            return str(devs[0].get("status", ""))
        return ""

    async def validate_connection(self) -> int:
        """Connect, authenticate, and return the device count.

        Raises :class:`LarnitechAuthError` on a bad access key, or
        :class:`LarnitechConnectionError` / :class:`LarnitechTimeoutError` if the
        controller is unreachable.
        """
        await self.connect(auto_reconnect=False)
        return len(await self.get_devices())

    async def ws_send_json(self, data: dict[str, Any]) -> None:
        """Subscribe to real-time status push (the HTTP client's WS-subscribe analogue).

        The ``data`` argument is ignored; a single bare ``status-subscribe`` subscribes
        the
        session to ALL device changes. The subscribe reply's current statuses are
        dispatched immediately so entities seed without waiting for the next change.
        """
        await self._ensure_connected()
        await self._subscribe_all()

    async def _subscribe_all(self) -> None:
        reply = await self.json_call({"request": "status-subscribe"})
        self._subscribed = True
        self._dispatch_statuses(reply.get("devices") or [])

    # ---- high-level writes (earn the automation "manual action" backoff) ---

    async def set_device_status(
        self, addr: str, status: dict[str, Any]
    ) -> dict[str, Any]:
        """Set a device's status using named fields, e.g. ``{"state": "on"}``.

        Translates the named fields to the raw wire status for the device's type, then
        sends ``status-set``. Mirrors :meth:`LarnitechClient.set_device_status`.
        """
        await self._ensure_connected()
        device = self._devices.get(addr)
        dtype = device.type if device else ""
        wire = native_codec.encode_named_status(dtype, status)
        return await self.status_set(addr, wire)

    async def set_device_status_raw(self, addr: str, state: str) -> dict[str, Any]:
        """Set a device's status using a raw hex string (any controllable type).

        ``state`` is hex with or without a ``"0x"`` prefix (e.g. an
        ``ACState``/``BlindsState`` ``to_hex()`` value, or an IR signal). The mandatory
        ``"0x"`` prefix is added automatically.
        """
        await self._ensure_connected()
        return await self.status_set(addr, state)

    async def send_ir_signal(
        self, transmitter_addr: str, signal_hex: str
    ) -> dict[str, Any]:
        """Send an IR signal through a transmitter (raw status write)."""
        return await self.set_device_status_raw(transmitter_addr, signal_hex)

    async def status_set(self, addr: str, status: str) -> dict[str, Any]:
        """Low-level ``status-set``: ``status`` is ``"0x"`` + hex of the wire bytes.

        A bare hex string (no ``"0x"``) is normalised here. The ``"0x"`` prefix is
        mandatory on the wire: without it the controller copies the literal ASCII
        characters as the status bytes instead of hex-decoding (verified in
        ``libshi3a.so`` @0x13a4d40 — the source of the long-standing dimmer mis-parse).
        """
        s = status.strip()
        if s[:2].lower() != "0x":
            s = "0x" + s
        return await self.json_call(
            {"request": "status-set", "addr": addr, "status": s}
        )

    async def set_dimmer(
        self, addr: str, percent: int, *, fade: int = 0, on: bool = True
    ) -> dict[str, Any]:
        """Set a dimmer to ``percent`` (0-100) brightness, optional ``fade`` seconds.

        Wirestatus=``"0x"+hex([on,brightness,fade,0])``wherebrightness=
        ``round(pct*2.5)``
        on the 0..250 scale (``0xFA``=100%).
        """
        bright = max(0, min(250, round(percent * 2.5)))
        status = bytes([0x01 if on else 0x00, bright, fade & 0xFF, 0x00]).hex().upper()
        return await self.status_set(addr, "0x" + status)

    async def set_switch(self, addr: str, on: bool) -> dict[str, Any]:
        """Turn a relay/lamp/switch on (``"0x01"``) or off (``"0x00"``)."""
        return await self.status_set(addr, "0x01" if on else "0x00")

    # ---- decode helpers (kept for direct callers) --------------------------

    @staticmethod
    def status_bytes(status: str) -> bytes:
        """Decode a ``"0xHHLL.."`` status string into its raw bytes."""
        return native_codec.status_bytes(status)

    @classmethod
    def dimmer_percent(cls, status: str) -> int | None:
        """Brightness percent (0-100) from a dimmer status string, or ``None`` if
        undecodable."""
        b = native_codec.status_bytes(status)
        if len(b) < 2:
            return None
        if not (b[0] & 1):
            return 0
        return round(b[1] * 100 / 250)

    # ---- framing -----------------------------------------------------------
    # Handshake frames: [LE32 len][plaintext payload]  (VERIFIED).
    # Data frames: ECB( LE16(msglen+4) ‖ LE32(msglen) ‖ msg ) padded to 16; reply same
    # shape.

    def _ecb_enc(self, data: bytes) -> bytes:
        if len(data) % 16:
            data = data + b"\x00" * (16 - len(data) % 16)
        return self._cipher().encryptor().update(data)

    def _ecb_dec(self, data: bytes) -> bytes:
        return self._cipher().decryptor().update(data[: len(data) // 16 * 16])

    async def _read_exactly(self, n: int) -> bytes:
        if self._reader is None:
            raise LarnitechConnectionError("stream not open")
        try:
            return await self._reader.readexactly(n)
        except asyncio.IncompleteReadError as err:
            raise LarnitechConnectionError("connection closed mid-frame") from err

    async def _read_frame_plain(self) -> bytes:
        (length,) = struct.unpack("<I", await self._read_exactly(4))
        return await self._read_exactly(length)

    async def _send_frame_plain(self, payload: bytes) -> None:
        if self._writer is None:
            raise LarnitechConnectionError("stream not open")
        self._writer.write(struct.pack("<I", len(payload)) + payload)
        await self._writer.drain()

    async def _send_frame_data(self, msg: bytes) -> None:
        if self._writer is None:
            raise LarnitechConnectionError("stream not open")
        inner = struct.pack("<H", len(msg) + 4) + struct.pack("<I", len(msg)) + msg
        self._writer.write(self._ecb_enc(inner))
        await self._writer.drain()

    async def _read_frame_data(self) -> bytes:
        first = self._ecb_dec(await self._read_exactly(16))
        content_len = struct.unpack("<H", first[:2])[0]
        payload_len = struct.unpack("<I", first[2:6])[0]
        total_ct = ((2 + content_len + 15) // 16) * 16
        rest = b""
        if total_ct > 16:
            rest = self._ecb_dec(await self._read_exactly(total_ct - 16))
        return (first + rest)[6 : 6 + payload_len]
