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
        assert result.hw_version == 0  # no valid version at [15:17]

    def test_gen1_double_50a(self):
        name = "PMD" + "B" * 16
        assert len(name) == 19
        result = classify_device(name)
        assert result is not None
        assert result.generation == 1
        assert result.device_type == "PMD"
        assert result.line_type == "double"
        assert result.hw_version == 0

    def test_gen1_hw_version_v1(self):
        name = "PMD" + "X" * 12 + "E2" + "XX"
        assert len(name) == 19
        result = classify_device(name)
        assert result.hw_version == 1

    def test_gen1_hw_version_v2(self):
        name = "PMS" + "X" * 12 + "E3" + "XX"
        assert len(name) == 19
        result = classify_device(name)
        assert result.hw_version == 2

    def test_gen1_hw_version_v3(self):
        name = "PMD" + "X" * 12 + "E4" + "XX"
        assert len(name) == 19
        result = classify_device(name)
        assert result.hw_version == 3

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


# ── Session liveness (the 2026-08-09 wedge) ───────────────────────────────
#
# The service sat wedged for four hours because the notification watchdog
# died (its sync callback was awaited and raised TypeError) while the session
# loop spun on a stale ``client.is_connected``.  These cover the two
# independent guards added so neither failure alone can wedge it again.


import asyncio
import time as _time

from power_watchdog_ble import NOTIFICATION_WATCHDOG_TIMEOUT, PowerWatchdogBLE


class _FakeWatchdog:
    def __init__(self, last_activity: float):
        self.last_activity = last_activity


def _bare_ble() -> PowerWatchdogBLE:
    """A PowerWatchdogBLE without running __init__ (which starts a thread)."""
    ble = object.__new__(PowerWatchdogBLE)
    ble.address = "AA:BB:CC:DD:EE:FF"
    ble._watchdog = None
    ble._connected = True
    ble._reconnect_requested = False
    ble._sleep_task = None
    return ble


class TestDataIsStale:
    def test_no_watchdog_is_not_stale(self):
        # Before a session is established there is nothing to be stale.
        assert _bare_ble()._data_is_stale() is False

    def test_fresh_activity_is_not_stale(self):
        ble = _bare_ble()
        ble._watchdog = _FakeWatchdog(_time.monotonic())
        assert ble._data_is_stale() is False

    def test_just_inside_limit_is_not_stale(self):
        ble = _bare_ble()
        limit = NOTIFICATION_WATCHDOG_TIMEOUT * PowerWatchdogBLE.STALE_DATA_FACTOR
        ble._watchdog = _FakeWatchdog(_time.monotonic() - (limit - 5))
        assert ble._data_is_stale() is False

    def test_beyond_limit_is_stale(self):
        ble = _bare_ble()
        limit = NOTIFICATION_WATCHDOG_TIMEOUT * PowerWatchdogBLE.STALE_DATA_FACTOR
        ble._watchdog = _FakeWatchdog(_time.monotonic() - (limit + 5))
        assert ble._data_is_stale() is True

    def test_stale_limit_exceeds_watchdog_timeout(self):
        # The inline check must be the backstop, never the fast path —
        # otherwise it would pre-empt the watchdog's own recovery.
        assert PowerWatchdogBLE.STALE_DATA_FACTOR > 1.0


class TestWatchdogTimeoutCallback:
    def test_callback_is_a_coroutine_function(self):
        # NotificationWatchdog awaits the return value. A plain def returns
        # None, and `await None` is the TypeError that killed the watchdog.
        assert asyncio.iscoroutinefunction(
            PowerWatchdogBLE._on_watchdog_timeout
        )

    def test_callback_requests_reconnect(self):
        ble = _bare_ble()
        asyncio.run(ble._on_watchdog_timeout())
        assert ble._reconnect_requested is True
        assert ble._connected is False

    def test_callback_survives_no_sleep_task(self):
        # Fires between sleeps: _cancel_sleep must not raise.
        ble = _bare_ble()
        ble._sleep_task = None
        asyncio.run(ble._on_watchdog_timeout())
        assert ble._reconnect_requested is True


# ── Notification watchdog ─────────────────────────────────────────────────
#
# The v1 connection manager supplied this; v2 routes connections and leaves
# noticing a silent link to the consumer, so it lives here now.

from unittest.mock import patch

from power_watchdog_ble import (  # noqa: E402
    NotificationWatchdog,
    SCAN_TIMEOUT,
    scan_for_devices,
)


def pw_ble_scanner():
    """The BleakScanner class as power_watchdog_ble sees it."""
    import power_watchdog_ble

    return power_watchdog_ble.BleakScanner



