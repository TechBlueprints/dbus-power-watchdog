"""Tests for power_watchdog_proto_gen1.py — Gen1 raw Modbus-style protocol.

Exercises the two-chunk state machine, 40-byte telemetry parser, and
L1/L2 line detection without requiring a real Bluetooth adapter.
"""

from __future__ import annotations

import struct
import threading
from unittest.mock import patch

from power_watchdog_ble import (
    WatchdogData,
    PowerWatchdogBLE,
)
from power_watchdog_proto_gen1 import (
    Gen1Protocol,
    parse_gen1_telemetry,
    GEN1_CHUNK_SIZE,
    GEN1_MERGED_SIZE,
    GEN1_HEADER,
)


# ── Test instance factory ──────────────────────────────────────────────────


def _make_ble_instance():
    """Create a PowerWatchdogBLE with daemon thread suppressed and Gen1 state."""
    with patch.object(PowerWatchdogBLE, "__init__", lambda self, **kw: None):
        ble = PowerWatchdogBLE.__new__(PowerWatchdogBLE)

    ble._data = WatchdogData()
    ble._data_lock = threading.Lock()
    ble._connected = False
    ble._running = False
    ble._loop = None
    ble._sleep_task = None

    proto = Gen1Protocol()
    proto.init_state(ble)
    return ble, proto


# ── Telemetry building helpers ─────────────────────────────────────────────


