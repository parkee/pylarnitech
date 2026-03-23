"""Data models for Larnitech devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LarnitechDevice:
    """Representation of a Larnitech device from getDevicesList."""

    addr: str
    type: str
    name: str
    n_addr: int = 0
    area: str = ""
    system: str = "no"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LarnitechDevice:
        """Create from API response dict."""
        known_keys = {"addr", "type", "name", "nAddr", "area", "system"}
        return cls(
            addr=data.get("addr", ""),
            type=data.get("type", ""),
            name=data.get("name", ""),
            n_addr=data.get("nAddr", 0),
            area=data.get("area", ""),
            system=data.get("system", "no"),
            extra={k: v for k, v in data.items() if k not in known_keys},
        )

    @property
    def module_id(self) -> int:
        """Extract module ID from address."""
        return int(self.addr.split(":")[0]) if ":" in self.addr else 0

    @property
    def channel_id(self) -> int:
        """Extract channel ID from address."""
        return int(self.addr.split(":")[1]) if ":" in self.addr else 0


@dataclass
class LarnitechDeviceStatus:
    """Status of a Larnitech device from getDeviceStatus."""

    addr: str
    type: str
    state: str
    n_addr: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LarnitechDeviceStatus:
        """Create from API response dict."""
        known_keys = {"addr", "type", "state", "nAddr"}
        return cls(
            addr=data.get("addr", ""),
            type=data.get("type", ""),
            state=str(data.get("state", "")),
            n_addr=data.get("nAddr", 0),
            extra={k: v for k, v in data.items() if k not in known_keys},
        )

    @property
    def brightness(self) -> int | None:
        """Get brightness for dimmer devices."""
        return self.extra.get("brightness")

    @property
    def meas_temp(self) -> float | None:
        """Get measured temperature for climate devices."""
        val = self.extra.get("meas_temp")
        return float(val) if val is not None and val != 0 else None

    @property
    def setpoint_temp(self) -> float | None:
        """Get setpoint temperature for climate devices."""
        val = self.extra.get("setpoint_temp")
        return float(val) if val is not None and val != 0 else None

    @property
    def mode_named(self) -> str | None:
        """Get named mode for climate devices."""
        return self.extra.get("modeNamed") or self.extra.get("mode")


@dataclass
class LarnitechIRSignal:
    """An IR signal from a remote-control device."""

    transmitter_addr: str
    value: str
    name: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LarnitechIRSignal:
        """Create from API response dict."""
        return cls(
            transmitter_addr=data.get("transmitter-addr", ""),
            value=data.get("value", ""),
            name=data.get("name", ""),
        )


@dataclass
class LarnitechControllerInfo:
    """Information about the Larnitech controller."""

    host: str
    port: int
    api_key: str
    serial: str = ""
    version: str = ""
    device_count: int = 0
