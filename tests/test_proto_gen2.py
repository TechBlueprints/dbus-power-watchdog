"""Tests for power_watchdog_proto_gen2.py — Gen2 framed binary protocol.

Exercises the packet parser, DLData parser, notification handler,
and handshake logic without requiring a real Bluetooth adapter.
"""

from __future__ import annotations

import struct
import threading
from unittest.mock import patch

from power_watchdog_ble import (
    LineData,
    WatchdogData,
    PowerWatchdogBLE,
)
from power_watchdog_proto_gen2 import (
    Gen2Protocol,
    parse_dl_data,
    PACKET_IDENTIFIER,
    PACKET_TAIL,
    HEADER_SIZE,
    TAIL_SIZE,
    CMD_DL_REPORT,
    CMD_ERROR_REPORT,
    CMD_ALARM,
    DL_DATA_SIZE,
    MAX_BUFFER_SIZE,
)


# ── Test instance factory ──────────────────────────────────────────────────


def _make_ble_instance():
    """Create a PowerWatchdogBLE with daemon thread suppressed and Gen2 state."""
    with patch.object(PowerWatchdogBLE, "__init__", lambda self, **kw: None):
        ble = PowerWatchdogBLE.__new__(PowerWatchdogBLE)

    ble._data = WatchdogData()
    ble._data_lock = threading.Lock()
    ble._connected = False
    ble._running = False
    ble._loop = None
    ble._sleep_task = None

    proto = Gen2Protocol()
    proto.init_state(ble)
    return ble, proto


# ── Packet building helpers ────────────────────────────────────────────────


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


# ── parse_dl_data ──────────────────────────────────────────────────────────


class TestParseDlData:
    """Tests for the parse_dl_data() function."""

    def test_typical_values(self):
        body = _build_dl_data(
            voltage_v=122.3, current_a=1.77, power_w=178.0,
            energy_kwh=2652.45, frequency_hz=60.0,
        )
        result = parse_dl_data(body, 0)

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
        result = parse_dl_data(body, 0)
        assert result.voltage == 0.0
        assert result.current == 0.0
        assert result.power == 0.0
        assert result.frequency == 0.0

    def test_high_values(self):
        body = _build_dl_data(
            voltage_v=240.0, current_a=50.0, power_w=12000.0,
            energy_kwh=99999.99, frequency_hz=50.0,
        )
        result = parse_dl_data(body, 0)
        assert abs(result.voltage - 240.0) < 0.01
        assert abs(result.current - 50.0) < 0.01
        assert abs(result.power - 12000.0) < 0.01
        assert abs(result.frequency - 50.0) < 0.1

    def test_boost_flag(self):
        body = _build_dl_data(boost=True)
        result = parse_dl_data(body, 0)
        assert result.boost is True

    def test_error_code(self):
        body = _build_dl_data(error_code=5)
        result = parse_dl_data(body, 0)
        assert result.error_code == 5

    def test_offset(self):
        """Parse from an offset (as in dual-line L2 at byte 34)."""
        l1_body = _build_dl_data(voltage_v=120.0)
        l2_body = _build_dl_data(voltage_v=121.0)
        combined = l1_body + l2_body

        l2 = parse_dl_data(combined, DL_DATA_SIZE)
        assert abs(l2.voltage - 121.0) < 0.01


# ── Notification handler + packet parsing ──────────────────────────────────


