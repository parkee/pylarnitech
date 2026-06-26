"""Tests for the native status codec (decode/encode parity with the HTTP API).

The expected values below are the live-verified native-raw -> HTTP-enriched mappings
captured back-to-back from a real controller.
"""

from pylarnitech import LarnitechDevice
from pylarnitech.native_codec import (
    decode_status,
    encode_named_status,
    reshape_shc_item,
    status_bytes,
)


class TestStatusBytes:
    def test_with_prefix(self) -> None:
        assert status_bytes("0x01FA") == bytes([0x01, 0xFA])

    def test_without_prefix(self) -> None:
        assert status_bytes("01FA") == bytes([0x01, 0xFA])

    def test_undefined_and_empty(self) -> None:
        assert status_bytes("undefined") == b""
        assert status_bytes("") == b""
        assert status_bytes(None) == b""

    def test_odd_length(self) -> None:
        assert status_bytes("0x012") == b""


class TestDecodeStatus:
    def test_lamp_off(self) -> None:
        assert decode_status("388:3", "lamp", "0x08") == {
            "addr": "388:3",
            "type": "lamp",
            "state": "off",
        }

    def test_lamp_on(self) -> None:
        assert decode_status("1:1", "lamp", "0x01")["state"] == "on"

    def test_dimmer_off_brightness_zero(self) -> None:
        d = decode_status("475:106", "dimmer-lamp", "0x0801")
        assert d["state"] == "off"
        assert d["brightness"] == 0

    def test_dimmer_on_brightness(self) -> None:
        # 0x01BC -> on, byte1 0xBC=188 -> round(188*100/250)=75
        d = decode_status("327:1", "dimmer-lamp", "0x01BC")
        assert d["state"] == "on"
        assert d["brightness"] == 75

    def test_dimmer_undefined(self) -> None:
        d = decode_status("1:1", "dimmer-lamp", "undefined")
        assert d["state"] == "undefined"
        assert d["brightness"] is None

    def test_light_scheme(self) -> None:
        assert decode_status("451:250", "light-scheme", "0x08")["state"] == "off"

    def test_valve_open_when_deenergised(self) -> None:
        assert decode_status("279:1", "valve", "0x00")["state"] == "open"

    def test_valve_closed_when_energised(self) -> None:
        assert decode_status("279:1", "valve", "0x01")["state"] == "closed"

    def test_ac_passthrough_uppercase(self) -> None:
        d = decode_status("407:1", "AC", "0x1800130804311A0000")
        assert d["state"] == "1800130804311A0000"

    def test_blinds_passthrough(self) -> None:
        assert decode_status("426:3", "blinds", "0x090000")["state"] == "090000"

    def test_temperature_one_decimal(self) -> None:
        # 0x0011 -> int16_le(0x00,0x11)/256 = 17.0, low byte 0 -> one decimal
        assert decode_status("999:3", "temperature-sensor", "0x0011")["state"] == "17.0"

    def test_illumination_two_decimals(self) -> None:
        # 0x4909 -> int16_le(0x49,0x09)/256 = 9.285 -> "9.29"
        assert decode_status("1:1", "illumination-sensor", "0x4909")["state"] == "9.29"

    def test_humidity(self) -> None:
        assert decode_status("999:2", "humidity-sensor", "0x0053")["state"] == "83.0"

    def test_motion_off_on(self) -> None:
        # float2 little-endian: bytes [lo, hi]; 1.0 = int16 256 = bytes [0x00, 0x01]
        assert decode_status("1:1", "motion-sensor", "0x0000")["state"] == "0.0"
        assert decode_status("1:1", "motion-sensor", "0x0001")["state"] == "1.0"

    def test_door(self) -> None:
        assert decode_status("279:15", "door-sensor", "0x01")["state"] == "opened"
        assert decode_status("339:14", "door-sensor", "0x00")["state"] == "closed"

    def test_leak(self) -> None:
        assert decode_status("279:14", "leak-sensor", "0x00")["state"] == "no leakage"
        assert decode_status("279:14", "leak-sensor", "0x01")["state"] == "leakage"

    def test_virtual_passthrough(self) -> None:
        d = decode_status("999:4", "virtual", "0x3736326D6D4867")
        assert d["state"] == "3736326D6D4867"

    def test_undefined_sensor(self) -> None:
        d = decode_status("887:90", "voltage-sensor", "undefined")
        assert d["state"] == "undefined"


