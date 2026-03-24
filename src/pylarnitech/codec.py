"""Encode/decode Larnitech device state hex strings."""

from __future__ import annotations

import struct
from dataclasses import dataclass


def status_float2(low_byte: int, high_byte: int) -> float:
    """Decode a Larnitech statusFloat2 value.

    Signed 16-bit little-endian fixed-point with 8 fractional bits.
    Formula: int16_le(low, high) / 256.0
    Range: -128.0 to +127.996
    Resolution: 1/256 ≈ 0.0039
    """
    raw = struct.unpack("<h", bytes([low_byte, high_byte]))[0]
    return raw / 256.0


def encode_float2(value: float) -> tuple[int, int]:
    """Encode a float to Larnitech statusFloat2 format.

    Returns (low_byte, high_byte) tuple.
    """
    raw = int(value * 256)
    packed = struct.pack("<h", raw)
    return packed[0], packed[1]


@dataclass
class ACState:
    """Decoded AC device state.

    Temperature is stored as statusFloat2 across bytes 1-2:
      byte 1 = fractional part (0x00=.0, 0x80=.5)
      byte 2 = integer part (degrees Celsius)
      temperature = int16_le(byte1, byte2) / 256.0
    """

    power: bool
    mode: int
    temperature: float
    fan: int
    vane_horizontal: int
    vane_vertical: int
    raw: str

    @classmethod
    def from_hex(cls, hex_state: str) -> ACState:
        """Decode hex state string to ACState."""
        if not hex_state or len(hex_state) < 10:
            return cls(
                power=False,
                mode=0,
                temperature=0.0,
                fan=0,
                vane_horizontal=0,
                vane_vertical=0,
                raw=hex_state or "",
            )
        try:
            b = bytes.fromhex(hex_state)
        except ValueError:
            return cls(
                power=False, mode=0, temperature=0.0, fan=0,
                vane_horizontal=0, vane_vertical=0, raw=hex_state,
            )
        # Temperature as statusFloat2 from bytes 1-2
        temp = status_float2(b[1], b[2]) if len(b) > 2 else 0.0
        return cls(
            power=bool(b[0] & 0x01),
            mode=(b[0] >> 4) & 0x0F,
            temperature=temp,
            fan=b[4] & 0x0F if len(b) > 4 else 0,
            vane_horizontal=b[3] & 0x0F if len(b) > 3 else 0,
            vane_vertical=(b[3] >> 4) & 0x0F if len(b) > 3 else 0,
            raw=hex_state,
        )

    def to_hex(self) -> str:
        """Encode ACState back to hex string."""
        # Preserve extra bytes from the original state
        extra = self.raw[10:] if len(self.raw) > 10 else ""
        b0 = (self.mode << 4) | (0x01 if self.power else 0x00)
        # Preserve bits 1-3 from original byte 0 if available
        if self.raw and len(self.raw) >= 2:
            orig_b0 = int(self.raw[:2], 16)
            b0 = (b0 & 0xF1) | (orig_b0 & 0x0E)
        # Temperature as statusFloat2: encode to bytes 1-2
        b1, b2 = encode_float2(self.temperature)
        b3 = ((self.vane_vertical & 0x0F) << 4) | (self.vane_horizontal & 0x0F)
        b4 = self.fan & 0x0F
        return f"{b0:02x}{b1:02x}{b2:02x}{b3:02x}{b4:02x}{extra}"


@dataclass
class BlindsState:
    """Decoded blinds/cover device state."""

    command: int
    position: int
    tilt: int
    raw: str

    @classmethod
    def from_hex(cls, hex_state: str) -> BlindsState:
        """Decode hex state string to BlindsState."""
        if not hex_state or len(hex_state) < 6:
            return cls(command=0, position=0, tilt=0, raw=hex_state or "")
        try:
            b = bytes.fromhex(hex_state)
        except ValueError:
            return cls(command=0, position=0, tilt=0, raw=hex_state)
        return cls(
            command=b[0],
            position=b[1],
            tilt=b[2],
            raw=hex_state,
        )

    def to_hex(self) -> str:
        """Encode BlindsState back to hex string."""
        return f"{self.command:02x}{self.position:02x}{self.tilt:02x}"

    @property
    def position_pct(self) -> int:
        """Position as 0-100 percentage (0=closed, 100=open)."""
        return round(self.position * 100 / 250)

    @position_pct.setter
    def position_pct(self, value: int) -> None:
        """Set position from 0-100 percentage."""
        self.position = round(value * 250 / 100)

    @property
    def tilt_pct(self) -> int:
        """Tilt as 0-100 percentage."""
        return round(self.tilt * 100 / 250)

    @tilt_pct.setter
    def tilt_pct(self, value: int) -> None:
        """Set tilt from 0-100 percentage."""
        self.tilt = round(value * 250 / 100)

    @property
    def is_closed(self) -> bool:
        """Return True if fully closed."""
        return self.position == 0
