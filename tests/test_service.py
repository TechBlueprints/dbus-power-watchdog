"""Tests for dbus-power-watchdog.py — formatters, slider math, config,
adapter detection, MAC conversion, and grid update logic.

The main service file has a non-importable filename (dashes), so we
use importlib to load it.  Heavy Venus OS integration (D-Bus, GLib) is
mocked; we focus on the pure-logic helpers.
"""

from __future__ import annotations

import configparser
import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# conftest.py mocks bleak / dbus / gi / vedbus / settingsdevice
# before we import the production module.

_SERVICE_FILE = str(
    Path(__file__).resolve().parent.parent / "dbus-power-watchdog.py"
)

# Ensure the project root is on sys.path so the BLE module can resolve
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_service_module():
    """Import dbus-power-watchdog.py as a module named 'pw_service'."""
    spec = importlib.util.spec_from_file_location("pw_service", _SERVICE_FILE)
    mod = importlib.util.module_from_spec(spec)
    # Prevent main() from running if __name__ guard is present
    sys.modules["pw_service"] = mod
    spec.loader.exec_module(mod)
    return mod


pw = _load_service_module()


# ── D-Bus value formatters ──────────────────────────────────────────────────


class TestFmtW:
    def test_zero(self):
        assert pw._fmt_w("/Ac/Power", 0) == "0W"

    def test_integer(self):
        assert pw._fmt_w("/Ac/Power", 1500) == "1500W"

    def test_fractional(self):
        assert pw._fmt_w("/Ac/Power", 1234.56) == "1235W"

    def test_negative(self):
        assert pw._fmt_w("/Ac/Power", -300) == "-300W"

    def test_none(self):
        assert pw._fmt_w("/Ac/Power", None) == "---"


class TestFmtV:
    def test_nominal(self):
        assert pw._fmt_v("/Ac/Voltage", 120.3) == "120.3V"

    def test_zero(self):
        assert pw._fmt_v("/Ac/Voltage", 0) == "0.0V"

    def test_none(self):
        assert pw._fmt_v("/Ac/Voltage", None) == "---"


class TestFmtA:
    def test_nominal(self):
        assert pw._fmt_a("/Ac/Current", 15.37) == "15.37A"

    def test_zero(self):
        assert pw._fmt_a("/Ac/Current", 0) == "0.00A"

    def test_none(self):
        assert pw._fmt_a("/Ac/Current", None) == "---"


class TestFmtKwh:
    def test_nominal(self):
        assert pw._fmt_kwh("/Ac/Energy/Forward", 2652.45) == "2652.45kWh"

    def test_none(self):
        assert pw._fmt_kwh("/Ac/Energy/Forward", None) == "---"


class TestFmtHz:
    def test_nominal(self):
        assert pw._fmt_hz("/Ac/Frequency", 60.0) == "60.0Hz"

    def test_none(self):
        assert pw._fmt_hz("/Ac/Frequency", None) == "---"


# Coarse rounding tests (TestRoundTo) moved to tests/test_grid_publisher.py
# alongside the helper itself.


# ── Slider math ─────────────────────────────────────────────────────────────
# Slider 1-100 maps directly to 100ms-10000ms (100ms per step)
# POLL_INTERVAL_MS_PER_STEP = 100
# DEFAULT_UPDATE_INTERVAL_MS = 5000


