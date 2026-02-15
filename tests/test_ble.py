"""Tests for power_watchdog_ble.py — device classification, packet parsing,
and data models.

These tests exercise the pure logic in the BLE module without requiring
a real Bluetooth adapter or Venus OS environment.
"""

from __future__ import annotations

import struct
import time
from unittest.mock import patch

import pytest

# conftest.py mocks bleak before this import
from power_watchdog_ble import (
    classify_device,
    DiscoveredDevice,
    LineData,
    WatchdogData,
    PowerWatchdogBLE,
    DL_DATA_SIZE,
    PACKET_IDENTIFIER,
    PACKET_TAIL,
    HEADER_SIZE,
    TAIL_SIZE,
    CMD_DL_REPORT,
    CMD_ERROR_REPORT,
    CMD_ALARM,
    MAX_BUFFER_SIZE,
)


# ── Data model defaults ────────────────────────────────────────────────────


class TestLineData:
    def test_defaults(self):
        ld = LineData()
        assert ld.voltage == 0.0
        assert ld.current == 0.0
        assert ld.power == 0.0
        assert ld.energy == 0.0
        assert ld.output_voltage == 0.0
        assert ld.frequency == 0.0
        assert ld.error_code == 0
        assert ld.status == 0
        assert ld.boost is False

    def test_custom_values(self):
        ld = LineData(voltage=120.5, current=15.3, power=1843.65,
                      energy=1234.56, frequency=60.0, error_code=2,
                      boost=True)
        assert ld.voltage == 120.5
        assert ld.current == 15.3
        assert ld.power == 1843.65
        assert ld.energy == 1234.56
        assert ld.frequency == 60.0
        assert ld.error_code == 2
        assert ld.boost is True


class TestWatchdogData:
    def test_defaults(self):
        wd = WatchdogData()
        assert isinstance(wd.l1, LineData)
        assert isinstance(wd.l2, LineData)
        assert wd.has_l2 is False
        assert wd.timestamp == 0.0
        assert wd.raw_hex == ""

    def test_l1_only(self):
        wd = WatchdogData(
            l1=LineData(voltage=120.0, power=1500.0),
            has_l2=False,
            timestamp=1000.0,
        )
        assert wd.l1.voltage == 120.0
        assert wd.has_l2 is False

    def test_l1_l2(self):
        wd = WatchdogData(
            l1=LineData(voltage=120.0),
            l2=LineData(voltage=121.0),
            has_l2=True,
        )
        assert wd.has_l2 is True
        assert wd.l2.voltage == 121.0


class TestDiscoveredDevice:
    def test_defaults(self):
        dd = DiscoveredDevice(mac="AA:BB:CC:DD:EE:FF", name="test")
        assert dd.generation == 0
        assert dd.device_type == ""
        assert dd.line_type == ""

    def test_custom(self):
        dd = DiscoveredDevice(
            mac="AA:BB:CC:DD:EE:FF", name="WD_E7_abc",
            generation=2, device_type="E7", line_type="double",
        )
        assert dd.generation == 2
        assert dd.device_type == "E7"
        assert dd.line_type == "double"


# ── classify_device ─────────────────────────────────────────────────────────


