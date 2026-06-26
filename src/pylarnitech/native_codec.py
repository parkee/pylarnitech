"""Decode/encode native (TCP 55555) raw device status to match the HTTP API exactly.

The native protocol returns only the RAW status bytes (``"0xHHLL.."``); the controller's
HTTP API (``getDeviceStatus``) additionally decodes them into semantic fields (``state``
``"on"``/``"off"``, ``brightness`` 0-100, ``meas_temp``/``setpoint_temp``/``modeNamed``
for climate, float strings for sensors). For a *fully native* client to be a drop-in for
the HTTP client, it must reproduce that enrichment so the Home Assistant entities behave
identically in either mode.

Every rule was verified by capturing native ``status-get`` and HTTP ``getDeviceStatus``
for the SAME device back-to-back against a live controller. Key shapes confirmed:

    type                  native raw      HTTP enriched           rule
    lamp / light-scheme   0x08            {"state":"off"}         on = byte0 & 1
    dimmer-lamp           0x01BC          {state:on,bri:75}       bri=round(b1*100/250)
    valve                 0x00            {"state":"open"}        open = NOT(byte0 & 1)
    valve-heating         0x200019F01C00  {mode:2,modeNamed:..,   setpoint=float2(b1,b2)
                                           setpoint:"25.0",         meas=float2(b3,b4)
                                           meas:"28.96"}
    temperature/humidity  0x0011          {"state":"17.0"}        int16_le(b0,b1)/256
    illumination/motion   0x4909          {"state":"9.29"}        same float2
    door-sensor           0x01 / 0x00     "opened" / "closed"     opened = byte0 & 1
    leak-sensor           0x00            "no leakage"            leak = byte0 & 1
    AC/blinds/virtual/current/voltage     raw hex passthrough (uppercase, no "0x")
"""

from __future__ import annotations

import contextlib
import struct
from typing import Any

from .const import (
    DEVICE_TYPE_AC,
    DEVICE_TYPE_BLINDS,
    DEVICE_TYPE_CLIMATE_CONTROL,
    DEVICE_TYPE_CURRENT_SENSOR,
    DEVICE_TYPE_DIMMER_LAMP,
    DEVICE_TYPE_DOOR_SENSOR,
    DEVICE_TYPE_GATE,
    DEVICE_TYPE_HUMIDITY_SENSOR,
    DEVICE_TYPE_ILLUMINATION_SENSOR,
    DEVICE_TYPE_JALOUSIE,
    DEVICE_TYPE_LAMP,
    DEVICE_TYPE_LEAK_SENSOR,
    DEVICE_TYPE_LIGHT_SCHEME,
    DEVICE_TYPE_MOTION_SENSOR,
    DEVICE_TYPE_TEMPERATURE_SENSOR,
    DEVICE_TYPE_VALVE,
    DEVICE_TYPE_VALVE_HEATING,
    DEVICE_TYPE_VIRTUAL,
    DEVICE_TYPE_VOLTAGE_SENSOR,
)

# statusFloat2 value at/below this is the controller's "no sensor / invalid" sentinel
# (e.g. raw 0x8000 -> -128.0). The HTTP API reports such fields as 0.
_SENTINEL = -100.0

# Device types whose HTTP "state" is the RAW hex string (decoded downstream by codec.py
# / platforms).
_RAW_PASSTHROUGH = frozenset(
    {
        DEVICE_TYPE_AC,
        DEVICE_TYPE_BLINDS,
        DEVICE_TYPE_JALOUSIE,
        DEVICE_TYPE_GATE,
        DEVICE_TYPE_CLIMATE_CONTROL,
        DEVICE_TYPE_VIRTUAL,
        DEVICE_TYPE_CURRENT_SENSOR,
        DEVICE_TYPE_VOLTAGE_SENSOR,
    }
)
# Sensor types whose HTTP "state" is a decoded float string (int16_le/256).
_FLOAT_SENSORS = frozenset(
    {
        DEVICE_TYPE_TEMPERATURE_SENSOR,
        DEVICE_TYPE_HUMIDITY_SENSOR,
        DEVICE_TYPE_ILLUMINATION_SENSOR,
        DEVICE_TYPE_MOTION_SENSOR,
    }
)


def status_bytes(status: str | None) -> bytes:
    """Decode a ``"0xHHLL.."`` status string to raw bytes (empty/undefined -> b"")."""
    s = (status or "").strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    if not s or len(s) % 2:
        return b""
    try:
        return bytes.fromhex(s)
    except ValueError:
        return b""


def _float2(lo: int, hi: int) -> float:
    """Signed 16-bit little-endian fixed-point with 8 fractional bits (statusFloat2)."""
    return struct.unpack("<h", bytes([lo, hi]))[0] / 256.0


def _float2_str(lo: int, hi: int) -> str:
    """Format a statusFloat2 pair the way the controller's HTTP API does.

    Integers (low byte 0) render with one decimal ("17.0"), else two ("9.29").
    """
    v = _float2(lo, hi)
    return f"{v:.1f}" if lo == 0 else f"{v:.2f}"


def _hex_state(status: str | None) -> str:
    """Raw-hex passthrough: strip a ``"0x"`` prefix and uppercase (matches HTTP)."""
    s = (status or "").strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    return s.upper()