def _build_gen1_merged(
    voltage_v: float = 120.0,
    current_a: float = 15.0,
    power_w: float = 1800.0,
    energy_kwh: float = 100.0,
    frequency_hz: float = 60.0,
    error_code: int = 0,
    line_markers: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
    """Build a 40-byte Gen1 merged telemetry buffer."""
    buf = bytearray(GEN1_MERGED_SIZE)
    buf[0:3] = GEN1_HEADER
    struct.pack_into(">I", buf, 3, int(voltage_v * 10000))
    struct.pack_into(">I", buf, 7, int(current_a * 10000))
    struct.pack_into(">I", buf, 11, int(power_w * 10000))
    struct.pack_into(">I", buf, 15, int(energy_kwh * 10000))
    buf[19] = error_code
    struct.pack_into(">I", buf, 31, int(frequency_hz * 100))
    buf[37], buf[38], buf[39] = line_markers
    return bytes(buf)


def _gen1_chunks(merged: bytes) -> tuple[bytearray, bytearray]:
    """Split a 40-byte merged buffer into two 20-byte BLE notification chunks."""
    return bytearray(merged[:20]), bytearray(merged[20:])


# ── Telemetry parser ──────────────────────────────────────────────────────


class TestGen1TelemetryParser:
    """Tests for parse_gen1_telemetry (the 40-byte merged buffer parser)."""

    def test_typical_values(self):
        ble, _ = _make_ble_instance()
        merged = _build_gen1_merged(
            voltage_v=122.3, current_a=1.77, power_w=215.7,
            energy_kwh=50.0, frequency_hz=60.0, error_code=3,
        )
        parse_gen1_telemetry(ble, merged)
        data = ble.get_data()
        assert data.timestamp > 0
        assert abs(data.l1.voltage - 122.3) < 0.01
        assert abs(data.l1.current - 1.77) < 0.01
        assert abs(data.l1.power - 215.7) < 0.01
        assert abs(data.l1.energy - 50.0) < 0.01
        assert abs(data.l1.frequency - 60.0) < 0.1
        assert data.l1.error_code == 3

    def test_error_code_zero_by_default(self):
        ble, _ = _make_ble_instance()
        merged = _build_gen1_merged(voltage_v=120.0)
        parse_gen1_telemetry(ble, merged)
        assert ble.get_data().l1.error_code == 0

    def test_zero_values(self):
        ble, _ = _make_ble_instance()
        merged = _build_gen1_merged(
            voltage_v=0, current_a=0, power_w=0,
            energy_kwh=0, frequency_hz=0,
        )
        parse_gen1_telemetry(ble, merged)
        data = ble.get_data()
        assert data.l1.voltage == 0.0
        assert data.l1.frequency == 0.0

    def test_wrong_buffer_size_ignored(self):
        ble, _ = _make_ble_instance()
        parse_gen1_telemetry(ble, b"\x00" * 30)
        assert ble.get_data().timestamp == 0.0


# ── Notification handler (two-chunk state machine) ────────────────────────


class TestGen1NotificationHandler:
    """Tests for the Gen1 two-chunk state machine."""

    def test_two_chunk_assembly(self):
        ble, proto = _make_ble_instance()
        merged = _build_gen1_merged(voltage_v=119.5, frequency_hz=60.0)
        chunk1, chunk2 = _gen1_chunks(merged)

        proto.notification_handler(ble, None, chunk1)
        assert ble.get_data().timestamp == 0.0

        proto.notification_handler(ble, None, chunk2)
        data = ble.get_data()
        assert data.timestamp > 0
        assert abs(data.l1.voltage - 119.5) < 0.01

    def test_second_chunk_without_first_ignored(self):
        ble, proto = _make_ble_instance()
        merged = _build_gen1_merged(voltage_v=120.0)
        _, chunk2 = _gen1_chunks(merged)

        proto.notification_handler(ble, None, chunk2)
        assert ble.get_data().timestamp == 0.0

    def test_duplicate_first_chunk_resets_state(self):
        """If two first-chunks arrive in a row, only the latest is kept."""
        ble, proto = _make_ble_instance()
        m1 = _build_gen1_merged(voltage_v=100.0)
        m2 = _build_gen1_merged(voltage_v=120.0)
        c1a, _ = _gen1_chunks(m1)
        c1b, c2b = _gen1_chunks(m2)

        proto.notification_handler(ble, None, c1a)
        proto.notification_handler(ble, None, c1b)  # replaces first
        proto.notification_handler(ble, None, c2b)

        data = ble.get_data()
        assert abs(data.l1.voltage - 120.0) < 0.01

    def test_wrong_size_chunk_ignored(self):
        ble, proto = _make_ble_instance()
        proto.notification_handler(ble, None, bytearray(15))
        assert ble.get_data().timestamp == 0.0

    def test_sequential_updates(self):
        ble, proto = _make_ble_instance()

        m1 = _build_gen1_merged(voltage_v=120.0)
        c1, c2 = _gen1_chunks(m1)
        proto.notification_handler(ble, None, c1)
        proto.notification_handler(ble, None, c2)
        assert abs(ble.get_data().l1.voltage - 120.0) < 0.01

        m2 = _build_gen1_merged(voltage_v=121.5)
        c1, c2 = _gen1_chunks(m2)
        proto.notification_handler(ble, None, c1)
        proto.notification_handler(ble, None, c2)
        assert abs(ble.get_data().l1.voltage - 121.5) < 0.01


# ── L1/L2 line detection ─────────────────────────────────────────────────


class TestGen1LineDetection:
    """Tests for L1/L2 detection using line marker bytes."""

    def test_single_line_default_l1(self):
        """Single-line device: markers (0,0,0) always means L1."""
        ble, _ = _make_ble_instance()
        merged = _build_gen1_merged(voltage_v=120.0, line_markers=(0, 0, 0))
        parse_gen1_telemetry(ble, merged)
        data = ble.get_data()
        assert data.has_l2 is False
        assert abs(data.l1.voltage - 120.0) < 0.01

    def test_v2v3_dual_line(self):
        """v2/v3 50A: markers (1,1,1) = L2, anything else = L1."""
        ble, _ = _make_ble_instance()

        m1 = _build_gen1_merged(voltage_v=120.0, line_markers=(1, 0, 0))
        parse_gen1_telemetry(ble, m1)
        assert ble.get_data().has_l2 is True
        assert abs(ble.get_data().l1.voltage - 120.0) < 0.01

        m2 = _build_gen1_merged(voltage_v=121.0, line_markers=(1, 1, 1))
        parse_gen1_telemetry(ble, m2)
        data = ble.get_data()
        assert abs(data.l2.voltage - 121.0) < 0.01
        assert abs(data.l1.voltage - 120.0) < 0.01

    def test_v2v3_zero_markers_are_l1(self):
        """v2/v3 50A: (0,0,0) must remain L1 even after (1,1,1) is seen."""
        ble, _ = _make_ble_instance()

        # L2 frame locks version inference
        m1 = _build_gen1_merged(voltage_v=121.0, line_markers=(1, 1, 1))
        parse_gen1_telemetry(ble, m1)
        assert ble._gen1_is_v2v3 is True

        # (0,0,0) frame must be treated as L1, NOT L2
        m2 = _build_gen1_merged(voltage_v=120.0, line_markers=(0, 0, 0))
        parse_gen1_telemetry(ble, m2)
        data = ble.get_data()
        assert abs(data.l1.voltage - 120.0) < 0.01
        assert abs(data.l2.voltage - 121.0) < 0.01

    def test_v1_dual_line_after_nonzero_marker(self):
        """v1 50A: (0,0,0) = L2 but only once dual-line is detected."""
        ble, _ = _make_ble_instance()

        m1 = _build_gen1_merged(voltage_v=120.0, line_markers=(1, 0, 0))
        parse_gen1_telemetry(ble, m1)
        assert ble.get_data().has_l2 is True

        m2 = _build_gen1_merged(voltage_v=119.0, line_markers=(0, 0, 0))
        parse_gen1_telemetry(ble, m2)
        data = ble.get_data()
        assert abs(data.l2.voltage - 119.0) < 0.01