class TestClassifyDevice:
    """Tests for the classify_device() pure function."""

    # ── Gen2 (WD_ prefix) ───────────────────────────────────────────────

    @pytest.mark.parametrize("name,expected_type,expected_line", [
        ("WD_E5_26ec4ae469a5", "E5", "single"),
        ("WD_E6_aabbccddeeff", "E6", "single"),
        ("WD_V5_112233445566", "V5", "single"),
        ("WD_V6_deadbeef1234", "V6", "single"),
    ])
    def test_gen2_30a_single(self, name, expected_type, expected_line):
        result = classify_device(name)
        assert result is not None
        assert result.generation == 2
        assert result.device_type == expected_type
        assert result.line_type == expected_line
        assert result.mac == ""  # caller fills this in

    @pytest.mark.parametrize("name,expected_type,expected_line", [
        ("WD_E7_26ec4ae469a5", "E7", "double"),
        ("WD_E8_aabbccddeeff", "E8", "double"),
        ("WD_E9_112233445566", "E9", "double"),
        ("WD_V7_deadbeef1234", "V7", "double"),
        ("WD_V8_abcdef012345", "V8", "double"),
        ("WD_V9_ffffffffffff", "V9", "double"),
    ])
    def test_gen2_50a_double(self, name, expected_type, expected_line):
        result = classify_device(name)
        assert result is not None
        assert result.generation == 2
        assert result.device_type == expected_type
        assert result.line_type == expected_line

    def test_gen2_unknown_model_number(self):
        result = classify_device("WD_E3_abcdef123456")
        assert result is not None
        assert result.device_type == "E3"
        assert result.line_type == "unknown"

    def test_gen2_single_char_type(self):
        result = classify_device("WD_X_abcdef123456")
        assert result is not None
        assert result.device_type == "X"
        assert result.line_type == "unknown"  # not 2 chars

    # ── Gen1 (PM prefix) ────────────────────────────────────────────────

    def test_gen1_single_30a(self):
        # PMS followed by 16 chars = 19 total
        name = "PMS" + "A" * 16
        assert len(name) == 19
        result = classify_device(name)
        assert result is not None
        assert result.generation == 1
        assert result.device_type == "PMS"
        assert result.line_type == "single"

    def test_gen1_double_50a(self):
        name = "PMD" + "B" * 16
        assert len(name) == 19
        result = classify_device(name)
        assert result is not None
        assert result.generation == 1
        assert result.device_type == "PMD"
        assert result.line_type == "double"

    def test_gen1_with_trailing_spaces(self):
        # 19-char name padded to 27 with spaces
        name = "PMD" + "C" * 16 + "        "
        assert len(name) == 27
        result = classify_device(name)
        assert result is not None
        assert result.generation == 1
        assert result.line_type == "double"

    def test_gen1_unknown_third_char(self):
        name = "PMX" + "D" * 16
        result = classify_device(name)
        assert result is not None
        assert result.line_type == "unknown"

    def test_gen1_wrong_length(self):
        # PM prefix but not 19 chars after rstrip
        assert classify_device("PMD_short") is None

    # ── Non-matching names ───────────────────────────────────────────────

    def test_empty_name(self):
        assert classify_device("") is None

    def test_none_name(self):
        # classify_device checks `if not name` first
        assert classify_device("") is None

    def test_random_device_name(self):
        assert classify_device("iPhone") is None
        assert classify_device("SomeOtherBLE") is None

    def test_wd_prefix_wrong_parts(self):
        # WD_ but only 2 parts instead of 3
        assert classify_device("WD_E7") is None

    def test_wd_prefix_too_many_parts(self):
        # WD_ with 4 parts
        assert classify_device("WD_E7_abc_extra") is None

    def test_pm_prefix_too_short(self):
        assert classify_device("PM") is None

    def test_pm_prefix_not_19_chars(self):
        assert classify_device("PMD12345") is None


# ── Packet building helpers ─────────────────────────────────────────────────


def _build_dl_data(
    voltage_v: float = 120.0,
    current_a: float = 15.0,
    power_w: float = 1800.0,
    energy_kwh: float = 1234.5,
    output_voltage_v: float = 120.0,
    frequency_hz: float = 60.0,
    error_code: int = 0,
    status: int = 0,
    boost: bool = False,
) -> bytes:
    """Build a 34-byte DLData block from human-readable values."""
    buf = bytearray(DL_DATA_SIZE)
    struct.pack_into(">i", buf, 0, int(voltage_v * 10000))
    struct.pack_into(">i", buf, 4, int(current_a * 10000))
    struct.pack_into(">i", buf, 8, int(power_w * 10000))
    struct.pack_into(">i", buf, 12, int(energy_kwh * 10000))
    struct.pack_into(">i", buf, 16, 0)  # temp1 (unused)
    struct.pack_into(">i", buf, 20, int(output_voltage_v * 10000))
    buf[24] = 0  # backlight
    buf[25] = 0  # neutralDetection
    buf[26] = 1 if boost else 0
    buf[27] = 25  # temperature
    struct.pack_into(">i", buf, 28, int(frequency_hz * 100))
    buf[32] = error_code
    buf[33] = status
    return bytes(buf)


def _build_packet(cmd: int, body: bytes) -> bytes:
    """Build a complete framed packet with header + body + tail."""
    header = bytearray(HEADER_SIZE)
    struct.pack_into(">I", header, 0, PACKET_IDENTIFIER)
    header[4] = 1   # version
    header[5] = 0   # msgId
    header[6] = cmd
    struct.pack_into(">H", header, 7, len(body))

    tail = struct.pack(">H", PACKET_TAIL)
    return bytes(header) + body + tail


# ── _parse_dl_data (static method) ─────────────────────────────────────────