class TestSliderMath:
    """Validate the slider <-> milliseconds conversion logic used
    in _create_device_switch and _on_device_dimming_changed."""

    STEP = pw.POLL_INTERVAL_MS_PER_STEP

    def test_slider_1_gives_100ms(self):
        assert 1 * self.STEP == 100

    def test_slider_50_gives_5000ms(self):
        assert 50 * self.STEP == 5000

    def test_slider_100_gives_10000ms(self):
        assert 100 * self.STEP == 10000

    def test_default_is_5000(self):
        assert pw.DEFAULT_UPDATE_INTERVAL_MS == 5000

    def test_default_slider_pos(self):
        """The default poll value should map to slider position 50."""
        pos = max(1, min(pw.DEFAULT_UPDATE_INTERVAL_MS // self.STEP, 100))
        assert pos == 50

    def test_clamp_slider_zero(self):
        """Slider position 0 should be clamped to 1."""
        slider_pos = max(1, min(0, 100))
        assert slider_pos == 1

    def test_clamp_slider_over_100(self):
        """Slider position >100 should be clamped to 100."""
        slider_pos = max(1, min(150, 100))
        assert slider_pos == 100

    def test_round_trip_all_positions(self):
        """Every slider position 1-100 round-trips cleanly."""
        for pos in range(1, 101):
            ms = pos * self.STEP
            back = max(1, min(ms // self.STEP, 100))
            assert back == pos, f"Round-trip failed for pos={pos}"


# ── Config loading ──────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_no_config_returns_empty(self, tmp_path, monkeypatch):
        """When no config file exists, returns an empty ConfigParser."""
        monkeypatch.setattr(
            "os.path.dirname",
            lambda _: str(tmp_path),
        )
        # Re-import load_config won't work cleanly, but we can
        # call it with a patched __file__-relative directory.
        with patch.object(pw, "__file__", str(tmp_path / "fake.py")):
            # Call the module-level function; it reads relative to __file__
            # We need to also patch os.path.abspath
            config = pw.load_config()
        assert isinstance(config, configparser.ConfigParser)

    def test_config_ini_is_read(self, tmp_path):
        """When config.ini exists, it is loaded."""
        config_file = tmp_path / "config.ini"
        config_file.write_text("[DEFAULT]\nscan_interval = 30\n")

        with patch("os.path.dirname", return_value=str(tmp_path)):
            with patch("os.path.abspath", return_value=str(tmp_path / "fake.py")):
                config = pw.load_config()

        assert config["DEFAULT"]["scan_interval"] == "30"

    def test_default_ini_fallback(self, tmp_path):
        """When only config.default.ini exists, it is loaded."""
        default_file = tmp_path / "config.default.ini"
        default_file.write_text("[DEFAULT]\nscan_interval = 90\n")

        # config.ini does NOT exist
        with patch("os.path.dirname", return_value=str(tmp_path)):
            with patch("os.path.abspath", return_value=str(tmp_path / "fake.py")):
                config = pw.load_config()

        assert config["DEFAULT"]["scan_interval"] == "90"


# Adapter detection used to live in PowerWatchdogService._detect_adapters,
# scanning /sys/class/bluetooth.  When the project moved to
# bleak-connection-manager (commit c82b550) BCM took over adapter discovery,
# and the method was deleted.  The corresponding TestDetectAdapters class
# was left behind and has been failing on every commit since — removed here
# alongside other test cleanup.


# ── MAC conversion ──────────────────────────────────────────────────────────


class TestMacIdToAddress:
    """Test the _mac_id_to_address method.

    When the device is in _device_info, it returns the stored MAC.
    Otherwise it reconstructs the MAC from the hex mac_id.
    """

    def _make_service_stub(self, device_info=None):
        """Create a minimal stub of PowerWatchdogService with just the
        fields needed for _mac_id_to_address."""
        from power_watchdog_ble import DiscoveredDevice

        class Stub:
            pass

        stub = Stub()
        stub._device_info = device_info or {}
        # Bind the unbound method to our stub
        stub._mac_id_to_address = pw.PowerWatchdogService._mac_id_to_address.__get__(stub)
        return stub

    def test_known_device(self):
        from power_watchdog_ble import DiscoveredDevice

        stub = self._make_service_stub({
            "aabbccddeeff": DiscoveredDevice(
                mac="AA:BB:CC:DD:EE:FF", name="WD_E7_aabbccddeeff"
            ),
        })
        assert stub._mac_id_to_address("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"

    def test_unknown_device_reconstructs(self):
        stub = self._make_service_stub()
        result = stub._mac_id_to_address("aabbccddeeff")
        assert result == "AA:BB:CC:DD:EE:FF"

    def test_reconstruction_format(self):
        stub = self._make_service_stub()
        result = stub._mac_id_to_address("112233445566")
        assert result == "11:22:33:44:55:66"


# ── Constants and roles ─────────────────────────────────────────────────────


class TestConstants:
    def test_allowed_roles(self):
        assert "grid" in pw.ALLOWED_ROLES
        assert "pvinverter" in pw.ALLOWED_ROLES
        assert "genset" in pw.ALLOWED_ROLES

    def test_role_to_service_mapping(self):
        assert pw.ROLE_TO_SERVICE["grid"] == "com.victronenergy.grid"
        assert pw.ROLE_TO_SERVICE["pvinverter"] == "com.victronenergy.pvinverter"
        assert pw.ROLE_TO_SERVICE["genset"] == "com.victronenergy.genset"

    def test_version_format(self):
        parts = pw.VERSION.split(".")
        assert len(parts) == 3
        for p in parts:
            assert p.isdigit()


# ── BLE liveness backstop ─────────────────────────────────────────────────
#
# _check_ble_liveness exits the process so runit restarts it.  The risk is
# not that it fails to fire but that it fires when it shouldn't: a rig with
# shore power unplugged is silent for days and must never restart-loop.


class _FakeBle:
    def __init__(self, cycles: int = 0):
        self.connect_cycles = cycles


class _FakeData:
    def __init__(self, timestamp: float = 0.0):
        self.timestamp = timestamp


def _liveness_svc(cycles: int = 0):
    """A bare service object carrying only the liveness state."""
    svc = object.__new__(pw.PowerWatchdogService)
    svc._ble = _FakeBle(cycles)
    svc._last_ble_cycles = -1
    svc._last_data_timestamp = 0.0
    svc._last_ble_progress = pw.time.monotonic()
    return svc


class TestBleLiveness:
    def test_timeout_exceeds_offline_poll_interval(self):
        # The guard rail: an unplugged unit wakes every OFFLINE_POLL_INTERVAL
        # to poll, so the liveness window must be comfortably longer or a
        # legitimately offline rig would restart-loop forever.
        from power_watchdog_ble import PowerWatchdogBLE

        assert pw.BLE_LIVENESS_TIMEOUT > PowerWatchdogBLE.OFFLINE_POLL_INTERVAL

    def test_advancing_cycles_resets_progress(self, monkeypatch):
        # Offline device: no data ever, but the session loop keeps polling.
        exited = []
        monkeypatch.setattr(pw.os, "_exit", lambda code: exited.append(code))

        svc = _liveness_svc(cycles=1)
        svc._last_ble_progress = pw.time.monotonic() - 10_000

        svc._check_ble_liveness(_FakeData(timestamp=0.0))

        assert exited == []
        assert pw.time.monotonic() - svc._last_ble_progress < 5

    def test_offline_device_never_exits_over_many_polls(self, monkeypatch):
        exited = []
        monkeypatch.setattr(pw.os, "_exit", lambda code: exited.append(code))

        svc = _liveness_svc(cycles=0)
        for i in range(1, 50):
            svc._ble.connect_cycles = i
            # Pretend each poll happened a full offline interval apart.
            svc._last_ble_progress -= 300.0
            svc._check_ble_liveness(_FakeData(timestamp=0.0))

        assert exited == []

    def test_flowing_data_with_frozen_cycles_does_not_exit(self, monkeypatch):
        # Healthy connected device: one long session, so the cycle counter
        # is frozen for hours while telemetry streams.
        exited = []
        monkeypatch.setattr(pw.os, "_exit", lambda code: exited.append(code))

        svc = _liveness_svc(cycles=7)
        svc._check_ble_liveness(_FakeData(timestamp=100.0))
        svc._last_ble_progress = pw.time.monotonic() - 10_000
        svc._check_ble_liveness(_FakeData(timestamp=200.0))

        assert exited == []

    def test_both_signals_frozen_exits_nonzero(self, monkeypatch):
        # The wedge: no connect attempts and no telemetry.
        exited = []
        monkeypatch.setattr(pw.os, "_exit", lambda code: exited.append(code))

        svc = _liveness_svc(cycles=7)
        svc._check_ble_liveness(_FakeData(timestamp=100.0))
        svc._last_ble_progress = pw.time.monotonic() - (
            pw.BLE_LIVENESS_TIMEOUT + 1
        )
        svc._check_ble_liveness(_FakeData(timestamp=100.0))

        assert exited == [1]

    def test_frozen_but_within_window_does_not_exit(self, monkeypatch):
        exited = []
        monkeypatch.setattr(pw.os, "_exit", lambda code: exited.append(code))

        svc = _liveness_svc(cycles=7)
        svc._check_ble_liveness(_FakeData(timestamp=100.0))
        svc._last_ble_progress = pw.time.monotonic() - (
            pw.BLE_LIVENESS_TIMEOUT - 30
        )
        svc._check_ble_liveness(_FakeData(timestamp=100.0))

        assert exited == []


# ── Presence via the advertisement router ─────────────────────────────────
#
# _on_router_advertisement has two jobs: wake the active unit's BLE loop,
# and discover units we do not know from the name BlueZ holds for them.


class TestRouterAdvertisement:
    MAC = "24:EC:4A:E4:69:A5"
    MAC_ID = "24ec4ae469a5"
    HUGHES = 0x2312

    def _svc(self, active=None, device_info=None):
        svc = object.__new__(pw.PowerWatchdogService)
        svc._active_mac_id = active
        svc._ble = MagicMock() if active else None
        svc._device_info = dict(device_info or {})
        svc._router_discovery_attempts = {}
        svc._router = MagicMock()
        svc._bus = MagicMock()
        svc._create_device_switch = MagicMock()
        svc._save_device_to_settings = MagicMock()
        return svc

    def _known(self):
        from power_watchdog_ble import DiscoveredDevice
        return {self.MAC_ID: DiscoveredDevice(mac=self.MAC, name="WD_E7_26ec4ae469a5")}

    def test_active_unit_is_told_it_was_heard(self):
        svc = self._svc(active=self.MAC_ID, device_info=self._known())
        svc._on_router_advertisement(self.MAC, self.HUGHES, b"EV\x00\x00", -60, "684E054477B0")
        svc._ble.notify_present.assert_called_once_with("684E054477B0")
        svc._create_device_switch.assert_not_called()

    def test_known_inactive_unit_is_left_alone(self, monkeypatch):
        svc = self._svc(active=None, device_info=self._known())
        lookup = MagicMock()
        monkeypatch.setattr(pw, "bluez_device_name", lookup)
        svc._on_router_advertisement(self.MAC, self.HUGHES, b"EV\x00\x00", -60, "hci5")
        lookup.assert_not_called()
        svc._create_device_switch.assert_not_called()

    def test_unknown_unit_is_discovered_from_its_bluez_name(self, monkeypatch):
        svc = self._svc()
        monkeypatch.setattr(pw, "bluez_device_name",
                            MagicMock(return_value="WD_E7_26ec4ae469a5"))
        monkeypatch.setattr(pw, "resolve_adapter", MagicMock(return_value="hci5"))

        svc._on_router_advertisement(self.MAC, self.HUGHES, b"EV\x00\x00", -60, "684E054477B0")

        pw.bluez_device_name.assert_called_once_with(svc._bus, self.MAC, "hci5")
        assert svc._device_info[self.MAC_ID].mac == self.MAC
        assert svc._device_info[self.MAC_ID].generation == 2
        svc._create_device_switch.assert_called_once_with(
            self.MAC_ID, "WD_E7_26ec4ae469a5", enabled=False)
        svc._save_device_to_settings.assert_called_once_with(
            self.MAC_ID, "WD_E7_26ec4ae469a5", self.MAC, enabled=False)
        svc._router.add_device.assert_called_once_with(self.MAC)

    def test_no_name_yet_retries_after_the_quiet_period(self, monkeypatch):
        svc = self._svc()
        lookup = MagicMock(return_value=None)
        monkeypatch.setattr(pw, "bluez_device_name", lookup)
        monkeypatch.setattr(pw, "resolve_adapter", MagicMock(return_value=None))

        now = [1000.0]
        monkeypatch.setattr(pw.time, "monotonic", lambda: now[0])

        svc._on_router_advertisement(self.MAC, self.HUGHES, b"EV\x00\x00", -60, "hci5")
        svc._on_router_advertisement(self.MAC, self.HUGHES, b"EV\x00\x00", -60, "hci5")
        assert lookup.call_count == 1                 # within the quiet period
        svc._create_device_switch.assert_not_called()

        now[0] += pw.ROUTER_DISCOVERY_RETRY + 1
        svc._on_router_advertisement(self.MAC, self.HUGHES, b"EV\x00\x00", -60, "hci5")
        assert lookup.call_count == 2

    def test_a_name_that_is_not_a_watchdog_is_ignored(self, monkeypatch):
        svc = self._svc()
        monkeypatch.setattr(pw, "bluez_device_name", MagicMock(return_value="Ruuvi 1234"))
        monkeypatch.setattr(pw, "resolve_adapter", MagicMock(return_value=None))
        svc._on_router_advertisement(self.MAC, self.HUGHES, b"EV\x00\x00", -60, "hci5")
        svc._create_device_switch.assert_not_called()
        svc._save_device_to_settings.assert_not_called()
        assert self.MAC_ID not in svc._device_info

    def test_other_manufacturers_are_not_looked_up(self, monkeypatch):
        svc = self._svc()
        lookup = MagicMock()
        monkeypatch.setattr(pw, "bluez_device_name", lookup)
        svc._on_router_advertisement("AA:BB:CC:DD:EE:FF", 0x02E1, b"\x10\x02", -60, "hci5")
        lookup.assert_not_called()

    def test_router_state_gates_the_active_ble_loop(self):
        svc = self._svc(active=self.MAC_ID, device_info=self._known())
        svc._on_router_state(True)
        svc._ble.set_presence_gated.assert_called_once_with(True)
        svc._on_router_state(False)
        svc._ble.set_presence_gated.assert_called_with(False)

    def test_router_state_with_no_active_unit(self):
        svc = self._svc()
        svc._on_router_state(True)  # nothing to gate; must not raise
