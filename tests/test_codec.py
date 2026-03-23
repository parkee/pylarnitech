"""Tests for the codec module."""

from pylarnitech.codec import ACState, BlindsState, encode_float2, status_float2


class TestStatusFloat2:
    """Tests for statusFloat2 encode/decode."""

    def test_decode_28_degrees(self) -> None:
        """Test decoding 28.0°C."""
        assert status_float2(0x00, 0x1C) == 28.0

    def test_decode_12_96_degrees(self) -> None:
        """Test decoding 12.96°C (valve-heating meas_temp)."""
        result = status_float2(0xF6, 0x0C)
        assert abs(result - 12.9609375) < 0.001

    def test_decode_16_19_degrees(self) -> None:
        """Test decoding ~16.19°C (AC current temp)."""
        result = status_float2(0x31, 0x10)
        assert abs(result - 16.19140625) < 0.001

    def test_decode_zero(self) -> None:
        """Test decoding 0.0°C."""
        assert status_float2(0x00, 0x00) == 0.0

    def test_decode_negative(self) -> None:
        """Test decoding negative temperature."""
        result = status_float2(0x00, 0xFF)
        assert result == -1.0

    def test_decode_max_positive(self) -> None:
        """Test maximum positive value."""
        result = status_float2(0xFF, 0x7F)
        assert abs(result - 127.99609375) < 0.001

    def test_encode_28_degrees(self) -> None:
        """Test encoding 28.0°C."""
        low, high = encode_float2(28.0)
        assert low == 0x00
        assert high == 0x1C

    def test_encode_zero(self) -> None:
        """Test encoding 0.0°C."""
        low, high = encode_float2(0.0)
        assert low == 0
        assert high == 0

    def test_roundtrip(self) -> None:
        """Test encode then decode returns original value."""
        for temp in [0.0, 22.0, 28.0, -5.0, 16.5]:
            low, high = encode_float2(temp)
            decoded = status_float2(low, high)
            assert abs(decoded - temp) < 0.01, f"Roundtrip failed for {temp}"


class TestACState:
    """Tests for AC state encoding/decoding."""

    def test_decode_real_state(self) -> None:
        """Test decoding a real AC state from the controller."""
        ac = ACState.from_hex("39001C620431100000")
        assert ac.power is True
        assert ac.mode == 3  # Heat (verified: 0=Fan, 1=Cool, 2=Dry, 3=Heat, 4=Auto)
        assert ac.temperature == 28.0
        assert ac.fan == 4
        assert ac.vane_horizontal == 2
        assert ac.vane_vertical == 6

    def test_decode_off_state(self) -> None:
        """Test decoding AC in off state."""
        ac = ACState.from_hex("38001C620331")
        assert ac.power is False
        assert ac.mode == 3
        assert ac.temperature == 28.0

    def test_decode_empty(self) -> None:
        """Test decoding empty string."""
        ac = ACState.from_hex("")
        assert ac.power is False
        assert ac.temperature == 0.0

    def test_decode_short(self) -> None:
        """Test decoding too-short string."""
        ac = ACState.from_hex("3900")
        assert ac.power is False
        assert ac.raw == "3900"

    def test_decode_none(self) -> None:
        """Test decoding None-ish input."""
        ac = ACState.from_hex("")
        assert ac.power is False

    def test_encode_roundtrip(self) -> None:
        """Test decode then encode preserves state."""
        original = "39001C620431100000"
        ac = ACState.from_hex(original)
        encoded = ac.to_hex()
        assert encoded == original.lower()

    def test_encode_preserves_extra_bytes(self) -> None:
        """Test that encoding preserves bytes beyond the 5 core bytes."""
        original = "38001C6203001B0000"
        ac = ACState.from_hex(original)
        ac.temperature = 25
        encoded = ac.to_hex()
        # Byte 2 should change to 0x19 (25 decimal)
        assert encoded[4:6] == "19"
        # Extra bytes (after byte 4, i.e., chars 10+) should be preserved
        assert encoded[10:] == "001B0000"

    def test_encode_power_toggle(self) -> None:
        """Test toggling power on/off."""
        ac = ACState.from_hex("38001C620331")
        assert ac.power is False
        ac.power = True
        encoded = ac.to_hex()
        b0 = int(encoded[:2], 16)
        assert b0 & 0x01 == 1

    def test_encode_mode_change(self) -> None:
        """Test changing AC mode."""
        ac = ACState.from_hex("39001C620431100000")
        ac.mode = 1  # Cool (verified mapping)
        encoded = ac.to_hex()
        b0 = int(encoded[:2], 16)
        assert (b0 >> 4) & 0xF == 1

    def test_all_modes(self) -> None:
        """Test all AC mode values decode correctly."""
        for mode in range(5):
            b0 = (mode << 4) | 0x01  # power on
            hex_state = f"{b0:02x}001C620431"
            ac = ACState.from_hex(hex_state)
            assert ac.mode == mode
            assert ac.power is True

    def test_vane_positions(self) -> None:
        """Test vane position encoding in byte 3."""
        # Byte 3 = (vane_v << 4) | vane_h
        for vh in range(8):
            for vv in range(8):
                b3 = (vv << 4) | vh
                hex_state = f"3900{0x1C:02x}{b3:02x}04"
                ac = ACState.from_hex(hex_state)
                assert ac.vane_horizontal == vh
                assert ac.vane_vertical == vv