class TestParseDlData:
    """Tests for PowerWatchdogBLE._parse_dl_data static method."""

    def test_typical_values(self):
        body = _build_dl_data(
            voltage_v=122.3, current_a=1.77, power_w=178.0,
            energy_kwh=2652.45, frequency_hz=60.0,
        )
        result = PowerWatchdogBLE._parse_dl_data(body, 0)

        assert abs(result.voltage - 122.3) < 0.01
        assert abs(result.current - 1.77) < 0.01
        assert abs(result.power - 178.0) < 0.01
        assert abs(result.energy - 2652.45) < 0.01
        assert abs(result.frequency - 60.0) < 0.1
        assert result.error_code == 0
        assert result.boost is False

    def test_zero_values(self):
        body = _build_dl_data(
            voltage_v=0, current_a=0, power_w=0,
            energy_kwh=0, frequency_hz=0,
        )
        result = PowerWatchdogBLE._parse_dl_data(body, 0)
        assert result.voltage == 0.0
        assert result.current == 0.0
        assert result.power == 0.0
        assert result.frequency == 0.0

    def test_high_values(self):
        body = _build_dl_data(
            voltage_v=240.0, current_a=50.0, power_w=12000.0,
            energy_kwh=99999.99, frequency_hz=50.0,
        )
        result = PowerWatchdogBLE._parse_dl_data(body, 0)
        assert abs(result.voltage - 240.0) < 0.01
        assert abs(result.current - 50.0) < 0.01
        assert abs(result.power - 12000.0) < 0.01
        assert abs(result.frequency - 50.0) < 0.1

    def test_boost_flag(self):
        body = _build_dl_data(boost=True)
        result = PowerWatchdogBLE._parse_dl_data(body, 0)
        assert result.boost is True

    def test_error_code(self):
        body = _build_dl_data(error_code=5)
        result = PowerWatchdogBLE._parse_dl_data(body, 0)
        assert result.error_code == 5

    def test_offset(self):
        """Parse from an offset (as in dual-line L2 at byte 34)."""
        l1_body = _build_dl_data(voltage_v=120.0)
        l2_body = _build_dl_data(voltage_v=121.0)
        combined = l1_body + l2_body

        l2 = PowerWatchdogBLE._parse_dl_data(combined, DL_DATA_SIZE)
        assert abs(l2.voltage - 121.0) < 0.01


# ── Packet parsing (notification handler + _try_parse_packet) ──────────────


def _make_ble_instance():
    """Create a PowerWatchdogBLE instance with the daemon thread suppressed."""
    with patch.object(PowerWatchdogBLE, "__init__", lambda self, **kw: None):
        ble = PowerWatchdogBLE.__new__(PowerWatchdogBLE)

    # Manually init the fields that the real __init__ sets
    import threading
    ble._data = WatchdogData()
    ble._data_lock = threading.Lock()
    ble._connected = False
    ble._running = False
    ble._rx_buffer = bytearray()
    ble._loop = None
    ble._sleep_task = None
    return ble


