#!/usr/bin/env python3
# Copyright 2025 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

"""
Single-process Venus OS service for Hughes Power Watchdog devices.

Registers as a Venus OS switch device (com.victronenergy.switch.power_watchdog)
with a discovery toggle and per-device dimmable switches.  When discovery is
enabled, periodically scans BLE for Power Watchdog devices (both gen1 PM* and
gen2 WD_* name patterns).

Each discovered device appears as a Type-2 (dimmable) switch:
  - State (on/off) controls whether the BLE connection is active
  - Dimming (1-100) sets the polling interval (100ms-10000ms, 100ms/step)

Only one device may be active at a time.  The BLE connection and D-Bus grid
(or genset/pvinverter) service run in-process -- no child processes.

Supports both 30A (single-line) and 50A (dual-line L1+L2) models.
"""

import asyncio
import configparser
import logging
import os
import platform
import signal
import sys
import threading
import xml.etree.ElementTree as ET

import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

# Add ext folders to sys.path
_ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
sys.path.insert(1, os.path.join(_ext_dir, "velib_python"))

# All BLE dependencies from local ext/ submodules (upstream repos)
for _sub in [
    os.path.join(_ext_dir, "bleak-connection-manager", "src"),  # bleak_connection_manager
    os.path.join(_ext_dir, "bleak-retry-connector", "src"),     # bleak_retry_connector
    os.path.join(_ext_dir, "bluetooth-adapters", "src"),        # bluetooth_adapters
    os.path.join(_ext_dir, "aiooui", "src"),                    # aiooui
    os.path.join(_ext_dir, "bleak"),                            # bleak (package at repo root)
]:
    if os.path.isdir(_sub) and _sub not in sys.path:
        sys.path.insert(0, _sub)

from vedbus import VeDbusService  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

from bleak_connection_manager import LockConfig, ScanLockConfig  # noqa: E402
from power_watchdog_ble import (  # noqa: E402
    PowerWatchdogBLE,
    scan_for_devices,
    classify_device,
    DiscoveredDevice,
)

VERSION = "0.8.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dbus-power-watchdog")

# Default scan interval when discovery is enabled (seconds)
DEFAULT_SCAN_INTERVAL = 60

# Slider 1-100 maps directly to 100ms-10000ms (100ms per step)
DEFAULT_UPDATE_INTERVAL_MS = 5000
POLL_INTERVAL_MS_PER_STEP = 100

# Default reconnect parameters for the BLE client
DEFAULT_RECONNECT_DELAY = 10
DEFAULT_RECONNECT_MAX_DELAY = 120

# Fronius PV-inverter ProductId (0xA142).  We use this so the Venus OS GUI
# displays our /ErrorCode — ListAcInError.qml only shows the error row for
# Fronius or Carlo Gavazzi product IDs.  The Fronius path is the simplest:
# it renders the raw error code number.
# TODO: switch to a generic product ID once gui-v2#2816 is accepted:
# https://github.com/victronenergy/gui-v2/pull/2816
PRODUCT_ID_FRONIUS = 0xA142

# Valid roles and their D-Bus service class
ALLOWED_ROLES = ["grid", "pvinverter", "genset"]
ROLE_TO_SERVICE = {
    "grid": "com.victronenergy.grid",
    "pvinverter": "com.victronenergy.pvinverter",
    "genset": "com.victronenergy.genset",
}


class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)


class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)


def get_bus() -> dbus.bus.BusConnection:
    return SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else SystemBus()


def load_config() -> configparser.ConfigParser:
    """Load configuration from config.ini, falling back to config.default.ini."""
    config = configparser.ConfigParser()
    config_dir = os.path.dirname(os.path.abspath(__file__))

    config_file = os.path.join(config_dir, "config.ini")
    default_file = os.path.join(config_dir, "config.default.ini")

    if os.path.exists(config_file):
        config.read(config_file)
        logger.info("Loaded config from %s", config_file)
    elif os.path.exists(default_file):
        config.read(default_file)
        logger.info("Loaded default config from %s", default_file)
    else:
        logger.warning("No config file found, using defaults")

    return config


# ── D-Bus value formatters ──────────────────────────────────────────────────

def _fmt_w(_path, x):
    return "{:.0f}W".format(x) if x is not None else "---"

def _fmt_v(_path, x):
    return "{:.1f}V".format(x) if x is not None else "---"

def _fmt_a(_path, x):
    return "{:.2f}A".format(x) if x is not None else "---"

def _fmt_kwh(_path, x):
    return "{:.2f}kWh".format(x) if x is not None else "---"

def _fmt_hz(_path, x):
    return "{:.1f}Hz".format(x) if x is not None else "---"


# ── Service ─────────────────────────────────────────────────────────────────