class TestBlindsState:
    """Tests for blinds state encoding/decoding."""

    def test_decode_fully_open(self) -> None:
        """Test decoding fully open blinds."""
        bl = BlindsState.from_hex("00FAFA")
        assert bl.command == 0
        assert bl.position == 250
        assert bl.tilt == 250
        assert bl.position_pct == 100
        assert bl.tilt_pct == 100

    def test_decode_fully_closed(self) -> None:
        """Test decoding fully closed blinds."""
        bl = BlindsState.from_hex("000000")
        assert bl.command == 0
        assert bl.position == 0
        assert bl.tilt == 0
        assert bl.is_closed is True

    def test_decode_half_open(self) -> None:
        """Test decoding half-open blinds."""
        bl = BlindsState.from_hex("007D7D")
        assert bl.position == 125
        assert bl.position_pct == 50
        assert bl.is_closed is False

    def test_decode_moving(self) -> None:
        """Test decoding blinds in motion."""
        bl = BlindsState.from_hex("08FAFA")
        assert bl.command == 8
        assert bl.position == 250

    def test_decode_empty(self) -> None:
        """Test decoding empty string."""
        bl = BlindsState.from_hex("")
        assert bl.position == 0
        assert bl.is_closed is True

    def test_decode_short(self) -> None:
        """Test decoding too-short string."""
        bl = BlindsState.from_hex("00FA")
        assert bl.position == 0
        assert bl.raw == "00FA"

    def test_encode_roundtrip(self) -> None:
        """Test decode then encode."""
        original = "00fafa"
        bl = BlindsState.from_hex(original)
        assert bl.to_hex() == original

    def test_position_pct_setter(self) -> None:
        """Test setting position via percentage."""
        bl = BlindsState.from_hex("000000")
        bl.position_pct = 50
        assert bl.position == 125
        bl.position_pct = 100
        assert bl.position == 250
        bl.position_pct = 0
        assert bl.position == 0

    def test_tilt_pct_setter(self) -> None:
        """Test setting tilt via percentage."""
        bl = BlindsState.from_hex("000000")
        bl.tilt_pct = 50
        assert bl.tilt == 125

    def test_encode_with_command(self) -> None:
        """Test encoding with different command values."""
        bl = BlindsState(command=1, position=125, tilt=200, raw="")
        assert bl.to_hex() == "017dc8"