class TestNotificationHandler:
    """Tests for the notification handler and packet parser."""

    def test_single_line_packet(self):
        ble = _make_ble_instance()
        body = _build_dl_data(voltage_v=122.0, current_a=1.5, power_w=183.0,
                              energy_kwh=100.0, frequency_hz=60.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        ble._notification_handler(None, bytearray(packet))

        data = ble.get_data()
        assert data.timestamp > 0
        assert data.has_l2 is False
        assert abs(data.l1.voltage - 122.0) < 0.01
        assert abs(data.l1.current - 1.5) < 0.01
        assert abs(data.l1.power - 183.0) < 0.01

    def test_dual_line_packet(self):
        ble = _make_ble_instance()
        l1 = _build_dl_data(voltage_v=122.0, current_a=1.77, power_w=178.0,
                            frequency_hz=60.0)
        l2 = _build_dl_data(voltage_v=123.5, current_a=0.36, power_w=7.0,
                            frequency_hz=60.0)
        body = l1 + l2
        packet = _build_packet(CMD_DL_REPORT, body)

        ble._notification_handler(None, bytearray(packet))

        data = ble.get_data()
        assert data.has_l2 is True
        assert abs(data.l1.voltage - 122.0) < 0.01
        assert abs(data.l2.voltage - 123.5) < 0.01
        assert abs(data.l2.current - 0.36) < 0.01

    def test_fragmented_delivery(self):
        """Packet arrives in two separate BLE notifications."""
        ble = _make_ble_instance()
        body = _build_dl_data(voltage_v=120.0, frequency_hz=60.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        mid = len(packet) // 2
        ble._notification_handler(None, bytearray(packet[:mid]))
        # After first fragment, no complete packet yet
        assert ble.get_data().timestamp == 0.0

        ble._notification_handler(None, bytearray(packet[mid:]))
        # Now the full packet should be parsed
        assert ble.get_data().timestamp > 0
        assert abs(ble.get_data().l1.voltage - 120.0) < 0.01

    def test_multiple_packets_in_one_notification(self):
        """Two complete packets arrive in a single BLE notification."""
        ble = _make_ble_instance()

        body1 = _build_dl_data(voltage_v=120.0)
        body2 = _build_dl_data(voltage_v=121.0)
        combined = _build_packet(CMD_DL_REPORT, body1) + _build_packet(CMD_DL_REPORT, body2)

        ble._notification_handler(None, bytearray(combined))

        # The second packet should overwrite the first
        data = ble.get_data()
        assert abs(data.l1.voltage - 121.0) < 0.01

    def test_garbage_before_packet(self):
        """Random bytes precede the actual packet."""
        ble = _make_ble_instance()
        body = _build_dl_data(voltage_v=119.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        garbage = bytearray(b"\xDE\xAD\xBE\xEF\x00\x01\x02")
        ble._notification_handler(None, garbage + bytearray(packet))

        data = ble.get_data()
        assert data.timestamp > 0
        assert abs(data.l1.voltage - 119.0) < 0.01

    def test_bad_tail(self):
        """Packet with corrupted tail marker is consumed but data is not updated."""
        ble = _make_ble_instance()
        body = _build_dl_data(voltage_v=120.0)
        packet = bytearray(_build_packet(CMD_DL_REPORT, body))
        # Corrupt the tail (last 2 bytes)
        packet[-2] = 0xFF
        packet[-1] = 0xFF

        ble._notification_handler(None, packet)

        # Data should NOT be updated because tail validation failed
        assert ble.get_data().timestamp == 0.0

    def test_buffer_overflow_protection(self):
        """Buffer is cleared when it exceeds MAX_BUFFER_SIZE."""
        ble = _make_ble_instance()
        # Fill buffer just under the limit
        ble._rx_buffer = bytearray(MAX_BUFFER_SIZE - 10)

        # Push it over
        ble._notification_handler(None, bytearray(20))

        # Buffer should have been cleared
        assert len(ble._rx_buffer) == 0

    def test_error_report_does_not_crash(self):
        """CMD_ERROR_REPORT packets are handled gracefully."""
        ble = _make_ble_instance()
        body = b"\x00" * 10
        packet = _build_packet(CMD_ERROR_REPORT, body)

        ble._notification_handler(None, bytearray(packet))
        # No crash, data unchanged
        assert ble.get_data().timestamp == 0.0

    def test_alarm_does_not_crash(self):
        """CMD_ALARM packets are handled gracefully."""
        ble = _make_ble_instance()
        body = b"\x01\x02"
        packet = _build_packet(CMD_ALARM, body)

        ble._notification_handler(None, bytearray(packet))
        assert ble.get_data().timestamp == 0.0

    def test_unknown_command(self):
        """Unknown command ID is handled gracefully."""
        ble = _make_ble_instance()
        body = b"\x00" * 4
        packet = _build_packet(99, body)

        ble._notification_handler(None, bytearray(packet))
        assert ble.get_data().timestamp == 0.0

    def test_invalid_data_len(self):
        """Packet claiming a body length > MAX_BUFFER_SIZE is discarded."""
        ble = _make_ble_instance()
        header = bytearray(HEADER_SIZE)
        struct.pack_into(">I", header, 0, PACKET_IDENTIFIER)
        header[4] = 1
        header[5] = 0
        header[6] = CMD_DL_REPORT
        struct.pack_into(">H", header, 7, MAX_BUFFER_SIZE + 1)  # bogus length

        ble._notification_handler(None, header)
        # Should discard and not crash
        assert ble.get_data().timestamp == 0.0

    def test_unexpected_dl_report_length(self):
        """DLReport with body length != 34 and != 68 is logged but not parsed."""
        ble = _make_ble_instance()
        body = b"\x00" * 20  # not 34 or 68
        packet = _build_packet(CMD_DL_REPORT, body)

        ble._notification_handler(None, bytearray(packet))
        assert ble.get_data().timestamp == 0.0

    def test_get_data_thread_safety(self):
        """get_data returns a snapshot, not a reference to internal state."""
        ble = _make_ble_instance()
        body = _build_dl_data(voltage_v=120.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        ble._notification_handler(None, bytearray(packet))
        snap1 = ble.get_data()

        # Send new data
        body2 = _build_dl_data(voltage_v=130.0)
        packet2 = _build_packet(CMD_DL_REPORT, body2)
        ble._notification_handler(None, bytearray(packet2))
        snap2 = ble.get_data()

        # snap1 should still reflect the old value
        assert abs(snap1.l1.voltage - 120.0) < 0.01
        assert abs(snap2.l1.voltage - 130.0) < 0.01
