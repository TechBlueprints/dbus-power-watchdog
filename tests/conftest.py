"""Shared test fixtures and mock infrastructure.

The production code imports Venus OS libraries (dbus, gi, vedbus,
settingsdevice) and BLE libraries (bleak) that are not available in a
normal development/CI environment.  We mock them at the sys.modules
level before any production code is imported.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# ── Mock bleak ──────────────────────────────────────────────────────────────

_bleak = types.ModuleType("bleak")
_bleak.BleakClient = MagicMock()
_bleak.BleakScanner = MagicMock()
_bleak.BleakError = type("BleakError", (Exception,), {})
sys.modules.setdefault("bleak", _bleak)

# ── Mock dbus and gi ────────────────────────────────────────────────────────

_dbus = types.ModuleType("dbus")
_dbus.bus = types.ModuleType("dbus.bus")
_dbus.bus.BusConnection = MagicMock()
_dbus.bus.BusConnection.TYPE_SYSTEM = 0
_dbus.bus.BusConnection.TYPE_SESSION = 1
_dbus.SystemBus = MagicMock
_dbus.SessionBus = MagicMock
_dbus.Int32 = int
_dbus.Interface = MagicMock()
_dbus.service = types.ModuleType("dbus.service")
_dbus.service.BusName = MagicMock()
_dbus.exceptions = types.ModuleType("dbus.exceptions")
_dbus.exceptions.DBusException = type("DBusException", (Exception,), {})
_dbus_mainloop_glib = types.ModuleType("dbus.mainloop.glib")
_dbus_mainloop_glib.DBusGMainLoop = MagicMock()

sys.modules.setdefault("dbus", _dbus)
sys.modules.setdefault("dbus.bus", _dbus.bus)
sys.modules.setdefault("dbus.service", _dbus.service)
sys.modules.setdefault("dbus.exceptions", _dbus.exceptions)
sys.modules.setdefault("dbus.mainloop", types.ModuleType("dbus.mainloop"))
sys.modules.setdefault("dbus.mainloop.glib", _dbus_mainloop_glib)

_gi = types.ModuleType("gi")
_gi.repository = types.ModuleType("gi.repository")
_gi.repository.GLib = MagicMock()
sys.modules.setdefault("gi", _gi)
sys.modules.setdefault("gi.repository", _gi.repository)

# ── Mock velib_python (vedbus, settingsdevice) ──────────────────────────────

sys.modules.setdefault("vedbus", types.ModuleType("vedbus"))
sys.modules["vedbus"].VeDbusService = MagicMock()

sys.modules.setdefault("settingsdevice", types.ModuleType("settingsdevice"))
sys.modules["settingsdevice"].SettingsDevice = MagicMock()
