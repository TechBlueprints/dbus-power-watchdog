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


# ── Adapter detection ───────────────────────────────────────────────────────


class TestDetectAdapters:
    def test_single_adapter(self):
        """Single hci0 adapter is detected."""
        with patch("os.path.isdir", return_value=True):
            with patch("os.listdir", return_value=["hci0"]):
                result = pw.PowerWatchdogService._detect_adapters()
        assert result == ["hci0"]

    def test_multiple_adapters(self, tmp_path):
        """Multiple adapters are returned in sorted order."""
        bt_dir = tmp_path / "bluetooth"
        bt_dir.mkdir()
        (bt_dir / "hci0").mkdir()
        (bt_dir / "hci1").mkdir()

        with patch("os.path.isdir", return_value=True):
            with patch("os.listdir", return_value=["hci1", "hci0"]):
                result = pw.PowerWatchdogService._detect_adapters()
        assert result == ["hci0", "hci1"]

    def test_filters_connection_handles(self, tmp_path):
        """Entries like 'hci0:11' (connection handles) are excluded."""
        with patch("os.path.isdir", return_value=True):
            with patch("os.listdir", return_value=["hci0", "hci0:11", "hci1:5"]):
                result = pw.PowerWatchdogService._detect_adapters()
        assert result == ["hci0"]

    def test_no_adapters_returns_empty_string(self):
        """When /sys/class/bluetooth has no hci entries, returns ['']."""
        with patch("os.path.isdir", return_value=True):
            with patch("os.listdir", return_value=[]):
                result = pw.PowerWatchdogService._detect_adapters()
        assert result == [""]

    def test_no_bluetooth_dir(self):
        """When /sys/class/bluetooth doesn't exist, returns ['']."""
        with patch("os.path.isdir", return_value=False):
            result = pw.PowerWatchdogService._detect_adapters()
        assert result == [""]


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