class TestDecodeValveHeating:
    def _device(self) -> LarnitechDevice:
        return LarnitechDevice.from_dict(
            {
                "addr": "276:7",
                "type": "valve-heating",
                "name": "Laundry",
                "modes": [
                    {"mode_named": "Eco", "setpoint_temp": "16"},
                    {"mode_named": "Comfort", "setpoint_temp": "22"},
                    {"mode_named": "Hot", "setpoint_temp": "25"},
                ],
            }
        )

    def test_active_mode_hot(self) -> None:
        # 0x200019F01C00 -> off, mode 2 (Hot), setpoint 25.0, meas float2(F0,1C)=28.94
        d = decode_status("276:7", "valve-heating", "0x200019F01C00", self._device())
        assert d["state"] == "off"
        assert d["mode"] == 2
        assert d["modeNamed"] == "Hot"
        assert d["setpoint_temp"] == "25.0"
        assert d["meas_temp"] == "28.94"

    def test_manual_mode_sentinel(self) -> None:
        # 0x0000800080FF -> manual, both temps sentinel -> 0
        d = decode_status("276:5", "valve-heating", "0x0000800080FF", self._device())
        assert d["state"] == "off"
        assert d["mode"] == "manual"
        assert d["setpoint_temp"] == 0
        assert d["meas_temp"] == 0


class TestEncodeNamedStatus:
    def test_lamp_on_off(self) -> None:
        assert encode_named_status("lamp", {"state": "on"}) == "0x01"
        assert encode_named_status("lamp", {"state": "off"}) == "0x00"

    def test_dimmer_with_brightness(self) -> None:
        # 60% -> round(60*2.5)=150=0x96 -> [0x01,0x96,0x00,0x00]
        out = encode_named_status("dimmer-lamp", {"state": "on", "brightness": 60})
        assert out == "0x01960000"

    def test_dimmer_brightness_full(self) -> None:
        out = encode_named_status("dimmer-lamp", {"state": "on", "brightness": 100})
        assert out == "0x01FA0000"

    def test_dimmer_on_no_brightness_restores(self) -> None:
        assert encode_named_status("dimmer-lamp", {"state": "on"}) == "0x01"

    def test_dimmer_off(self) -> None:
        assert encode_named_status("dimmer-lamp", {"state": "off"}) == "0x00"

    def test_valve(self) -> None:
        assert encode_named_status("valve", {"state": "open"}) == "0x00"
        assert encode_named_status("valve", {"state": "closed"}) == "0x01"

    def test_valve_heating(self) -> None:
        assert encode_named_status("valve-heating", {"state": "on"}) == "0x01"
        assert encode_named_status("valve-heating", {"state": "off"}) == "0x00"


class TestReshapeShcItem:
    class _El:
        def __init__(self, tag: str, attrib: dict[str, str]) -> None:
            self.tag = tag
            self._a = attrib

        def get(self, k: str, default: str = "") -> str:
            return self._a.get(k, default)

    def test_valve_heating_modes(self) -> None:
        attrib = {
            "addr": "276:7",
            "type": "valve-heating",
            "name": "Laundry Warm Floor",
            "automation": "Hot",
        }
        children = [
            self._El("automation", {"name": "Eco", "temperature-level": "16"}),
            self._El("automation", {"name": "Comfort", "temperature-level": "22"}),
            self._El("automation", {"name": "Hot", "temperature-level": "25"}),
        ]
        d = reshape_shc_item(attrib, children, "Laundry")
        assert d["mode_named"] == "Hot"
        assert d["modes"] == [
            {"mode_named": "Eco", "setpoint_temp": "16"},
            {"mode_named": "Comfort", "setpoint_temp": "22"},
            {"mode_named": "Hot", "setpoint_temp": "25"},
        ]
        assert d["nAddr"] == 276 * 256 + 7

    def test_plain_item(self) -> None:
        attrib = {"addr": "388:3", "type": "lamp", "name": "Lamp"}
        d = reshape_shc_item(attrib, [], "Office")
        assert d["area"] == "Office"
        assert "modes" not in d