def decode_status(
    addr: str, dtype: str, status: str | None, device: Any | None = None
) -> dict[str, Any]:
    """Decode one native raw ``status`` into the HTTP-style status dict for ``dtype``.

    Returns a flat dict shaped like the controller's
    ``getDeviceStatus``/``deviceStatusChange`` payload: ``{"addr", "type", "state",
    ...extra}`` where extra holds ``brightness`` / ``meas_temp`` / ``setpoint_temp`` /
    ``mode`` / ``modeNamed`` as appropriate. Pass ``device`` (a ``LarnitechDevice``) so
    valve-heating can resolve ``modeNamed`` from its configured modes.
    """
    d: dict[str, Any] = {"addr": addr, "type": dtype}

    if status in (None, "", "undefined"):
        d["state"] = "undefined"
        if dtype == DEVICE_TYPE_DIMMER_LAMP:
            d["brightness"] = None
        return d

    b = status_bytes(status)

    if dtype in (DEVICE_TYPE_LAMP, DEVICE_TYPE_LIGHT_SCHEME):
        d["state"] = "on" if (b and b[0] & 1) else "off"
    elif dtype == DEVICE_TYPE_DIMMER_LAMP:
        d["state"] = "on" if (b and b[0] & 1) else "off"
        d["brightness"] = round(b[1] * 100 / 250) if len(b) >= 2 else 0
    elif dtype == DEVICE_TYPE_VALVE:
        # Water/gas valve: de-energised (byte0 bit0 = 0) = open.
        d["state"] = "closed" if (b and b[0] & 1) else "open"
    elif dtype == DEVICE_TYPE_VALVE_HEATING:
        d["state"] = "on" if (b and b[0] & 1) else "off"
        _decode_valve_heating(d, b, device)
    elif dtype == DEVICE_TYPE_DOOR_SENSOR:
        d["state"] = "opened" if (b and b[0] & 1) else "closed"
    elif dtype == DEVICE_TYPE_LEAK_SENSOR:
        d["state"] = "leakage" if (b and b[0] & 1) else "no leakage"
    elif dtype in _FLOAT_SENSORS:
        d["state"] = _float2_str(b[0], b[1]) if len(b) >= 2 else "undefined"
    elif dtype in _RAW_PASSTHROUGH:
        d["state"] = _hex_state(status)
    else:
        # Unknown/unmapped type: preserve the raw hex so nothing is lost.
        d["state"] = _hex_state(status)
    return d


def _decode_valve_heating(d: dict[str, Any], b: bytes, device: Any | None) -> None:
    """Fill meas_temp/setpoint_temp/mode/modeNamed for a valve-heating raw status.

    Layout: ``[flags|mode, setpoint_lo, setpoint_hi, meas_lo, meas_hi, ...]``.
    setpoint = float2(b1,b2); meas = float2(b3,b4); mode = byte0 >> 4 (0 = manual).
    A sentinel temp (raw 0x8000 = -128.0) means "manual / no setpoint" -> reported 0.
    """
    setpoint = _float2(b[1], b[2]) if len(b) >= 3 else _SENTINEL - 1
    meas = _float2(b[3], b[4]) if len(b) >= 5 else _SENTINEL - 1
    mode_idx = (b[0] >> 4) if b else 0

    if setpoint <= _SENTINEL or mode_idx == 0:
        d["mode"] = "manual"
        d["setpoint_temp"] = 0
        d["meas_temp"] = 0 if meas <= _SENTINEL else _float2_str(b[3], b[4])
        return

    d["mode"] = mode_idx
    d["setpoint_temp"] = _float2_str(b[1], b[2])
    d["meas_temp"] = 0 if meas <= _SENTINEL else _float2_str(b[3], b[4])
    modes = []
    if device is not None:
        modes = getattr(device, "extra", {}).get("modes") or []
    if 0 <= mode_idx < len(modes):
        name = modes[mode_idx].get("mode_named")
        if name:
            d["modeNamed"] = name


def encode_named_status(dtype: str, status: dict[str, Any]) -> str:
    """Encode HTTP-style named fields to a ``"0x"`` hex wire status for ``status-set``.

    e.g. ``{"state": "on", "brightness": 75}``. Used so ``set_device_status`` works
    natively, mirroring the controller's HTTP named-field write.
    """
    state = status.get("state")
    if dtype == DEVICE_TYPE_DIMMER_LAMP:
        if state == "off":
            return "0x00"
        bright = status.get("brightness")
        if bright is None:
            # Turn on with no level specified: send only the on-bit (1 byte). The
            # controller preserves the stored brightness byte, so this restores the
            # last level (like HTTP).
            return "0x01"
        raw = max(0, min(250, round(bright * 2.5)))
        return "0x" + bytes([0x01, raw, 0x00, 0x00]).hex().upper()
    if dtype == DEVICE_TYPE_VALVE:
        # open = de-energised (0x00), closed = energised (0x01)
        return "0x00" if state == "open" else "0x01"
    # Simple on/off devices: lamp, light-scheme, valve-heating.
    return "0x01" if state in ("on", "open", "opened") else "0x00"


def reshape_shc_item(
    attrib: dict[str, str], children: list[Any], area: str
) -> dict[str, Any]:
    """Convert a native ``get-shc`` ``<item>`` to an HTTP-style device dict.

    Shaped like the HTTP ``getDevicesList`` entry. valve-heating ``<automation>``
    children become the HTTP ``modes`` list and ``mode_named``.
    """
    d: dict[str, Any] = dict(attrib)
    d.setdefault("area", area)
    addr = d.get("addr", "")
    if ":" in addr:
        mid, sub = addr.split(":", 1)
        with contextlib.suppress(ValueError):
            d.setdefault("nAddr", int(mid) * 256 + int(sub))

    autos = [c for c in children if getattr(c, "tag", None) == "automation"]
    if autos:
        d["modes"] = [
            {
                "mode_named": c.get("name", ""),
                "setpoint_temp": c.get("temperature-level", ""),
            }
            for c in autos
        ]
        if d.get("automation"):
            d["mode_named"] = d["automation"]
    return d