class TestNotificationWatchdog:
    def test_record_activity_moves_the_stamp_forward(self):
        wd = NotificationWatchdog(timeout=60.0, on_timeout=None)
        wd.last_activity = _time.monotonic() - 30
        before = wd.last_activity
        wd.record_activity()
        assert wd.last_activity > before

    def test_fires_after_silence(self):
        fired = []

        async def on_timeout():
            fired.append(True)

        async def scenario():
            wd = NotificationWatchdog(timeout=0.0, on_timeout=on_timeout)
            with patch("power_watchdog_ble.WATCHDOG_CHECK_INTERVAL", 0.01):
                wd.start()
                await asyncio.sleep(0.1)
            wd.stop()

        asyncio.run(scenario())
        assert fired == [True]

    def test_does_not_fire_while_notifications_arrive(self):
        fired = []

        async def on_timeout():
            fired.append(True)

        async def scenario():
            wd = NotificationWatchdog(timeout=1.0, on_timeout=on_timeout)
            with patch("power_watchdog_ble.WATCHDOG_CHECK_INTERVAL", 0.01):
                wd.start()
                for _ in range(10):
                    await asyncio.sleep(0.01)
                    wd.record_activity()
            wd.stop()

        asyncio.run(scenario())
        assert fired == []

    def test_callback_failure_does_not_escape(self):
        # The session loop's own staleness check still covers us; a raising
        # callback must not take the event loop down with it.
        async def on_timeout():
            raise RuntimeError("boom")

        async def scenario():
            wd = NotificationWatchdog(timeout=0.0, on_timeout=on_timeout)
            with patch("power_watchdog_ble.WATCHDOG_CHECK_INTERVAL", 0.01):
                wd.start()
                await asyncio.sleep(0.1)
                task = wd._task
            wd.stop()
            return task

        task = asyncio.run(scenario())
        assert task.done()
        assert task.exception() is None

    def test_stop_from_another_thread(self):
        # PowerWatchdogBLE.stop() runs on the main thread while the task
        # lives in the BLE thread's loop; a direct Task.cancel() across
        # threads is not safe.
        import threading

        wd = NotificationWatchdog(timeout=60.0, on_timeout=None)
        ready = threading.Event()
        stopped = threading.Event()
        state = {}

        async def loop_body():
            wd.start()
            ready.set()
            stopped.wait(5.0)
            await asyncio.sleep(0.05)
            state["cancelled"] = wd_task.cancelled() or wd_task.done()

        def run_loop():
            asyncio.run(loop_body())

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        ready.wait(5.0)
        wd_task = wd._task
        wd.stop()
        stopped.set()
        thread.join(5.0)

        assert state["cancelled"] is True

    def test_stop_before_start_is_harmless(self):
        NotificationWatchdog(timeout=1.0, on_timeout=None).stop()

    def test_start_is_idempotent(self):
        async def scenario():
            wd = NotificationWatchdog(timeout=60.0, on_timeout=None)
            wd.start()
            first = wd._task
            wd.start()
            assert wd._task is first
            wd.stop()

        asyncio.run(scenario())


# ── Device resolution ─────────────────────────────────────────────────────


def _ble_for_resolve(adapters=None, force_scan=False) -> PowerWatchdogBLE:
    ble = object.__new__(PowerWatchdogBLE)
    ble.address = "AA:BB:CC:DD:EE:FF"
    ble._ble_adapters = adapters
    ble._force_scan = force_scan
    return ble


class _FakeDevice:
    def __init__(self, address="AA:BB:CC:DD:EE:FF", name="WD_E7_abc123"):
        self.address = address
        self.name = name


class TestResolveDevice:
    def test_cache_hit_skips_the_scan(self):
        device = _FakeDevice()

        async def fake_get_device(address):
            return device

        with patch("power_watchdog_ble.get_device", fake_get_device), \
             patch("power_watchdog_ble.BleakScanner") as scanner:
            found = asyncio.run(_ble_for_resolve()._resolve_device())

        assert found is device
        scanner.find_device_by_address.assert_not_called()

    def test_configured_adapter_is_used_for_the_cache_lookup(self):
        seen = {}

        async def fake_by_adapter(address, adapter):
            seen["address"] = address
            seen["adapter"] = adapter
            return _FakeDevice()

        with patch(
            "power_watchdog_ble.get_device_by_adapter", fake_by_adapter,
        ):
            asyncio.run(_ble_for_resolve(["hci1"])._resolve_device())

        assert seen == {"address": "AA:BB:CC:DD:EE:FF", "adapter": "hci1"}

    def test_cache_miss_falls_back_to_a_scan(self):
        device = _FakeDevice()
        seen = {}

        async def fake_get_device(address):
            return None

        async def fake_find(address, timeout=None, **kwargs):
            seen["timeout"] = timeout
            seen["kwargs"] = kwargs
            return device

        with patch("power_watchdog_ble.get_device", fake_get_device), \
             patch.object(
                 pw_ble_scanner(), "find_device_by_address", fake_find,
             ):
            found = asyncio.run(_ble_for_resolve()._resolve_device())

        assert found is device
        assert seen["timeout"] == SCAN_TIMEOUT
        assert seen["kwargs"] == {}

    def test_scan_uses_the_configured_adapter(self):
        seen = {}

        async def fake_by_adapter(address, adapter):
            return None

        async def fake_find(address, timeout=None, **kwargs):
            seen["kwargs"] = kwargs
            return None

        with patch(
            "power_watchdog_ble.get_device_by_adapter", fake_by_adapter,
        ), patch.object(pw_ble_scanner(), "find_device_by_address", fake_find):
            asyncio.run(_ble_for_resolve(["hci1"])._resolve_device())

        assert seen["kwargs"] == {"adapter": "hci1"}

    def test_force_scan_skips_the_cache_once(self):
        # A stale BlueZ entry fails the connect the same way every time, so
        # the cycle after a not-found must go back to the radio.
        device = _FakeDevice()
        cache_calls = []

        async def fake_get_device(address):
            cache_calls.append(address)
            return _FakeDevice()

        async def fake_find(address, timeout=None, **kwargs):
            return device

        ble = _ble_for_resolve(force_scan=True)
        with patch("power_watchdog_ble.get_device", fake_get_device), \
             patch.object(
                 pw_ble_scanner(), "find_device_by_address", fake_find,
             ):
            assert asyncio.run(ble._resolve_device()) is device
            assert cache_calls == []
            # One cycle only: the next resolve is back to cache-first.
            assert ble._force_scan is False
            assert asyncio.run(ble._resolve_device()) is not device
            assert cache_calls == ["AA:BB:CC:DD:EE:FF"]

    def test_bluez_failure_falls_through_to_the_scan(self):
        # No D-Bus, no bluetoothd: say so once, then let the scan produce
        # the real error rather than giving up here.
        device = _FakeDevice()

        async def boom(address):
            raise RuntimeError("no bus")

        async def fake_find(address, timeout=None, **kwargs):
            return device

        with patch("power_watchdog_ble.get_device", boom), \
             patch.object(
                 pw_ble_scanner(), "find_device_by_address", fake_find,
             ):
            assert asyncio.run(
                _ble_for_resolve()._resolve_device()
            ) is device

