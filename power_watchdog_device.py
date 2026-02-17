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
Per-device D-Bus service for a single Hughes Power Watchdog.

Spawned by the discovery process (dbus-power-watchdog.py) for each
enabled Power Watchdog device.  Connects via BLE, reads AC line data,
and publishes as com.victronenergy.grid (or genset/pvinverter depending
on configured role) so that Venus OS treats the data as an AC input.

Usage:
    python3 power_watchdog_device.py --mac XX:XX:XX:XX:XX:XX [--adapter hciN] \\
        [--update-interval-ms 5000] [--reconnect-delay 10] [--reconnect-max-delay 120]
"""

import argparse
import logging
import os
import platform
import signal
import sys

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
from power_watchdog_ble import PowerWatchdogBLE, WatchdogData  # noqa: E402

VERSION = "0.6.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("power-watchdog-device")

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


# ── Formatters ──────────────────────────────────────────────────────────────

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

class PowerWatchdogDeviceService:
    """Venus OS D-Bus grid meter service for a single Power Watchdog BLE device."""

    def __init__(self, mac_address: str, adapter: str = "",
                 update_interval_ms: int = 5000,
                 reconnect_delay: float = 10.0,
                 reconnect_max_delay: float = 120.0):
        self._mac_address = mac_address
        self._mac_id = mac_address.replace(":", "").lower()
        self._update_interval_ms = update_interval_ms

        logger.info("Power Watchdog MAC: %s", self._mac_address)
        logger.info("BLE adapter: %s", adapter or "auto")
        logger.info("Update interval: %dms", self._update_interval_ms)

        # Start BLE client in daemon thread
        self._ble = PowerWatchdogBLE(
            address=self._mac_address,
            reconnect_delay=reconnect_delay,
            reconnect_max_delay=reconnect_max_delay,
            lock_config=LockConfig(enabled=True),
            scan_lock_config=ScanLockConfig(enabled=True),
        )

        # D-Bus connection
        self._bus = get_bus()

        # Persistent settings via com.victronenergy.settings (localsettings)
        settings_path = "/Settings/Devices/power_watchdog_%s" % self._mac_id
        global_path = "/Settings/Devices/power_watchdog"
        self._settings = SettingsDevice(
            bus=dbus.SystemBus(private=True) if "DBUS_SESSION_BUS_ADDRESS" not in os.environ
                else dbus.SessionBus(private=True),
            supportedSettings={
                "role": ["%s/Role" % settings_path, "grid", 0, 0],
                "custom_name": ["%s/CustomName" % settings_path, "Power Watchdog", 0, 0],
                "position": ["%s/Position" % settings_path, 0, 0, 2],
                "poll_interval_ms": [
                    "%s/PollIntervalMs" % global_path,
                    update_interval_ms,  # default from CLI arg
                    100,   # min
                    10000, # max
                ],
            },
            eventCallback=self._handle_setting_changed,
            timeout=10,
        )

        # Override CLI interval with the live setting value (parent may have
        # changed it after we were spawned)
        try:
            saved_ms = int(self._settings["poll_interval_ms"])
            if 100 <= saved_ms <= 10000:
                self._update_interval_ms = saved_ms
                logger.info("Polling interval from settings: %dms", saved_ms)
        except Exception:
            pass  # keep CLI default

        # State
        self._dbusservice = None
        self._update_index = 0
        self._timer_id = None
        self._current_role = None

        # Create the D-Bus service with the persisted role
        role = self._settings["role"]
        if role not in ALLOWED_ROLES:
            logger.warning("Persisted role '%s' invalid, falling back to 'grid'", role)
            role = "grid"
        self._create_service(role)

    def _create_service(self, role: str):
        """Create (or re-create) the D-Bus service for the given role."""
        if self._dbusservice is not None:
            # Tear down the existing service
            if self._timer_id is not None:
                GLib.source_remove(self._timer_id)
                self._timer_id = None
            del self._dbusservice
            self._dbusservice = None
            logger.info("Torn down old D-Bus service (was %s)", self._current_role)

        service_class = ROLE_TO_SERVICE.get(role, ROLE_TO_SERVICE["grid"])
        # Use MAC-based suffix for unique service name per device
        servicename = "%s.power_watchdog_%s" % (service_class, self._mac_id)
        self._current_role = role

        self._dbusservice = VeDbusService(servicename, self._bus, register=False)

        # Management paths
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion", "Python " + platform.python_version()
        )
        self._dbusservice.add_path(
            "/Mgmt/Connection", "BLE " + self._mac_address
        )

        # Mandatory device paths
        # DeviceInstance: hash the MAC to get a stable, unique instance number
        # Use range 40-299 to avoid conflicts with other devices
        device_instance = 40 + (int(self._mac_id, 16) % 260)
        self._dbusservice.add_path("/DeviceInstance", device_instance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", "Hughes Power Watchdog")
        self._dbusservice.add_path("/CustomName", self._settings["custom_name"],
                                   writeable=True,
                                   onchangecallback=self._on_custom_name_changed)
        self._dbusservice.add_path("/FirmwareVersion", VERSION)
        self._dbusservice.add_path("/HardwareVersion", "BLE")
        self._dbusservice.add_path("/Connected", 0)
        self._dbusservice.add_path("/Serial", self._mac_address)

        # Role & type
        self._dbusservice.add_path("/DeviceType", 0)
        self._dbusservice.add_path("/Role", role,
                                   writeable=True,
                                   onchangecallback=self._on_role_changed)
        self._dbusservice.add_path("/AllowedRoles", ALLOWED_ROLES)

        # Phase count -- updated dynamically when data arrives
        self._dbusservice.add_path("/NrOfPhases", None)

        # Position (for pvinverter role: 0=AC-In1, 1=AC-Out, 2=AC-In2)
        self._dbusservice.add_path("/Position", self._settings["position"],
                                   writeable=True,
                                   onchangecallback=self._on_position_changed)

        # Refresh time (measurement interval in ms)
        self._dbusservice.add_path("/RefreshTime", self._update_interval_ms)

        # AC total paths
        self._dbusservice.add_path("/Ac/Power", None, gettextcallback=_fmt_w)
        self._dbusservice.add_path("/Ac/Current", None, gettextcallback=_fmt_a)
        self._dbusservice.add_path("/Ac/Voltage", None, gettextcallback=_fmt_v)
        self._dbusservice.add_path("/Ac/Frequency", None, gettextcallback=_fmt_hz)
        self._dbusservice.add_path("/Ac/Energy/Forward", None, gettextcallback=_fmt_kwh)
        self._dbusservice.add_path("/Ac/Energy/Reverse", 0)

        # L1 paths
        self._dbusservice.add_path("/Ac/L1/Voltage", None, gettextcallback=_fmt_v)
        self._dbusservice.add_path("/Ac/L1/Current", None, gettextcallback=_fmt_a)
        self._dbusservice.add_path("/Ac/L1/Power", None, gettextcallback=_fmt_w)
        self._dbusservice.add_path("/Ac/L1/Energy/Forward", None, gettextcallback=_fmt_kwh)
        self._dbusservice.add_path("/Ac/L1/Energy/Reverse", 0)
        self._dbusservice.add_path("/Ac/L1/Frequency", None, gettextcallback=_fmt_hz)

        # L2 paths (populated only on 50A dual-line models)
        self._dbusservice.add_path("/Ac/L2/Voltage", None, gettextcallback=_fmt_v)
        self._dbusservice.add_path("/Ac/L2/Current", None, gettextcallback=_fmt_a)
        self._dbusservice.add_path("/Ac/L2/Power", None, gettextcallback=_fmt_w)
        self._dbusservice.add_path("/Ac/L2/Energy/Forward", None, gettextcallback=_fmt_kwh)
        self._dbusservice.add_path("/Ac/L2/Energy/Reverse", 0)
        self._dbusservice.add_path("/Ac/L2/Frequency", None, gettextcallback=_fmt_hz)

        # Error code
        self._dbusservice.add_path("/ErrorCode", 0)

        # Update index (0-255, incremented on each update)
        self._update_index = 0
        self._dbusservice.add_path("/UpdateIndex", 0)

        # Register on D-Bus
        self._dbusservice.register()
        logger.info("Registered on D-Bus as %s (role=%s)", servicename, role)

        # Set up periodic update via GLib timer (milliseconds)
        self._timer_id = GLib.timeout_add(self._update_interval_ms, self._update)

    # ── Settings callbacks ──────────────────────────────────────────────────

    def _on_role_changed(self, path, value):
        """Called when the GUI writes to /Role on our D-Bus service."""
        if value not in ALLOWED_ROLES:
            logger.warning("Rejected invalid role '%s'", value)
            return False
        if value == self._current_role:
            return True

        logger.info("Role change requested: %s -> %s", self._current_role, value)
        self._settings["role"] = value
        GLib.idle_add(self._create_service, value)
        return True

    def _on_custom_name_changed(self, path, value):
        """Called when the GUI writes to /CustomName on our D-Bus service."""
        logger.info("CustomName changed to '%s'", value)
        self._settings["custom_name"] = value
        return True

    def _on_position_changed(self, path, value):
        """Called when the GUI writes to /Position on our D-Bus service."""
        if value not in (0, 1, 2):
            return False
        logger.info("Position changed to %d", value)
        self._settings["position"] = value
        return True

    def _handle_setting_changed(self, setting, oldvalue, newvalue):
        """Called when a setting is changed externally."""
        logger.info("Setting '%s' changed: %s -> %s", setting, oldvalue, newvalue)

        if setting == "role" and newvalue != self._current_role:
            if newvalue in ALLOWED_ROLES:
                GLib.idle_add(self._create_service, newvalue)
        elif setting == "custom_name" and self._dbusservice is not None:
            self._dbusservice["/CustomName"] = newvalue
        elif setting == "position" and self._dbusservice is not None:
            self._dbusservice["/Position"] = newvalue
        elif setting == "poll_interval_ms":
            self._reschedule_timer(int(newvalue))

    def _reschedule_timer(self, new_ms: int):
        """Change the polling interval without restarting BLE."""
        new_ms = max(100, min(new_ms, 10000))
        if new_ms == self._update_interval_ms:
            return
        logger.info("Polling interval changed: %dms -> %dms", self._update_interval_ms, new_ms)
        self._update_interval_ms = new_ms
        # Cancel existing timer and start a new one at the new rate
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
        self._timer_id = GLib.timeout_add(self._update_interval_ms, self._update)
        # Update the D-Bus RefreshTime path
        if self._dbusservice is not None:
            self._dbusservice["/RefreshTime"] = self._update_interval_ms

    # ── Update loop ─────────────────────────────────────────────────────────

    def _update(self) -> bool:
        """Called periodically by GLib to push BLE data to D-Bus."""
        data = self._ble.get_data()
        connected = self._ble.connected

        self._dbusservice["/Connected"] = 1 if connected else 0

        if data.timestamp > 0:
            l1 = data.l1

            # L1
            self._dbusservice["/Ac/L1/Voltage"] = round(l1.voltage, 1)
            self._dbusservice["/Ac/L1/Current"] = round(l1.current, 2)
            self._dbusservice["/Ac/L1/Power"] = round(l1.power, 0)
            self._dbusservice["/Ac/L1/Energy/Forward"] = round(l1.energy, 2)
            if l1.frequency > 0:
                self._dbusservice["/Ac/L1/Frequency"] = round(l1.frequency, 1)

            total_power = l1.power
            total_current = l1.current
            total_energy = l1.energy
            error_code = l1.error_code

            if data.has_l2:
                l2 = data.l2

                # L2
                self._dbusservice["/Ac/L2/Voltage"] = round(l2.voltage, 1)
                self._dbusservice["/Ac/L2/Current"] = round(l2.current, 2)
                self._dbusservice["/Ac/L2/Power"] = round(l2.power, 0)
                self._dbusservice["/Ac/L2/Energy/Forward"] = round(l2.energy, 2)
                if l2.frequency > 0:
                    self._dbusservice["/Ac/L2/Frequency"] = round(l2.frequency, 1)

                total_power += l2.power
                total_current += l2.current
                total_energy += l2.energy
                if l2.error_code > error_code:
                    error_code = l2.error_code

            # Phase count (updated from data so 30A=1, 50A=2)
            self._dbusservice["/NrOfPhases"] = 2 if data.has_l2 else 1

            # Totals
            self._dbusservice["/Ac/Power"] = round(total_power, 0)
            self._dbusservice["/Ac/Current"] = round(total_current, 2)
            avg_voltage = l1.voltage
            if data.has_l2 and data.l2.voltage > 0:
                avg_voltage = (l1.voltage + data.l2.voltage) / 2.0
            self._dbusservice["/Ac/Voltage"] = round(avg_voltage, 1)
            if l1.frequency > 0:
                self._dbusservice["/Ac/Frequency"] = round(l1.frequency, 1)
            self._dbusservice["/Ac/Energy/Forward"] = round(total_energy, 2)
            self._dbusservice["/ErrorCode"] = error_code

            # Bump update index
            self._update_index = (self._update_index + 1) % 256
            self._dbusservice["/UpdateIndex"] = self._update_index

            if data.has_l2:
                logger.info(
                    "L1: %.1fV %.2fA %.0fW | L2: %.1fV %.2fA %.0fW | "
                    "Total: %.0fW %.2fkWh %.1fHz",
                    l1.voltage, l1.current, l1.power,
                    data.l2.voltage, data.l2.current, data.l2.power,
                    total_power, total_energy, l1.frequency,
                )
            else:
                logger.info(
                    "%.1fV %.2fA %.0fW %.2fkWh %.1fHz",
                    l1.voltage, l1.current, l1.power,
                    l1.energy, l1.frequency,
                )
        else:
            logger.debug("No data yet from Power Watchdog")

        return True  # keep timer running

    def stop(self):
        """Clean shutdown: stop BLE client."""
        self._ble.stop()


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Power Watchdog BLE device service for Venus OS"
    )
    parser.add_argument(
        "--mac", required=True,
        help="MAC address of the Power Watchdog (e.g., AA:BB:CC:DD:EE:FF)"
    )
    parser.add_argument(
        "--adapter", default="",
        help="Bluetooth adapter to use (e.g., hci0, hci1). Empty for auto."
    )
    parser.add_argument(
        "--update-interval-ms", type=int, default=5000,
        help="D-Bus update interval in milliseconds (default: 5000)"
    )
    parser.add_argument(
        "--reconnect-delay", type=float, default=10.0,
        help="Initial reconnect delay in seconds (default: 10)"
    )
    parser.add_argument(
        "--reconnect-max-delay", type=float, default=120.0,
        help="Maximum reconnect delay in seconds (default: 120)"
    )
    return parser.parse_args()


def main():
    DBusGMainLoop(set_as_default=True)

    args = parse_args()

    service = PowerWatchdogDeviceService(
        mac_address=args.mac,
        adapter=args.adapter,
        update_interval_ms=args.update_interval_ms,
        reconnect_delay=args.reconnect_delay,
        reconnect_max_delay=args.reconnect_max_delay,
    )

    mainloop = GLib.MainLoop()

    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        service.stop()
        mainloop.quit()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info(
        "power-watchdog-device v%s started for %s",
        VERSION, args.mac,
    )

    mainloop.run()


if __name__ == "__main__":
    main()
