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
Discovery and management service for Hughes Power Watchdog devices on Venus OS.

Registers as a Venus OS switch device (com.victronenergy.switch.power_watchdog)
with a discovery toggle and per-device enable/disable toggles.  When discovery
is enabled, periodically scans BLE for Power Watchdog devices (both gen1 PM*
and gen2 WD_* name patterns).  When a device toggle is enabled, spawns a child
process (power_watchdog_device.py) to handle the BLE connection and D-Bus
grid/pvinverter/genset service for that device.

Supports both 30A (single-line) and 50A (dual-line L1+L2) models.
"""

import asyncio
import configparser
import logging
import os
import signal
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET

import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

# Add ext folders to sys.path
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

# Use bleak from dbus-serialbattery's vendored copy if not installed system-wide
_serialbattery_ext = "/data/apps/dbus-serialbattery/ext"
if os.path.isdir(_serialbattery_ext) and _serialbattery_ext not in sys.path:
    sys.path.insert(2, _serialbattery_ext)

from vedbus import VeDbusService  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

from power_watchdog_ble import scan_for_devices, classify_device, DiscoveredDevice  # noqa: E402

VERSION = "0.6.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dbus-power-watchdog")

# Path to the per-device service script (same directory as this file)
DEVICE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "power_watchdog_device.py")

# Default scan interval when discovery is enabled (seconds)
DEFAULT_SCAN_INTERVAL = 60

# Default D-Bus update interval passed to child processes (seconds)
DEFAULT_UPDATE_INTERVAL = 5

# Default reconnect parameters passed to child processes
DEFAULT_RECONNECT_DELAY = 10
DEFAULT_RECONNECT_MAX_DELAY = 120


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


# ── Discovery / Management Service ──────────────────────────────────────────

class PowerWatchdogDiscoveryService:
    """Venus OS switch device that discovers Power Watchdog devices via BLE
    and manages per-device child processes."""

    def __init__(self):
        config = load_config()
        defaults = config["DEFAULT"] if "DEFAULT" in config else {}

        # Config values
        self._scan_interval = int(defaults.get("scan_interval", str(DEFAULT_SCAN_INTERVAL)))
        self._update_interval = int(defaults.get("update_interval", str(DEFAULT_UPDATE_INTERVAL)))
        self._reconnect_delay = float(defaults.get("reconnect_delay", str(DEFAULT_RECONNECT_DELAY)))
        self._reconnect_max_delay = float(defaults.get("reconnect_max_delay", str(DEFAULT_RECONNECT_MAX_DELAY)))

        # Collect available BLE adapters from config or auto-detect
        adapter_config = defaults.get("bluetooth_adapters", "").strip()
        if adapter_config:
            self._adapters = [a.strip() for a in adapter_config.split(",") if a.strip()]
        else:
            self._adapters = self._detect_adapters()

        logger.info("Scan interval: %ds", self._scan_interval)
        logger.info("BLE adapters: %s", self._adapters or ["default"])

        # D-Bus connection
        self._bus = get_bus()

        # Child processes: mac_id -> subprocess.Popen
        self._children: dict[str, subprocess.Popen] = {}

        # Discovered device info cache: mac_id -> DiscoveredDevice
        self._device_info: dict[str, DiscoveredDevice] = {}

        # Scanning state
        self._scan_timer_id = None
        self._scanning = False

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
        self._dbusservice.add_path("/State", 0x100)  # Required for GUI device list visibility

        # ── Discovery toggle switch ─────────────────────────────────────
        discovery_path = "/SwitchableOutput/relay_discovery"
        self._dbusservice.add_path(
            "%s/Name" % discovery_path, "* Power Watchdog Device Discovery"
        )
        self._dbusservice.add_path("%s/Type" % discovery_path, 1)  # toggle
        self._dbusservice.add_path(
            "%s/State" % discovery_path, 0,
            writeable=True,
            onchangecallback=self._on_discovery_changed,
        )
        self._dbusservice.add_path("%s/Status" % discovery_path, 0x00)
        self._dbusservice.add_path("%s/Current" % discovery_path, 0)

        # Settings paths for the discovery toggle
        self._dbusservice.add_path("%s/Settings/CustomName" % discovery_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/Type" % discovery_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidTypes" % discovery_path, 2)
        self._dbusservice.add_path("%s/Settings/Function" % discovery_path, 2, writeable=True)
        self._dbusservice.add_path("%s/Settings/ValidFunctions" % discovery_path, 4)
        self._dbusservice.add_path("%s/Settings/Group" % discovery_path, "", writeable=True)
        self._dbusservice.add_path("%s/Settings/ShowUIControl" % discovery_path, 1, writeable=True)
        self._dbusservice.add_path("%s/Settings/PowerOnState" % discovery_path, 1)

        # ── Persistent settings ─────────────────────────────────────────
        settings = {
            "ClassAndVrmInstance": [
                "/Settings/Devices/power_watchdog/ClassAndVrmInstance",
                "switch:120",
                0,
                0,
            ],
            "DiscoveryEnabled": [
                "/Settings/Devices/power_watchdog/DiscoveryEnabled",
                0,  # Default: OFF
                0,
                1,
            ],
        }
        self._settings = SettingsDevice(
            self._bus,
            settings,
            eventCallback=self._on_settings_changed,
            timeout=10,
        )

        # Register service on D-Bus
        self._dbusservice.register()
        logger.info("Registered discovery service on D-Bus")

        # Restore previously discovered devices from settings
        self._restore_devices_from_settings()

        # Restore discovery state
        discovery_state = self._settings["DiscoveryEnabled"]
        self._dbusservice["/SwitchableOutput/relay_discovery/State"] = discovery_state
        if discovery_state:
            logger.info("Discovery enabled from saved settings")
            self._start_scanning()

        # Re-spawn child processes for enabled devices
        self._respawn_enabled_children()

        # Periodic health check for child processes (every 30s)
        GLib.timeout_add_seconds(30, self._health_check)

    # ── Adapter detection ───────────────────────────────────────────────────

    @staticmethod
    def _detect_adapters() -> list[str]:
        """Detect available Bluetooth adapters on the system."""
        adapters = []
        try:
            hci_dir = "/sys/class/bluetooth"
            if os.path.isdir(hci_dir):
                for entry in sorted(os.listdir(hci_dir)):
                    if entry.startswith("hci"):
                        adapters.append(entry)
        except Exception:
            logger.exception("Failed to detect BLE adapters")
        if not adapters:
            adapters = [""]  # default adapter
        return adapters

    # ── Discovery toggle callbacks ──────────────────────────────────────────

    def _on_discovery_changed(self, path, value):
        """Called when the discovery toggle is changed."""
        enabled = (value == 1)
        logger.info("Discovery %s", "enabled" if enabled else "disabled")
        self._settings["DiscoveryEnabled"] = value

        if enabled:
            self._start_scanning()
            # Show all per-device switches
            for mac_id in self._device_info:
                show_path = "/SwitchableOutput/relay_%s/Settings/ShowUIControl" % mac_id
                if show_path in self._dbusservice:
                    self._dbusservice[show_path] = 1
        else:
            self._stop_scanning()
            # Keep enabled devices visible, only hide disabled ones
            for mac_id in self._device_info:
                show_path = "/SwitchableOutput/relay_%s/Settings/ShowUIControl" % mac_id
                state_path = "/SwitchableOutput/relay_%s/State" % mac_id
                if show_path in self._dbusservice and state_path in self._dbusservice:
                    device_enabled = self._dbusservice[state_path]
                    self._dbusservice[show_path] = 1 if device_enabled else 0

        return True

    def _on_settings_changed(self, setting, old_value, new_value):
        """Callback when a setting changes in com.victronenergy.settings."""
        logger.debug("Setting changed: %s = %s", setting, new_value)

    # ── BLE Scanning ────────────────────────────────────────────────────────

    def _start_scanning(self):
        """Start periodic BLE scanning for Power Watchdog devices."""
        if self._scan_timer_id is not None:
            return  # already scanning

        logger.info("Starting BLE discovery (interval: %ds)", self._scan_interval)
        # Run first scan immediately (one-shot; must return False to avoid
        # GLib re-invoking on every idle iteration)
        GLib.idle_add(lambda: (self._trigger_scan(), False)[-1])
        # Then schedule periodic scans
        self._scan_timer_id = GLib.timeout_add_seconds(
            self._scan_interval, self._trigger_scan
        )

    def _stop_scanning(self):
        """Stop periodic BLE scanning."""
        if self._scan_timer_id is not None:
            GLib.source_remove(self._scan_timer_id)
            self._scan_timer_id = None
            logger.info("Stopped BLE discovery")

    def _trigger_scan(self) -> bool:
        """Run a BLE scan in a background thread to avoid blocking GLib."""
        if self._scanning:
            logger.debug("Scan already in progress, skipping")
            return True

        self._scanning = True

        thread = threading.Thread(
            target=self._run_scan_thread,
            name="PowerWatchdog_Discovery",
            daemon=True,
        )
        thread.start()

        return True  # keep timer running

    def _run_scan_thread(self):
        """Execute BLE scan in a background thread, then process results on GLib."""
        try:
            loop = asyncio.new_event_loop()
            found = loop.run_until_complete(
                scan_for_devices(adapters=self._adapters, timeout=15.0)
            )
            loop.close()

            # Process results on the GLib main loop thread
            GLib.idle_add(self._process_scan_results, found)

        except Exception:
            logger.exception("BLE scan failed")
        finally:
            self._scanning = False

    def _process_scan_results(self, found: list[DiscoveredDevice]) -> bool:
        """Process scan results on the GLib main thread."""
        for device in found:
            mac_id = device.mac.replace(":", "").lower()

            if mac_id in self._device_info:
                # Already known device
                continue

            # New device discovered
            logger.info(
                "New Power Watchdog discovered: %s (%s) gen%d %s",
                device.name, device.mac, device.generation, device.device_type,
            )
            self._device_info[mac_id] = device

            # Create a switch for this device (default: disabled)
            self._create_device_switch(mac_id, device.name, enabled=False)
            self._save_device_to_settings(mac_id, device.name, device.mac, enabled=False)

        return False  # don't repeat (one-shot idle callback)

    # ── Per-device switch management ────────────────────────────────────────

    def _create_device_switch(self, mac_id: str, name: str, enabled: bool):
        """Create a D-Bus switchable output for a discovered device."""
        output_path = "/SwitchableOutput/relay_%s" % mac_id

        # Don't duplicate
        if "%s/State" % output_path in self._dbusservice:
            return

        with self._dbusservice as ctx:
            ctx.add_path("%s/Name" % output_path, name)
            ctx.add_path("%s/Type" % output_path, 1)  # toggle
            ctx.add_path(
                "%s/State" % output_path, 1 if enabled else 0,
                writeable=True,
                onchangecallback=self._on_device_state_changed,
            )
            ctx.add_path("%s/Status" % output_path, 0)
            ctx.add_path("%s/Current" % output_path, 0)
            ctx.add_path("%s/Settings/CustomName" % output_path, "", writeable=True)
            ctx.add_path("%s/Settings/Type" % output_path, 1, writeable=True)
            ctx.add_path("%s/Settings/ValidTypes" % output_path, 2)
            ctx.add_path("%s/Settings/Function" % output_path, 2, writeable=True)
            ctx.add_path("%s/Settings/ValidFunctions" % output_path, 4)
            ctx.add_path("%s/Settings/Group" % output_path, "", writeable=True)
            ctx.add_path("%s/Settings/ShowUIControl" % output_path, 1, writeable=True)
            ctx.add_path("%s/Settings/PowerOnState" % output_path, 1)

        logger.info("Created switch for device %s (%s), enabled=%s", name, mac_id, enabled)

    def _on_device_state_changed(self, path, value):
        """Called when a per-device toggle is changed."""
        # Extract mac_id from path like "/SwitchableOutput/relay_24ec4ae469a5/State"
        path_parts = path.split("/")
        if len(path_parts) < 3 or not path_parts[2].startswith("relay_"):
            return True

        mac_id = path_parts[2].replace("relay_", "")
        enabled = (value == 1)

        # Get device name
        name_path = "/SwitchableOutput/relay_%s/Name" % mac_id
        device_name = self._dbusservice[name_path] if name_path in self._dbusservice else mac_id

        logger.info(
            "Device '%s' (%s): %s",
            device_name, mac_id, "enabled" if enabled else "disabled",
        )

        # Persist state
        mac_address = ""
        if mac_id in self._device_info:
            mac_address = self._device_info[mac_id].mac
        self._save_device_to_settings(mac_id, device_name, mac_address, enabled)

        # Start or stop the child process
        if enabled:
            self._start_child(mac_id)
        else:
            self._stop_child(mac_id)

        return True

    # ── Child process management ────────────────────────────────────────────

    def _mac_id_to_address(self, mac_id: str) -> str:
        """Convert a MAC ID (no colons) back to a MAC address."""
        if mac_id in self._device_info:
            return self._device_info[mac_id].mac

        # Reconstruct from mac_id
        return ":".join(mac_id[i:i+2].upper() for i in range(0, 12, 2))

    def _start_child(self, mac_id: str):
        """Spawn a child process for the given device."""
        if mac_id in self._children:
            proc = self._children[mac_id]
            if proc.poll() is None:
                logger.info("Child for %s already running (pid %d)", mac_id, proc.pid)
                return

        mac_address = self._mac_id_to_address(mac_id)

        cmd = [
            sys.executable, "-u", DEVICE_SCRIPT,
            "--mac", mac_address,
            "--update-interval", str(self._update_interval),
            "--reconnect-delay", str(self._reconnect_delay),
            "--reconnect-max-delay", str(self._reconnect_max_delay),
        ]

        # Pass adapter preference if we have adapters configured
        if self._adapters and self._adapters[0]:
            cmd.extend(["--adapter", self._adapters[0]])

        try:
            # Let children inherit our stdout/stderr so their logs go to
            # multilog alongside ours.  Piping would cause BrokenPipeError
            # in children if the discovery process dies unexpectedly.
            proc = subprocess.Popen(cmd)
            self._children[mac_id] = proc
            logger.info(
                "Started child process for %s (pid %d): %s",
                mac_address, proc.pid, " ".join(cmd),
            )

        except Exception:
            logger.exception("Failed to start child process for %s", mac_address)

    def _stop_child(self, mac_id: str):
        """Stop the child process for the given device."""
        if mac_id not in self._children:
            return

        proc = self._children[mac_id]
        if proc.poll() is None:
            logger.info("Sending SIGTERM to child for %s (pid %d)", mac_id, proc.pid)
            proc.terminate()
            try:
                # Keep under daemontools' ~5s SIGTERM-to-SIGKILL window
                proc.wait(timeout=3)
                logger.info("Child for %s exited cleanly", mac_id)
            except subprocess.TimeoutExpired:
                logger.warning("Child for %s did not exit in 3s, sending SIGKILL", mac_id)
                proc.kill()
                proc.wait(timeout=2)

        del self._children[mac_id]

    def _health_check(self) -> bool:
        """Periodic check that enabled devices have running children."""
        for mac_id, proc in list(self._children.items()):
            if proc.poll() is not None:
                exit_code = proc.returncode
                logger.warning(
                    "Child for %s exited unexpectedly (code %d), restarting...",
                    mac_id, exit_code,
                )
                del self._children[mac_id]
                # Re-check if still enabled
                state_path = "/SwitchableOutput/relay_%s/State" % mac_id
                if state_path in self._dbusservice and self._dbusservice[state_path] == 1:
                    self._start_child(mac_id)
        return True  # keep timer running

    def _respawn_enabled_children(self):
        """On startup, spawn child processes for all enabled devices."""
        for mac_id in self._device_info:
            state_path = "/SwitchableOutput/relay_%s/State" % mac_id
            if state_path in self._dbusservice and self._dbusservice[state_path] == 1:
                self._start_child(mac_id)

    # ── Settings persistence ────────────────────────────────────────────────

    def _save_device_to_settings(self, mac_id: str, name: str, mac_address: str, enabled: bool):
        """Save a device's state to persistent settings via AddSetting."""
        try:
            settings_proxy = self._bus.get_object(
                "com.victronenergy.settings", "/Settings"
            )
            settings_iface = dbus.Interface(
                settings_proxy, "com.victronenergy.Settings"
            )

            group = "Devices/power_watchdog"
            prefix = "Device_%s" % mac_id

            # Add/update Enabled setting
            try:
                settings_iface.AddSetting(
                    group, "%s/Enabled" % prefix,
                    1 if enabled else 0,
                    "i", 0, 1,
                )
            except dbus.exceptions.DBusException:
                path = "/Settings/%s/%s/Enabled" % (group, prefix)
                proxy = self._bus.get_object("com.victronenergy.settings", path)
                proxy.SetValue(1 if enabled else 0)

            # Add/update Name setting
            try:
                settings_iface.AddSetting(
                    group, "%s/Name" % prefix,
                    name, "s", 0, 0,
                )
            except dbus.exceptions.DBusException:
                path = "/Settings/%s/%s/Name" % (group, prefix)
                proxy = self._bus.get_object("com.victronenergy.settings", path)
                proxy.SetValue(name)

            # Add/update MAC setting
            if mac_address:
                try:
                    settings_iface.AddSetting(
                        group, "%s/MAC" % prefix,
                        mac_address, "s", 0, 0,
                    )
                except dbus.exceptions.DBusException:
                    path = "/Settings/%s/%s/MAC" % (group, prefix)
                    proxy = self._bus.get_object("com.victronenergy.settings", path)
                    proxy.SetValue(mac_address)

        except Exception:
            logger.exception("Failed to save device %s to settings", mac_id)

    def _restore_devices_from_settings(self):
        """Restore previously discovered devices from com.victronenergy.settings."""
        try:
            settings_proxy = self._bus.get_object(
                "com.victronenergy.settings",
                "/Settings/Devices/power_watchdog",
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

                    # Read sub-nodes
                    dev_proxy = self._bus.get_object(
                        "com.victronenergy.settings", device_path
                    )
                    dev_iface = dbus.Interface(
                        dev_proxy, "org.freedesktop.DBus.Introspectable"
                    )
                    dev_xml = dev_iface.Introspect()
                    dev_root = ET.fromstring(dev_xml)

                    has_enabled = any(
                        n.get("name") == "Enabled" for n in dev_root.findall("node")
                    )
                    has_name = any(
                        n.get("name") == "Name" for n in dev_root.findall("node")
                    )
                    has_mac = any(
                        n.get("name") == "MAC" for n in dev_root.findall("node")
                    )

                    if not has_enabled:
                        continue

                    enabled_proxy = self._bus.get_object(
                        "com.victronenergy.settings",
                        "%s/Enabled" % device_path,
                    )
                    enabled = int(enabled_proxy.GetValue())

                    name = mac_id
                    if has_name:
                        name_proxy = self._bus.get_object(
                            "com.victronenergy.settings",
                            "%s/Name" % device_path,
                        )
                        name = str(name_proxy.GetValue())

                    mac_address = ""
                    if has_mac:
                        mac_proxy = self._bus.get_object(
                            "com.victronenergy.settings",
                            "%s/MAC" % device_path,
                        )
                        mac_address = str(mac_proxy.GetValue())

                    # Re-classify if possible (from saved name)
                    classified = classify_device(name)
                    if classified is not None:
                        classified.mac = mac_address
                        self._device_info[mac_id] = classified
                    elif mac_address:
                        # Create a minimal DiscoveredDevice
                        self._device_info[mac_id] = DiscoveredDevice(
                            mac=mac_address, name=name,
                        )

                    # Create the toggle switch
                    self._create_device_switch(mac_id, name, enabled=bool(enabled))
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
        """Clean shutdown: stop scanning and terminate all children."""
        self._stop_scanning()

        for mac_id in list(self._children.keys()):
            self._stop_child(mac_id)

        logger.info("All child processes stopped")


def main():
    DBusGMainLoop(set_as_default=True)

    service = PowerWatchdogDiscoveryService()

    mainloop = GLib.MainLoop()

    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        service.stop()
        mainloop.quit()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("dbus-power-watchdog v%s discovery service started", VERSION)

    mainloop.run()


if __name__ == "__main__":
    main()