# ── Discovery scan ────────────────────────────────────────────────────────


class TestScanForDevices:
    def test_classifies_and_dedupes(self):
        devices = [
            _FakeDevice("AA:BB:CC:DD:EE:01", "WD_E7_abc123"),
            _FakeDevice("AA:BB:CC:DD:EE:01", "WD_E7_abc123"),
            _FakeDevice("AA:BB:CC:DD:EE:02", "iPhone"),
        ]

        async def fake_discover(timeout=None, **kwargs):
            return devices

        with patch.object(pw_ble_scanner(), "discover", fake_discover):
            found = asyncio.run(scan_for_devices(timeout=1.0))

        assert [d.mac for d in found] == ["AA:BB:CC:DD:EE:01"]

    def test_pool_adapter_is_passed_to_the_scan(self):
        seen = {}

        async def fake_discover(timeout=None, **kwargs):
            seen["kwargs"] = kwargs
            return []

        with patch.object(pw_ble_scanner(), "discover", fake_discover):
            asyncio.run(scan_for_devices(ble_adapters=["hci1"]))

        assert seen["kwargs"] == {"adapter": "hci1"}

    def test_scan_failure_returns_empty(self):
        # Discovery runs on a timer; one failed sweep must not take the
        # service down or stop later sweeps.
        async def boom(timeout=None, **kwargs):
            raise BleakError("adapter gone")

        with patch.object(pw_ble_scanner(), "discover", boom):
            assert asyncio.run(scan_for_devices()) == []


# ── Post-connect validation ───────────────────────────────────────────────
#
# v1's establish_connection took validate_connection; v2 moved the hook onto
# the routed client, so the same guard is back with the same contract: a
# rejection is a connection failure, retried on the next radio.

from power_watchdog_ble import (  # noqa: E402
    VALIDATE_CONNECTION,
    validate_power_watchdog_gatt,
)


class TestValidatePowerWatchdogGatt:
    def test_accepts_gen2(self):
        client = _MockClient(
            [_MockSvc([_MockChar(CHARACTERISTIC_UUID_GEN2, ["notify", "write"])])],
        )
        assert asyncio.run(validate_power_watchdog_gatt(client)) is True

    def test_accepts_gen1(self):
        client = _MockClient(
            [
                _MockSvc(
                    [
                        _MockChar(CHARACTERISTIC_UUID_GEN1_TX, ["notify"]),
                        _MockChar(
                            CHARACTERISTIC_UUID_GEN1_RX,
                            ["write-without-response"],
                        ),
                    ],
                ),
            ],
        )
        assert asyncio.run(validate_power_watchdog_gatt(client)) is True

    def test_rejects_empty_gatt(self):
        # The case the validator exists for: connect succeeded, GATT is
        # empty or only carries Generic Attribute.
        assert asyncio.run(validate_power_watchdog_gatt(_MockClient([]))) is False

    def test_rejects_unknown_layout(self):
        client = _MockClient(
            [_MockSvc([_MockChar("0000180a-0000-1000-8000-00805f9b34fb", ["read"])])],
        )
        assert asyncio.run(validate_power_watchdog_gatt(client)) is False

    def test_exported_validator_tolerates_late_gatt(self):
        # v1 waited out chips that register vendor services seconds after
        # ServicesResolved; v2 makes that wrapper explicit, so it must
        # actually be applied and not silently dropped.
        assert VALIDATE_CONNECTION is not validate_power_watchdog_gatt
        assert asyncio.iscoroutinefunction(VALIDATE_CONNECTION)