class PowerWatchdogService:
    """Single-process Venus OS service: discovery + BLE connection + grid meter."""

    def __init__(self):
        config = load_config()
        defaults = config["DEFAULT"] if "DEFAULT" in config else {}

        # Config values
        self._scan_interval = int(defaults.get("scan_interval", str(DEFAULT_SCAN_INTERVAL)))
        self._reconnect_delay = float(defaults.get("reconnect_delay", str(DEFAULT_RECONNECT_DELAY)))
        self._reconnect_max_delay = float(defaults.get("reconnect_max_delay", str(DEFAULT_RECONNECT_MAX_DELAY)))

        # BCM lock configs for cross-process BLE coordination
        self._lock_config = LockConfig(enabled=True)
        self._scan_lock_config = ScanLockConfig(enabled=True)

        logger.info("Scan interval: %ds", self._scan_interval)

        # D-Bus connection
        self._bus = get_bus()

        # Discovered device info cache: mac_id -> DiscoveredDevice
        self._device_info: dict[str, DiscoveredDevice] = {}

        # Per-device polling intervals: mac_id -> milliseconds
        self._device_poll_ms: dict[str, int] = {}

        # Scanning state
        self._scan_timer_id = None
        self._scanning = False

        # Active device state (only one device at a time)
        self._active_mac_id: str | None = None
        self._ble: PowerWatchdogBLE | None = None
        self._grid_bus: dbus.bus.BusConnection | None = None  # separate bus for grid service
        self._grid_service: VeDbusService | None = None
        self._grid_timer_id: int | None = None
        self._grid_settings: SettingsDevice | None = None
        self._current_role: str | None = None
        self._update_index: int = 0
        self._update_interval_ms: int = DEFAULT_UPDATE_INTERVAL_MS

        # Debounce timer for polling interval slider
        self._poll_debounce_timer_id: int | None = None
        self._poll_debounce_target_ms: int = 0

        # ── Register as Venus OS switch device ──────────────────────────
        self._dbusservice = VeDbusService(
            "com.victronenergy.switch.power_watchdog", self._bus, register=False
        )

        # Mandatory device paths
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", VERSION)
        self._dbusservice.add_path("/Mgmt/Connection", "Power Watchdog Discovery")
        self._dbusservice.add_path("/DeviceInstance", 130)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", "Power Watchdog Manager")
        self._dbusservice.add_path("/CustomName", "Power Watchdog Manager")
        self._dbusservice.add_path("/FirmwareVersion", VERSION)
        self._dbusservice.add_path("/HardwareVersion", None)
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/State", 0x100)

        # ── Discovery toggle switch ─────────────────────────────────────
        discovery_path = "/SwitchableOutput/relay_discovery"
        self._dbusservice.add_path("%s/Name" % discovery_path, "* Power Watchdog Device Discovery")
        self._dbusservice.add_path("%s/Type" % discovery_path, 1)
        self._dbusservice.add_path(
            "%s/State" % discovery_path, 0,
            writeable=True, onchangecallback=self._on_discovery_changed,
        )
        self._dbusservice.add_path("%s/Status" % discovery_path, 0x00)
        self._dbusservice.add_path("%s/Current" % discovery_path, 0)
        self._dbusservice.add_path("%s/Settings/CustomName" % discovery_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/Type" % discovery_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidTypes" % discovery_path, 2)
        self._dbusservice.add_path("%s/Settings/Function" % discovery_path, 2, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidFunctions" % discovery_path, 4)
        self._dbusservice.add_path("%s/Settings/Group" % discovery_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/ShowUIControl" % discovery_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/PowerOnState" % discovery_path, 1)

        # ── HasAcInLoads toggle switch ───────────────────────────────────
        ac_loads_path = "/SwitchableOutput/relay_has_ac_in_loads"
        self._dbusservice.add_path("%s/Name" % ac_loads_path, "* Report AC Input Loads")
        self._dbusservice.add_path("%s/Type" % ac_loads_path, 1)
        self._dbusservice.add_path(
            "%s/State" % ac_loads_path, 0,
            writeable=True, onchangecallback=self._on_has_ac_in_loads_changed,
        )
        self._dbusservice.add_path("%s/Status" % ac_loads_path, 0x00)
        self._dbusservice.add_path("%s/Current" % ac_loads_path, 0)
        self._dbusservice.add_path("%s/Settings/CustomName" % ac_loads_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/Type" % ac_loads_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidTypes" % ac_loads_path, 2)
        self._dbusservice.add_path("%s/Settings/Function" % ac_loads_path, 2, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidFunctions" % ac_loads_path, 4)
        self._dbusservice.add_path("%s/Settings/Group" % ac_loads_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/ShowUIControl" % ac_loads_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/PowerOnState" % ac_loads_path, 1)

        # ── Inverter Metering toggle switch ──────────────────────────────
        inv_meter_path = "/SwitchableOutput/relay_inverter_metering"
        self._dbusservice.add_path(
            "%s/Name" % inv_meter_path, "* Use Inverter Metering (not grid meter)"
        )
        self._dbusservice.add_path("%s/Type" % inv_meter_path, 1)
        self._dbusservice.add_path(
            "%s/State" % inv_meter_path, 0,
            writeable=True, onchangecallback=self._on_run_without_grid_meter_changed,
        )
        self._dbusservice.add_path("%s/Status" % inv_meter_path, 0x00)
        self._dbusservice.add_path("%s/Current" % inv_meter_path, 0)
        self._dbusservice.add_path("%s/Settings/CustomName" % inv_meter_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/Type" % inv_meter_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidTypes" % inv_meter_path, 2)
        self._dbusservice.add_path("%s/Settings/Function" % inv_meter_path, 2, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidFunctions" % inv_meter_path, 4)
        self._dbusservice.add_path("%s/Settings/Group" % inv_meter_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/ShowUIControl" % inv_meter_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/PowerOnState" % inv_meter_path, 1)

        # ── Persistent settings ─────────────────────────────────────────
        settings = {
            "ClassAndVrmInstance": [
                "/Settings/Devices/power_watchdog/ClassAndVrmInstance",
                "switch:120", 0, 0,
            ],
            "DiscoveryEnabled": [
                "/Settings/Devices/power_watchdog/DiscoveryEnabled",
                0, 0, 1,
            ],
            "HasAcInLoads": [
                "/Settings/Devices/power_watchdog/HasAcInLoads",
                1, 0, 1,
            ],
            "RunWithoutGridMeter": [
                "/Settings/Devices/power_watchdog/RunWithoutGridMeter",
                0, 0, 1,
            ],
        }
        self._settings = SettingsDevice(
            self._bus, settings,
            eventCallback=self._on_settings_changed,
            timeout=10,
        )

        # Register switch service on D-Bus
        self._dbusservice.register()
        logger.info("Registered discovery service on D-Bus")

        # Restore HasAcInLoads toggle and apply
        has_ac_in_loads = int(self._settings["HasAcInLoads"])
        self._dbusservice["/SwitchableOutput/relay_has_ac_in_loads/State"] = has_ac_in_loads
        self._apply_has_ac_in_loads(has_ac_in_loads)

        # Restore RunWithoutGridMeter toggle and apply
        run_without = int(self._settings["RunWithoutGridMeter"])
        self._dbusservice["/SwitchableOutput/relay_inverter_metering/State"] = run_without
        self._apply_run_without_grid_meter(run_without)

        # Restore previously discovered devices from settings
        self._restore_devices_from_settings()

        # Restore discovery state
        discovery_state = self._settings["DiscoveryEnabled"]
        self._dbusservice["/SwitchableOutput/relay_discovery/State"] = discovery_state
        if discovery_state:
            logger.info("Discovery enabled from saved settings")
            self._start_scanning()

        # Activate the enabled device (if any) from restored settings
        self._activate_enabled_device()

    # ── System setting synchronisation ─────────────────────────────────────

    def _apply_has_ac_in_loads(self, value: int):
        """Write HasAcInLoads to the Venus OS system setting."""
        try:
            system_bus = dbus.SystemBus()
            system_bus.call_blocking(
                "com.victronenergy.settings",
                "/Settings/SystemSetup/HasAcInLoads",
                "com.victronenergy.BusItem", "SetValue",
                "v", [dbus.Int32(value)], timeout=5,
            )
            logger.info("System HasAcInLoads set to %d", value)
        except Exception:
            logger.exception("Failed to set HasAcInLoads on system bus")

    def _apply_run_without_grid_meter(self, value: int):
        """Write RunWithoutGridMeter to the Venus OS system setting."""
        try:
            system_bus = dbus.SystemBus()
            system_bus.call_blocking(
                "com.victronenergy.settings",
                "/Settings/CGwacs/RunWithoutGridMeter",
                "com.victronenergy.BusItem", "SetValue",
                "v", [dbus.Int32(value)], timeout=5,
            )
            logger.info("System RunWithoutGridMeter set to %d", value)
        except Exception:
            logger.exception("Failed to set RunWithoutGridMeter on system bus")

    # ── Toggle callbacks ─────────────────────────────────────────────────────

    def _on_has_ac_in_loads_changed(self, path, value):
        enabled = bool(int(value) if isinstance(value, str) else value)
        logger.info("HasAcInLoads %s by user", "enabled" if enabled else "disabled")
        self._settings["HasAcInLoads"] = 1 if enabled else 0
        self._apply_has_ac_in_loads(1 if enabled else 0)
        return True

    def _on_run_without_grid_meter_changed(self, path, value):
        enabled = bool(int(value) if isinstance(value, str) else value)
        logger.info("RunWithoutGridMeter %s by user", "enabled" if enabled else "disabled")
        self._settings["RunWithoutGridMeter"] = 1 if enabled else 0
        self._apply_run_without_grid_meter(1 if enabled else 0)
        return True

    def _on_settings_changed(self, setting, old_value, new_value):
        logger.debug("Setting changed: %s = %s", setting, new_value)
        if setting == "HasAcInLoads":
            self._apply_has_ac_in_loads(int(new_value))
        elif setting == "RunWithoutGridMeter":
            self._apply_run_without_grid_meter(int(new_value))

    # ── Discovery toggle ────────────────────────────────────────────────────

    def _on_discovery_changed(self, path, value):
        enabled = bool(int(value) if isinstance(value, str) else value)
        logger.info("Discovery %s", "enabled" if enabled else "disabled")
        self._settings["DiscoveryEnabled"] = 1 if enabled else 0

        if enabled:
            self._start_scanning()
            for mac_id in self._device_info:
                show_path = "/SwitchableOutput/relay_%s/Settings/ShowUIControl" % mac_id
                if show_path in self._dbusservice:
                    self._dbusservice[show_path] = 1
        else:
            self._stop_scanning()
            for mac_id in self._device_info:
                show_path = "/SwitchableOutput/relay_%s/Settings/ShowUIControl" % mac_id
                if show_path in self._dbusservice:
                    self._dbusservice[show_path] = 0
        return True

    # ── BLE Scanning ────────────────────────────────────────────────────────

    def _start_scanning(self):
        if self._scan_timer_id is not None:
            return
        logger.info("Starting BLE discovery (interval: %ds)", self._scan_interval)
        GLib.idle_add(lambda: (self._trigger_scan(), False)[-1])
        self._scan_timer_id = GLib.timeout_add_seconds(
            self._scan_interval, self._trigger_scan
        )

    def _stop_scanning(self):
        if self._scan_timer_id is not None:
            GLib.source_remove(self._scan_timer_id)
            self._scan_timer_id = None
            logger.info("Stopped BLE discovery")

    def _trigger_scan(self) -> bool:
        if self._scanning:
            return True
        self._scanning = True
        thread = threading.Thread(
            target=self._run_scan_thread, name="PowerWatchdog_Discovery", daemon=True,
        )
        thread.start()
        return True

    def _run_scan_thread(self):
        try:
            loop = asyncio.new_event_loop()
            found = loop.run_until_complete(
                scan_for_devices(
                    timeout=15.0,
                    scan_lock_config=self._scan_lock_config,
                )
            )
            loop.close()
            GLib.idle_add(self._process_scan_results, found)
        except Exception:
            logger.exception("BLE scan failed")
        finally:
            self._scanning = False

    def _process_scan_results(self, found: list[DiscoveredDevice]) -> bool:
        for device in found:
            mac_id = device.mac.replace(":", "").lower()
            if mac_id in self._device_info:
                continue
            logger.info(
                "New Power Watchdog discovered: %s (%s) gen%d %s",
                device.name, device.mac, device.generation, device.device_type,
            )
            self._device_info[mac_id] = device
            self._create_device_switch(mac_id, device.name, enabled=False)
            self._save_device_to_settings(mac_id, device.name, device.mac, enabled=False)
        return False

    # ── Per-device switch management ───────────────────────────────────────
    #
    # Disabled devices show as Type 1 (simple toggle).
    # The active (enabled) device shows as Type 2 (dimmable) so the user
    # can adjust the polling interval via the slider.

    def _create_device_switch(self, mac_id: str, name: str, enabled: bool,
                              poll_ms: int = DEFAULT_UPDATE_INTERVAL_MS):
        """Create a switch for a discovered device.

        Disabled devices start as Type 1 (toggle).  The enabled device
        starts as Type 2 (dimmable) with the polling interval slider.
        """
        output_path = "/SwitchableOutput/relay_%s" % mac_id

        if "%s/State" % output_path in self._dbusservice:
            return

        slider_pos = max(1, min(poll_ms // POLL_INTERVAL_MS_PER_STEP, 100))
        self._device_poll_ms[mac_id] = slider_pos * POLL_INTERVAL_MS_PER_STEP

        # Enabled → Type 2 (slider), disabled → Type 1 (toggle)
        switch_type = 2 if enabled else 1
        display_name = (
            "%s (%dms)" % (name, self._device_poll_ms[mac_id]) if enabled else name
        )

        with self._dbusservice as ctx:
            ctx.add_path("%s/Name" % output_path, display_name)
            ctx.add_path("%s/Type" % output_path, switch_type)
            ctx.add_path(
                "%s/State" % output_path, 1 if enabled else 0,
                writeable=True, onchangecallback=self._on_device_state_changed,
            )
            ctx.add_path("%s/Status" % output_path, 0x09 if enabled else 0x00)
            ctx.add_path("%s/Current" % output_path, 0)
            ctx.add_path(
                "%s/Dimming" % output_path, slider_pos,
                writeable=True, onchangecallback=self._on_device_dimming_changed,
            )
            ctx.add_path("%s/Settings/CustomName" % output_path, "", writeable=True)
            ctx.add_path("%s/Settings/Type" % output_path, switch_type, writeable=False)
            ctx.add_path("%s/Settings/ValidTypes" % output_path, 4)
            ctx.add_path("%s/Settings/Function" % output_path, 2, writeable=True)
            ctx.add_path("%s/Settings/ValidFunctions" % output_path, 4)
            ctx.add_path("%s/Settings/Group" % output_path, "", writeable=True)
            ctx.add_path("%s/Settings/ShowUIControl" % output_path, 1, writeable=True)
            ctx.add_path("%s/Settings/PowerOnState" % output_path, 1)
            ctx.add_path("%s/Settings/DimmingMin" % output_path, 1.0)
            ctx.add_path("%s/Settings/DimmingMax" % output_path, 100.0)
            ctx.add_path("%s/Settings/StepSize" % output_path, 1.0)
            ctx.add_path("%s/Settings/Decimals" % output_path, 0)

        logger.info("Created switch for %s (%s), type=%d, enabled=%s, poll=%dms",
                     name, mac_id, switch_type, enabled, self._device_poll_ms[mac_id])

    def _set_switch_type(self, mac_id: str, switch_type: int):
        """Change a device switch between Type 1 (toggle) and Type 2 (dimmable).

        Type 1 = simple on/off toggle (for disabled devices).
        Type 2 = dimmable with polling interval slider (for the active device).
        """
        output_path = "/SwitchableOutput/relay_%s" % mac_id
        type_path = "%s/Type" % output_path
        settings_type_path = "%s/Settings/Type" % output_path

        if type_path not in self._dbusservice:
            return

        self._dbusservice[type_path] = switch_type
        self._dbusservice[settings_type_path] = switch_type

        # Update the name label to show/hide the interval
        name_base = self._device_info[mac_id].name if mac_id in self._device_info else mac_id
        if switch_type == 2:
            poll_ms = self._device_poll_ms.get(mac_id, DEFAULT_UPDATE_INTERVAL_MS)
            self._dbusservice["%s/Name" % output_path] = "%s (%dms)" % (name_base, poll_ms)
        else:
            self._dbusservice["%s/Name" % output_path] = name_base

    def _on_device_state_changed(self, path, value):
        """Called when a per-device on/off toggle is changed."""
        path_parts = path.split("/")
        if len(path_parts) < 3 or not path_parts[2].startswith("relay_"):
            return True

        mac_id = path_parts[2].replace("relay_", "")
        enabled = bool(int(value) if isinstance(value, str) else value)

        name_path = "/SwitchableOutput/relay_%s/Name" % mac_id
        device_name = self._dbusservice[name_path] if name_path in self._dbusservice else mac_id

        logger.info("Device '%s' (%s): %s", device_name, mac_id,
                     "enabled" if enabled else "disabled")

        # Persist state
        mac_address = self._device_info[mac_id].mac if mac_id in self._device_info else ""
        self._save_device_to_settings(mac_id, device_name, mac_address, enabled)

        if enabled:
            # Single-active-device enforcement: deactivate any other device
            if self._active_mac_id is not None and self._active_mac_id != mac_id:
                old_mac = self._active_mac_id
                logger.info("Deactivating %s (only one device may be active)", old_mac)
                self._deactivate_device()
                # Turn off the old switch in the GUI and demote to toggle
                old_state_path = "/SwitchableOutput/relay_%s/State" % old_mac
                if old_state_path in self._dbusservice:
                    self._dbusservice[old_state_path] = 0
                self._set_switch_type(old_mac, 1)
                old_addr = self._device_info[old_mac].mac if old_mac in self._device_info else ""
                old_name = self._dbusservice.get("/SwitchableOutput/relay_%s/Name" % old_mac, old_mac)
                self._save_device_to_settings(old_mac, old_name, old_addr, False)

            # Promote to dimmable slider and activate
            self._set_switch_type(mac_id, 2)
            self._activate_device(mac_id)
        else:
            if self._active_mac_id == mac_id:
                self._deactivate_device()
            # Demote back to simple toggle
            self._set_switch_type(mac_id, 1)

        return True

    def _on_device_dimming_changed(self, path, value):
        """Called when the polling interval slider is moved."""
        path_parts = path.split("/")
        if len(path_parts) < 3 or not path_parts[2].startswith("relay_"):
            return True

        mac_id = path_parts[2].replace("relay_", "")
        slider_pos = int(value) if isinstance(value, str) else int(value)
        slider_pos = max(1, min(slider_pos, 100))
        new_ms = slider_pos * POLL_INTERVAL_MS_PER_STEP

        self._device_poll_ms[mac_id] = new_ms

        # Persist polling interval
        self._save_poll_interval_to_settings(mac_id, new_ms)

        # Update name label immediately
        name_base = self._device_info[mac_id].name if mac_id in self._device_info else mac_id
        self._dbusservice["/SwitchableOutput/relay_%s/Name" % mac_id] = (
            "%s (%dms)" % (name_base, new_ms)
        )

        # If this is the active device, debounce the timer reschedule
        if self._active_mac_id == mac_id:
            self._poll_debounce_target_ms = new_ms
            if self._poll_debounce_timer_id is not None:
                GLib.source_remove(self._poll_debounce_timer_id)
            self._poll_debounce_timer_id = GLib.timeout_add_seconds(
                5, self._apply_debounced_reschedule
            )

        return True

    def _apply_debounced_reschedule(self) -> bool:
        """Apply the debounced polling interval change."""
        self._poll_debounce_timer_id = None
        new_ms = self._poll_debounce_target_ms
        if new_ms == self._update_interval_ms:
            return False
        logger.info("Applying polling interval: %dms -> %dms", self._update_interval_ms, new_ms)
        self._update_interval_ms = new_ms
        if self._grid_timer_id is not None:
            GLib.source_remove(self._grid_timer_id)
        self._grid_timer_id = GLib.timeout_add(self._update_interval_ms, self._update_grid)
        if self._grid_service is not None:
            self._grid_service["/RefreshTime"] = self._update_interval_ms
        return False  # one-shot

    # ── Device activation / deactivation (in-process BLE + grid service) ────

    def _mac_id_to_address(self, mac_id: str) -> str:
        if mac_id in self._device_info:
            return self._device_info[mac_id].mac
        return ":".join(mac_id[i:i+2].upper() for i in range(0, 12, 2))

    def _activate_device(self, mac_id: str):
        """Start BLE connection and register grid D-Bus service for a device."""
        if self._active_mac_id == mac_id and self._ble is not None:
            logger.info("Device %s already active", mac_id)
            return

        mac_address = self._mac_id_to_address(mac_id)
        poll_ms = self._device_poll_ms.get(mac_id, DEFAULT_UPDATE_INTERVAL_MS)
        self._update_interval_ms = poll_ms
        self._active_mac_id = mac_id

        logger.info("Activating device %s (%s), poll=%dms", mac_id, mac_address, poll_ms)

        # Start BLE client with BCM lock configs for cross-process coordination
        self._ble = PowerWatchdogBLE(
            address=mac_address,
            reconnect_delay=self._reconnect_delay,
            reconnect_max_delay=self._reconnect_max_delay,
            lock_config=self._lock_config,
            scan_lock_config=self._scan_lock_config,
        )

        # Per-device persistent settings (role, name, position)
        settings_path = "/Settings/Devices/power_watchdog_%s" % mac_id
        self._grid_settings = SettingsDevice(
            bus=dbus.SystemBus(private=True) if "DBUS_SESSION_BUS_ADDRESS" not in os.environ
                else dbus.SessionBus(private=True),
            supportedSettings={
                "role": ["%s/Role" % settings_path, "grid", 0, 0],
                "custom_name": ["%s/CustomName" % settings_path, "Power Watchdog", 0, 0],
                "position": ["%s/Position" % settings_path, 0, 0, 2],
            },
            eventCallback=self._on_grid_setting_changed,
            timeout=10,
        )

        role = self._grid_settings["role"]
        if role not in ALLOWED_ROLES:
            role = "grid"
        self._create_grid_service(mac_id, mac_address, role)

    def _create_grid_service(self, mac_id: str, mac_address: str, role: str):
        """Create (or re-create) the D-Bus grid/genset/pvinverter service."""
        if self._grid_service is not None:
            if self._grid_timer_id is not None:
                GLib.source_remove(self._grid_timer_id)
                self._grid_timer_id = None
            del self._grid_service
            self._grid_service = None
        if self._grid_bus is not None:
            self._grid_bus.close()
            self._grid_bus = None

        service_class = ROLE_TO_SERVICE.get(role, ROLE_TO_SERVICE["grid"])
        servicename = "%s.power_watchdog_%s" % (service_class, mac_id)
        self._current_role = role

        # Grid service needs its own bus connection because VeDbusService
        # registers a root object handler on '/', and the switch service
        # already occupies that slot on self._bus.
        self._grid_bus = get_bus()

        svc = VeDbusService(servicename, self._grid_bus, register=False)

        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", "Python " + platform.python_version())
        svc.add_path("/Mgmt/Connection", "BLE " + mac_address)

        device_instance = 40 + (int(mac_id, 16) % 260)
        svc.add_path("/DeviceInstance", device_instance)
        svc.add_path("/ProductId", PRODUCT_ID_FRONIUS)
        svc.add_path("/ProductName", "Hughes Power Watchdog")
        svc.add_path("/CustomName", self._grid_settings["custom_name"],
                      writeable=True, onchangecallback=self._on_custom_name_changed)
        svc.add_path("/FirmwareVersion", VERSION)
        svc.add_path("/HardwareVersion", "BLE")
        svc.add_path("/Connected", 0)
        svc.add_path("/Serial", mac_address)

        svc.add_path("/DeviceType", 0)
        svc.add_path("/Role", role, writeable=True, onchangecallback=self._on_role_changed)
        svc.add_path("/AllowedRoles", ALLOWED_ROLES)
        svc.add_path("/NrOfPhases", None)
        svc.add_path("/Position", self._grid_settings["position"],
                      writeable=True, onchangecallback=self._on_position_changed)
        svc.add_path("/RefreshTime", self._update_interval_ms)

        svc.add_path("/Ac/Power", None, gettextcallback=_fmt_w)
        svc.add_path("/Ac/Current", None, gettextcallback=_fmt_a)
        svc.add_path("/Ac/Voltage", None, gettextcallback=_fmt_v)
        svc.add_path("/Ac/Frequency", None, gettextcallback=_fmt_hz)
        svc.add_path("/Ac/Energy/Forward", None, gettextcallback=_fmt_kwh)
        svc.add_path("/Ac/Energy/Reverse", 0)

        for phase in ("L1", "L2"):
            svc.add_path("/Ac/%s/Voltage" % phase, None, gettextcallback=_fmt_v)
            svc.add_path("/Ac/%s/Current" % phase, None, gettextcallback=_fmt_a)
            svc.add_path("/Ac/%s/Power" % phase, None, gettextcallback=_fmt_w)
            svc.add_path("/Ac/%s/Energy/Forward" % phase, None, gettextcallback=_fmt_kwh)
            svc.add_path("/Ac/%s/Energy/Reverse" % phase, 0)
            svc.add_path("/Ac/%s/Frequency" % phase, None, gettextcallback=_fmt_hz)

        svc.add_path("/ErrorCode", 0)
        self._update_index = 0
        svc.add_path("/UpdateIndex", 0)

        svc.register()
        self._grid_service = svc
        logger.info("Registered grid service as %s (role=%s)", servicename, role)

        self._grid_timer_id = GLib.timeout_add(self._update_interval_ms, self._update_grid)

    def _deactivate_device(self):
        """Stop BLE, tear down grid service, clear active state."""
        if self._poll_debounce_timer_id is not None:
            GLib.source_remove(self._poll_debounce_timer_id)
            self._poll_debounce_timer_id = None

        if self._grid_timer_id is not None:
            GLib.source_remove(self._grid_timer_id)
            self._grid_timer_id = None

        if self._ble is not None:
            logger.info("Disconnecting BLE...")
            self._ble.stop()
            self._ble = None

        if self._grid_service is not None:
            del self._grid_service
            self._grid_service = None
        if self._grid_bus is not None:
            self._grid_bus.close()
            self._grid_bus = None

        self._grid_settings = None
        self._current_role = None
        mac_id = self._active_mac_id
        self._active_mac_id = None
        logger.info("Deactivated device %s", mac_id)

    def _activate_enabled_device(self):
        """On startup, activate the first device with Enabled=1."""
        for mac_id in self._device_info:
            state_path = "/SwitchableOutput/relay_%s/State" % mac_id
            if state_path in self._dbusservice and self._dbusservice[state_path] == 1:
                self._activate_device(mac_id)
                break

    # ── Grid service callbacks ──────────────────────────────────────────────

    def _on_role_changed(self, path, value):
        if value not in ALLOWED_ROLES:
            return False
        if value == self._current_role:
            return True
        logger.info("Role change: %s -> %s", self._current_role, value)
        if self._grid_settings:
            self._grid_settings["role"] = value
        if self._active_mac_id:
            mac_address = self._mac_id_to_address(self._active_mac_id)
            GLib.idle_add(self._create_grid_service, self._active_mac_id, mac_address, value)
        return True

    def _on_custom_name_changed(self, path, value):
        logger.info("CustomName changed to '%s'", value)
        if self._grid_settings:
            self._grid_settings["custom_name"] = value
        return True

    def _on_position_changed(self, path, value):
        if value not in (0, 1, 2):
            return False
        logger.info("Position changed to %d", value)
        if self._grid_settings:
            self._grid_settings["position"] = value
        return True

    def _on_grid_setting_changed(self, setting, oldvalue, newvalue):
        logger.info("Grid setting '%s' changed: %s -> %s", setting, oldvalue, newvalue)
        if setting == "role" and newvalue != self._current_role:
            if newvalue in ALLOWED_ROLES and self._active_mac_id:
                mac_address = self._mac_id_to_address(self._active_mac_id)
                GLib.idle_add(self._create_grid_service, self._active_mac_id, mac_address, newvalue)
        elif setting == "custom_name" and self._grid_service is not None:
            self._grid_service["/CustomName"] = newvalue
        elif setting == "position" and self._grid_service is not None:
            self._grid_service["/Position"] = newvalue

    # ── Grid update loop ────────────────────────────────────────────────────

    def _update_grid(self) -> bool:
        """Called periodically to push BLE data to the grid D-Bus service."""
        if self._ble is None or self._grid_service is None:
            return False  # stop timer

        data = self._ble.get_data()
        connected = self._ble.connected
        self._grid_service["/Connected"] = 1 if connected else 0

        if data.timestamp > 0:
            l1 = data.l1
            self._grid_service["/Ac/L1/Voltage"] = round(l1.voltage, 1)
            self._grid_service["/Ac/L1/Current"] = round(l1.current, 2)
            self._grid_service["/Ac/L1/Power"] = round(l1.power, 0)
            self._grid_service["/Ac/L1/Energy/Forward"] = round(l1.energy, 2)
            if l1.frequency > 0:
                self._grid_service["/Ac/L1/Frequency"] = round(l1.frequency, 1)

            total_power = l1.power
            total_current = l1.current
            total_energy = l1.energy
            error_code = l1.error_code

            if data.has_l2:
                l2 = data.l2
                self._grid_service["/Ac/L2/Voltage"] = round(l2.voltage, 1)
                self._grid_service["/Ac/L2/Current"] = round(l2.current, 2)
                self._grid_service["/Ac/L2/Power"] = round(l2.power, 0)
                self._grid_service["/Ac/L2/Energy/Forward"] = round(l2.energy, 2)
                if l2.frequency > 0:
                    self._grid_service["/Ac/L2/Frequency"] = round(l2.frequency, 1)
                total_power += l2.power
                total_current += l2.current
                total_energy += l2.energy
                if l2.error_code > error_code:
                    error_code = l2.error_code

            self._grid_service["/NrOfPhases"] = 2 if data.has_l2 else 1
            self._grid_service["/Ac/Power"] = round(total_power, 0)
            self._grid_service["/Ac/Current"] = round(total_current, 2)
            avg_voltage = l1.voltage
            if data.has_l2 and data.l2.voltage > 0:
                avg_voltage = (l1.voltage + data.l2.voltage) / 2.0
            self._grid_service["/Ac/Voltage"] = round(avg_voltage, 1)
            if l1.frequency > 0:
                self._grid_service["/Ac/Frequency"] = round(l1.frequency, 1)
            self._grid_service["/Ac/Energy/Forward"] = round(total_energy, 2)
            self._grid_service["/ErrorCode"] = error_code

            self._update_index = (self._update_index + 1) % 256
            self._grid_service["/UpdateIndex"] = self._update_index

            if data.has_l2:
                logger.info(
                    "L1: %.1fV %.2fA %.0fW | L2: %.1fV %.2fA %.0fW | Total: %.0fW %.2fkWh %.1fHz",
                    l1.voltage, l1.current, l1.power,
                    data.l2.voltage, data.l2.current, data.l2.power,
                    total_power, total_energy, l1.frequency,
                )
            else:
                logger.info(
                    "%.1fV %.2fA %.0fW %.2fkWh %.1fHz",
                    l1.voltage, l1.current, l1.power, l1.energy, l1.frequency,
                )

        return True  # keep timer running

    # ── Settings persistence ────────────────────────────────────────────────

    def _ensure_poll_interval_setting(self, mac_id: str):
        """Ensure the PollIntervalMs setting exists for a device (idempotent)."""
        try:
            settings_proxy = self._bus.get_object("com.victronenergy.settings", "/Settings")
            settings_iface = dbus.Interface(settings_proxy, "com.victronenergy.Settings")
            group = "Devices/power_watchdog"
            prefix = "Device_%s" % mac_id
            settings_iface.AddSetting(
                group, "%s/PollIntervalMs" % prefix,
                DEFAULT_UPDATE_INTERVAL_MS, "i",
                POLL_INTERVAL_MS_PER_STEP, POLL_INTERVAL_MS_PER_STEP * 100,
            )
        except Exception:
            logger.exception("Failed to ensure PollIntervalMs setting for %s", mac_id)

    def _save_device_to_settings(self, mac_id: str, name: str, mac_address: str, enabled: bool):
        """Save a device's state to persistent settings via AddSetting."""
        try:
            settings_proxy = self._bus.get_object("com.victronenergy.settings", "/Settings")
            settings_iface = dbus.Interface(settings_proxy, "com.victronenergy.Settings")

            group = "Devices/power_watchdog"
            prefix = "Device_%s" % mac_id

            settings_iface.AddSetting(group, "%s/Enabled" % prefix, 0, "i", 0, 1)
            settings_iface.AddSetting(group, "%s/Name" % prefix, name, "s", 0, 0)
            settings_iface.AddSetting(
                group, "%s/PollIntervalMs" % prefix,
                DEFAULT_UPDATE_INTERVAL_MS, "i",
                POLL_INTERVAL_MS_PER_STEP, POLL_INTERVAL_MS_PER_STEP * 100,
            )
            if mac_address:
                settings_iface.AddSetting(group, "%s/MAC" % prefix, mac_address, "s", 0, 0)

            base = "/Settings/%s/%s" % (group, prefix)
            self._bus.get_object("com.victronenergy.settings", base + "/Enabled").SetValue(
                1 if enabled else 0
            )
            self._bus.get_object("com.victronenergy.settings", base + "/Name").SetValue(name)

            poll_ms = self._device_poll_ms.get(mac_id, DEFAULT_UPDATE_INTERVAL_MS)
            self._bus.get_object("com.victronenergy.settings", base + "/PollIntervalMs").SetValue(
                poll_ms
            )

            if mac_address:
                self._bus.get_object("com.victronenergy.settings", base + "/MAC").SetValue(mac_address)

            logger.info("Saved device %s to settings (enabled=%s, poll=%dms)", mac_id, enabled, poll_ms)

        except Exception:
            logger.exception("Failed to save device %s to settings", mac_id)

    def _save_poll_interval_to_settings(self, mac_id: str, poll_ms: int):
        """Persist a device's polling interval."""
        try:
            # Ensure the setting exists (AddSetting is idempotent — won't
            # overwrite an existing value, only creates if missing)
            self._ensure_poll_interval_setting(mac_id)

            path = "/Settings/Devices/power_watchdog/Device_%s/PollIntervalMs" % mac_id
            proxy = self._bus.get_object("com.victronenergy.settings", path)
            proxy.SetValue(poll_ms)
            logger.debug("Persisted PollIntervalMs=%d for %s", poll_ms, mac_id)
        except Exception:
            logger.exception("Failed to save PollIntervalMs for %s", mac_id)

    def _restore_devices_from_settings(self):
        """Restore previously discovered devices from com.victronenergy.settings."""
        try:
            settings_proxy = self._bus.get_object(
                "com.victronenergy.settings", "/Settings/Devices/power_watchdog",
            )
            settings_iface = dbus.Interface(
                settings_proxy, "org.freedesktop.DBus.Introspectable"
            )
            xml_str = settings_iface.Introspect()
            root = ET.fromstring(xml_str)

            restored = 0
            for node in root.findall("node"):
                node_name = node.get("name", "")
                if not node_name.startswith("Device_"):
                    continue

                mac_id = node_name.replace("Device_", "")

                try:
                    device_path = "/Settings/Devices/power_watchdog/%s" % node_name
                    dev_proxy = self._bus.get_object("com.victronenergy.settings", device_path)
                    dev_iface = dbus.Interface(dev_proxy, "org.freedesktop.DBus.Introspectable")
                    dev_xml = dev_iface.Introspect()
                    dev_root = ET.fromstring(dev_xml)

                    sub_names = {n.get("name") for n in dev_root.findall("node")}
                    if "Enabled" not in sub_names:
                        continue

                    enabled = int(self._bus.get_object(
                        "com.victronenergy.settings", "%s/Enabled" % device_path
                    ).GetValue())

                    name = mac_id
                    if "Name" in sub_names:
                        name = str(self._bus.get_object(
                            "com.victronenergy.settings", "%s/Name" % device_path
                        ).GetValue())

                    mac_address = ""
                    if "MAC" in sub_names:
                        mac_address = str(self._bus.get_object(
                            "com.victronenergy.settings", "%s/MAC" % device_path
                        ).GetValue())

                    poll_ms = DEFAULT_UPDATE_INTERVAL_MS
                    if "PollIntervalMs" in sub_names:
                        poll_ms = int(self._bus.get_object(
                            "com.victronenergy.settings", "%s/PollIntervalMs" % device_path
                        ).GetValue())
                    else:
                        # Setting missing (device saved by older code) — create it
                        self._ensure_poll_interval_setting(mac_id)

                    classified = classify_device(name)
                    if classified is not None:
                        classified.mac = mac_address
                        self._device_info[mac_id] = classified
                    elif mac_address:
                        self._device_info[mac_id] = DiscoveredDevice(mac=mac_address, name=name)

                    self._create_device_switch(mac_id, name, enabled=bool(enabled), poll_ms=poll_ms)
                    restored += 1

                except Exception:
                    logger.exception("Failed to restore device %s", node_name)

            if restored > 0:
                logger.info("Restored %d device(s) from settings", restored)

        except dbus.exceptions.DBusException:
            logger.debug("No saved devices to restore (fresh install)")
        except Exception:
            logger.exception("Error restoring devices from settings")

    # ── Shutdown ────────────────────────────────────────────────────────────

    def stop(self):
        """Clean shutdown: stop scanning, deactivate device."""
        self._stop_scanning()
        self._deactivate_device()
        logger.info("Service stopped")


def main():
    DBusGMainLoop(set_as_default=True)

    service = PowerWatchdogService()

    mainloop = GLib.MainLoop()

    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        service.stop()
        mainloop.quit()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("dbus-power-watchdog v%s started", VERSION)

    mainloop.run()


if __name__ == "__main__":
    main()
