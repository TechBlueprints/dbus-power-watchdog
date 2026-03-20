"""Tests for power_watchdog_ble.py — shared data models, device classification,
and GATT resolution.

Protocol-specific tests live in test_proto_gen2.py and test_proto_gen1.py.
"""

from __future__ import annotations

import pytest

# conftest.py mocks bleak before this import
from power_watchdog_ble import (
    classify_device,
    DiscoveredDevice,
    LineData,
    WatchdogData,
    resolve_power_watchdog_gatt,
    format_gatt_snapshot,
    CHARACTERISTIC_UUID_GEN2,
    CHARACTERISTIC_UUID_GEN1_TX,
    CHARACTERISTIC_UUID_GEN1_RX,
)
from bleak import BleakError


# ── GATT resolution test helpers ──────────────────────────────────────────


class _MockChar:
    def __init__(self, uuid: str, properties: list[str]):
        self.uuid = uuid
        self.properties = properties


class _MockSvc:
    def __init__(self, characteristics: list[_MockChar]):
        self.characteristics = characteristics


class _MockClient:
    def __init__(self, services: list[_MockSvc]):
        self.services = services


class _MockSvcWithUuid:
    def __init__(self, uuid: str, characteristics: list[_MockChar]):
        self.uuid = uuid
        self.characteristics = characteristics


class TestFormatGattSnapshot:
    def test_snapshot_multiline(self):
        c = _MockClient(
            [
                _MockSvcWithUuid(
                    "0000ffe0-0000-1000-8000-00805f9b34fb",
                    [
                        _MockChar(
                            CHARACTERISTIC_UUID_GEN1_TX,
                            ["notify"],
                        ),
                    ],
                ),
            ],
        )
        text = format_gatt_snapshot(c)
        assert "0000ffe0" in text
        assert "0000ffe2" in text
        assert "notify" in text


class TestResolvePowerWatchdogGatt:
    def test_gen2(self):
        c = _MockClient(
            [
                _MockSvc(
                    [
                        _MockChar(
                            CHARACTERISTIC_UUID_GEN2,
                            ["read", "notify", "write"],
                        ),
                    ],
                ),
            ],
        )
        n, w, resp, mode = resolve_power_watchdog_gatt(c)
        assert n == w == CHARACTERISTIC_UUID_GEN2
        assert resp is True
        assert mode == "gen2"

    def test_gen1_uart(self):
        c = _MockClient(
            [
                _MockSvc(
                    [
                        _MockChar(
                            CHARACTERISTIC_UUID_GEN1_TX,
                            ["read", "notify"],
                        ),
                        _MockChar(
                            CHARACTERISTIC_UUID_GEN1_RX,
                            [
                                "read",
                                "write-without-response",
                                "write",
                            ],
                        ),
                    ],
                ),
            ],
        )
        n, w, resp, mode = resolve_power_watchdog_gatt(c)
        assert n == CHARACTERISTIC_UUID_GEN1_TX
        assert w == CHARACTERISTIC_UUID_GEN1_RX
        assert resp is False
        assert mode == "gen1_uart"

    def test_gen2_preferred_when_both_present(self):
        """If ff01 exists with notify, use gen2 even if UART UUIDs also listed."""
        c = _MockClient(
            [
                _MockSvc(
                    [
                        _MockChar(
                            CHARACTERISTIC_UUID_GEN2,
                            ["notify", "write"],
                        ),
                        _MockChar(CHARACTERISTIC_UUID_GEN1_TX, ["notify"]),
                        _MockChar(CHARACTERISTIC_UUID_GEN1_RX, ["write"]),
                    ],
                ),
            ],
        )
        n, w, _, mode = resolve_power_watchdog_gatt(c)
        assert mode == "gen2"
        assert n == w == CHARACTERISTIC_UUID_GEN2

    def test_unknown_layout(self):
        c = _MockClient([_MockSvc([_MockChar("0000180f-0000-1000-8000-00805f9b34fb", ["read"])])])
        with pytest.raises(BleakError, match="not recognized"):
            resolve_power_watchdog_gatt(c)


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
        ("WD_E5_1a2b3c4d5e6f", "E5", "single"),
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
        ("WD_E7_1a2b3c4d5e6f", "E7", "double"),
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
        assert classify_device("") is None

    def test_random_device_name(self):
        assert classify_device("iPhone") is None
        assert classify_device("SomeOtherBLE") is None

    def test_wd_prefix_wrong_parts(self):
        assert classify_device("WD_E7") is None

    def test_wd_prefix_too_many_parts(self):
        assert classify_device("WD_E7_abc_extra") is None

    def test_pm_prefix_too_short(self):
        assert classify_device("PM") is None

    def test_pm_prefix_not_19_chars(self):
        assert classify_device("PMD12345") is None