class TestNotificationHandler:
    """Tests for the Gen2 notification handler and packet parser."""

    def test_single_line_packet(self):
        ble, proto = _make_ble_instance()
        body = _build_dl_data(voltage_v=122.0, current_a=1.5, power_w=183.0,
                              energy_kwh=100.0, frequency_hz=60.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        proto.notification_handler(ble, None, bytearray(packet))

        data = ble.get_data()
        assert data.timestamp > 0
        assert data.has_l2 is False
        assert abs(data.l1.voltage - 122.0) < 0.01
        assert abs(data.l1.current - 1.5) < 0.01
        assert abs(data.l1.power - 183.0) < 0.01

    def test_dual_line_packet(self):
        ble, proto = _make_ble_instance()
        l1 = _build_dl_data(voltage_v=122.0, current_a=1.77, power_w=178.0,
                            frequency_hz=60.0)
        l2 = _build_dl_data(voltage_v=123.5, current_a=0.36, power_w=7.0,
                            frequency_hz=60.0)
        body = l1 + l2
        packet = _build_packet(CMD_DL_REPORT, body)

        proto.notification_handler(ble, None, bytearray(packet))

        data = ble.get_data()
        assert data.has_l2 is True
        assert abs(data.l1.voltage - 122.0) < 0.01
        assert abs(data.l2.voltage - 123.5) < 0.01
        assert abs(data.l2.current - 0.36) < 0.01

    def test_fragmented_delivery(self):
        """Packet arrives in two separate BLE notifications."""
        ble, proto = _make_ble_instance()
        body = _build_dl_data(voltage_v=120.0, frequency_hz=60.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        mid = len(packet) // 2
        proto.notification_handler(ble, None, bytearray(packet[:mid]))
        assert ble.get_data().timestamp == 0.0

        proto.notification_handler(ble, None, bytearray(packet[mid:]))
        assert ble.get_data().timestamp > 0
        assert abs(ble.get_data().l1.voltage - 120.0) < 0.01

    def test_multiple_packets_in_one_notification(self):
        """Two complete packets arrive in a single BLE notification."""
        ble, proto = _make_ble_instance()

        body1 = _build_dl_data(voltage_v=120.0)
        body2 = _build_dl_data(voltage_v=121.0)
        combined = _build_packet(CMD_DL_REPORT, body1) + _build_packet(CMD_DL_REPORT, body2)

        proto.notification_handler(ble, None, bytearray(combined))

        data = ble.get_data()
        assert abs(data.l1.voltage - 121.0) < 0.01

    def test_garbage_before_packet(self):
        """Random bytes precede the actual packet."""
        ble, proto = _make_ble_instance()
        body = _build_dl_data(voltage_v=119.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        garbage = bytearray(b"\xDE\xAD\xBE\xEF\x00\x01\x02")
        proto.notification_handler(ble, None, garbage + bytearray(packet))

        data = ble.get_data()
        assert data.timestamp > 0
        assert abs(data.l1.voltage - 119.0) < 0.01

    def test_bad_tail(self):
        """Packet with corrupted tail marker is consumed but data is not updated."""
        ble, proto = _make_ble_instance()
        body = _build_dl_data(voltage_v=120.0)
        packet = bytearray(_build_packet(CMD_DL_REPORT, body))
        packet[-2] = 0xFF
        packet[-1] = 0xFF

        proto.notification_handler(ble, None, packet)
        assert ble.get_data().timestamp == 0.0

    def test_buffer_overflow_protection(self):
        """Buffer is cleared when it exceeds MAX_BUFFER_SIZE."""
        ble, proto = _make_ble_instance()
        ble._rx_buffer = bytearray(MAX_BUFFER_SIZE - 10)

        proto.notification_handler(ble, None, bytearray(20))
        assert len(ble._rx_buffer) == 0

    def test_error_report_does_not_crash(self):
        ble, proto = _make_ble_instance()
        body = b"\x00" * 10
        packet = _build_packet(CMD_ERROR_REPORT, body)
        proto.notification_handler(ble, None, bytearray(packet))
        assert ble.get_data().timestamp == 0.0

    def test_alarm_does_not_crash(self):
        ble, proto = _make_ble_instance()
        body = b"\x01\x02"
        packet = _build_packet(CMD_ALARM, body)
        proto.notification_handler(ble, None, bytearray(packet))
        assert ble.get_data().timestamp == 0.0

    def test_unknown_command(self):
        ble, proto = _make_ble_instance()
        body = b"\x00" * 4
        packet = _build_packet(99, body)
        proto.notification_handler(ble, None, bytearray(packet))
        assert ble.get_data().timestamp == 0.0

    def test_invalid_data_len(self):
        """Packet claiming a body length > MAX_BUFFER_SIZE is discarded."""
        ble, proto = _make_ble_instance()
        header = bytearray(HEADER_SIZE)
        struct.pack_into(">I", header, 0, PACKET_IDENTIFIER)
        header[4] = 1
        header[5] = 0
        header[6] = CMD_DL_REPORT
        struct.pack_into(">H", header, 7, MAX_BUFFER_SIZE + 1)

        proto.notification_handler(ble, None, header)
        assert ble.get_data().timestamp == 0.0

    def test_unexpected_dl_report_length(self):
        """DLReport with body length != 34 and != 68 is logged but not parsed."""
        ble, proto = _make_ble_instance()
        body = b"\x00" * 20
        packet = _build_packet(CMD_DL_REPORT, body)
        proto.notification_handler(ble, None, bytearray(packet))
        assert ble.get_data().timestamp == 0.0

    def test_get_data_thread_safety(self):
        """get_data returns a snapshot, not a reference to internal state."""
        ble, proto = _make_ble_instance()
        body = _build_dl_data(voltage_v=120.0)
        packet = _build_packet(CMD_DL_REPORT, body)

        proto.notification_handler(ble, None, bytearray(packet))
        snap1 = ble.get_data()

        body2 = _build_dl_data(voltage_v=130.0)
        packet2 = _build_packet(CMD_DL_REPORT, body2)
        proto.notification_handler(ble, None, bytearray(packet2))
        snap2 = ble.get_data()

        assert abs(snap1.l1.voltage - 120.0) < 0.01
        assert abs(snap2.l1.voltage - 130.0) < 0.01
