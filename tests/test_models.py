"""Tests for the models module."""

from pylarnitech.models import (
    LarnitechControllerInfo,
    LarnitechDevice,
    LarnitechDeviceStatus,
    LarnitechIRSignal,
)


class TestLarnitechDevice:
    """Tests for LarnitechDevice."""

    def test_from_dict_lamp(self) -> None:
        """Test creating a lamp device from API response."""
        data = {
            "addr": "388:3",
            "type": "lamp",
            "name": "Kitchen Light",
            "nAddr": 99331,
            "area": "Kitchen",
            "system": "no",
        }
        dev = LarnitechDevice.from_dict(data)
        assert dev.addr == "388:3"
        assert dev.type == "lamp"
        assert dev.name == "Kitchen Light"
        assert dev.n_addr == 99331
        assert dev.area == "Kitchen"
        assert dev.extra == {}

    def test_from_dict_valve_heating_with_modes(self) -> None:
        """Test creating a valve-heating device with modes in extra."""
        data = {
            "addr": "276:6",
            "type": "valve-heating",
            "name": "Radiator 6",
            "nAddr": 70662,
            "area": "Setup",
            "system": "no",
            "automation_stored": "unknown",
            "mode_named": "manual",
            "modes": [
                {"mode_named": "Eco", "setpoint_temp": "16"},
                {"mode_named": "Comfort", "setpoint_temp": "22"},
            ],
        }
        dev = LarnitechDevice.from_dict(data)
        assert dev.type == "valve-heating"
        assert "modes" in dev.extra
        assert len(dev.extra["modes"]) == 2
        assert dev.extra["mode_named"] == "manual"

    def test_from_dict_ac_with_attributes(self) -> None:
        """Test AC device with extra attributes."""
        data = {
            "addr": "407:1",
            "type": "AC",
            "name": "Office AC",
            "nAddr": 104193,
            "area": "Office",
            "system": "no",
            "t-min": "16",
            "t-delta": "16",
        }
        dev = LarnitechDevice.from_dict(data)
        assert dev.extra.get("t-min") == "16"
        assert dev.extra.get("t-delta") == "16"

    def test_from_dict_remote_with_signals(self) -> None:
        """Test remote-control device with signals."""
        data = {
            "addr": "2048:248",
            "type": "remote-control",
            "name": "TV Remote",
            "nAddr": 524536,
            "area": "Living",
            "system": "no",
            "sygnals": [
                {
                    "transmitter-addr": "288:11",
                    "value": "1964070002",
                    "name": "Power",
                },
            ],
        }
        dev = LarnitechDevice.from_dict(data)
        assert "sygnals" in dev.extra
        assert len(dev.extra["sygnals"]) == 1

    def test_from_dict_minimal(self) -> None:
        """Test creating device with minimal data."""
        dev = LarnitechDevice.from_dict({})
        assert dev.addr == ""
        assert dev.type == ""
        assert dev.name == ""
        assert dev.n_addr == 0

    def test_module_id(self) -> None:
        """Test module_id extraction."""
        dev = LarnitechDevice.from_dict({"addr": "407:1", "type": "AC", "name": ""})
        assert dev.module_id == 407

    def test_channel_id(self) -> None:
        """Test channel_id extraction."""
        dev = LarnitechDevice.from_dict({"addr": "407:1", "type": "AC", "name": ""})
        assert dev.channel_id == 1

    def test_module_id_no_colon(self) -> None:
        """Test module_id with malformed address."""
        dev = LarnitechDevice(addr="invalid", type="", name="")
        assert dev.module_id == 0


class TestLarnitechDeviceStatus:
    """Tests for LarnitechDeviceStatus."""

    def test_from_dict_lamp(self) -> None:
        """Test lamp status."""
        data = {
            "addr": "388:3",
            "type": "lamp",
            "state": "on",
            "nAddr": 99331,
        }
        status = LarnitechDeviceStatus.from_dict(data)
        assert status.state == "on"
        assert status.brightness is None

    def test_from_dict_dimmer(self) -> None:
        """Test dimmer status with brightness."""
        data = {
            "addr": "298:3",
            "type": "dimmer-lamp",
            "state": "on",
            "nAddr": 76291,
            "brightness": 50,
        }
        status = LarnitechDeviceStatus.from_dict(data)
        assert status.state == "on"
        assert status.brightness == 50

    def test_from_dict_valve_heating(self) -> None:
        """Test valve-heating status."""
        data = {
            "addr": "276:7",
            "type": "valve-heating",
            "state": "on",
            "nAddr": 70663,
            "meas_temp": "12.96",
            "setpoint_temp": "26.0",
            "mode": 1,
            "modeNamed": "Comfort",
        }
        status = LarnitechDeviceStatus.from_dict(data)
        assert status.state == "on"
        assert status.meas_temp == 12.96
        assert status.setpoint_temp == 26.0
        assert status.mode_named == "Comfort"

    def test_from_dict_ac_hex(self) -> None:
        """Test AC status with hex state."""
        data = {
            "addr": "407:1",
            "type": "AC",
            "state": "39001C620431100000",
            "nAddr": 104193,
        }
        status = LarnitechDeviceStatus.from_dict(data)
        assert status.state == "39001C620431100000"

    def test_from_dict_sensor(self) -> None:
        """Test temperature sensor status."""
        data = {
            "addr": "999:3",
            "type": "temperature-sensor",
            "state": "16.80",
        }
        status = LarnitechDeviceStatus.from_dict(data)
        assert status.state == "16.80"

    def test_meas_temp_zero(self) -> None:
        """Test meas_temp returns None for zero value."""
        status = LarnitechDeviceStatus(
            addr="", type="", state="", extra={"meas_temp": 0}
        )
        assert status.meas_temp is None

    def test_mode_named_fallback(self) -> None:
        """Test mode_named falls back to 'mode' field."""
        status = LarnitechDeviceStatus(
            addr="", type="", state="", extra={"mode": "manual"}
        )
        assert status.mode_named == "manual"

    def test_state_coerced_to_string(self) -> None:
        """Test that numeric states are coerced to string."""
        data = {"addr": "", "type": "", "state": 42}
        status = LarnitechDeviceStatus.from_dict(data)
        assert status.state == "42"
        assert isinstance(status.state, str)


class TestLarnitechIRSignal:
    """Tests for LarnitechIRSignal."""

    def test_from_dict(self) -> None:
        """Test creating IR signal from dict."""
        data = {
            "transmitter-addr": "288:11",
            "value": "196407000200A706",
            "name": "Power",
        }
        sig = LarnitechIRSignal.from_dict(data)
        assert sig.transmitter_addr == "288:11"
        assert sig.value == "196407000200A706"
        assert sig.name == "Power"

    def test_from_dict_no_name(self) -> None:
        """Test IR signal with missing name."""
        data = {"transmitter-addr": "288:11", "value": "AABB"}
        sig = LarnitechIRSignal.from_dict(data)
        assert sig.name == ""


class TestLarnitechControllerInfo:
    """Tests for LarnitechControllerInfo."""

    def test_creation(self) -> None:
        """Test creating controller info."""
        info = LarnitechControllerInfo(
            host="192.168.4.100",
            port=8080,
            api_key="7555054131",
            serial="543e5aaf",
            version="Larnitech 2.05 release",
            device_count=316,
        )
        assert info.host == "192.168.4.100"
        assert info.serial == "543e5aaf"
