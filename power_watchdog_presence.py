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

"""Presence from the BLE advertisement router (dbus-ble-sensors-py).

When ``dbus-ble-sensors-py`` is installed on the GX it already runs a
passive HCI tap on every adapter and offers what it hears to any service
that registers interest on D-Bus.  A Power Watchdog advertises Hughes'
manufacturer ID (0x2312) whenever it is powered and not connected, so the
router can tell us *when the unit is there* without this service ever
starting a scanner of its own -- and starting one is exactly what fights
the router for the adapter (BlueZ allows one active scanner per card).

So there are two presence sources, chosen once at startup:

``router``
    Registration objects under ``/ble_advertisements/power_watchdog/``
    on our own bus name, ``Advertisement`` signals in, no scanner.  The
    session loop waits for the router to hear the unit instead of
    polling the air every five minutes, and new units are discovered
    from the same feed.
``scan``
    The original mechanism: ``BleakScanner`` discovery on a timer and a
    blind resolve+scan every ``OFFLINE_POLL_INTERVAL`` while the unit is
    away.

The choice is made from what is INSTALLED, not from what is on the bus,
because runit starts services in no particular order: at our startup the
router may simply not be up yet.  Whether the router is *running* is
tracked live via ``NameOwnerChanged`` and only decides whether the BLE
loop is gated on presence right now (see
``PowerWatchdogBLE.set_presence_gated``); until the router appears, and
whenever it goes away, the loop behaves exactly as in ``scan`` mode.

Router contract (see dbus-ble-sensors-py ``docs/advertisement-router.md``):

* the EXISTENCE of an introspectable object at a registration path is the
  registration -- there is no method to call;
* the router introspects a service when its bus name appears, so the
  objects must be exported BEFORE the name is claimed;
* the ``Advertisement`` signal (``sqaynss``) is emitted on the
  registration path itself, and ``interface`` names the card that heard
  the unit by MAC (twelve hex digits), falling back to ``hciN`` -- treat
  it as opaque and hand it to ``resolve_adapter`` at the point of use.

This module imports dbus (it exports objects and subscribes to signals)
but never bleak: it is main-thread, D-Bus-side glue.
"""

from __future__ import annotations

import logging
import os
import re

import dbus
import dbus.service

logger = logging.getLogger(__name__)

# The router's surface.  Identical for dbus-ble-sensors-py's in-process
# router and for the standalone dbus-ble-advertisements service it replaced.
ROUTER_BUS_NAME = "com.victronenergy.switch.ble_advertisements"
ROUTER_INTERFACE = "com.victronenergy.switch.ble_advertisements"
ROUTER_ROOT_PATH = "/ble_advertisements"

# Bluetooth SIG company identifier the Power Watchdog advertises (gen2, the
# WD_* units, confirmed against BlueZ's ManufacturerData for one; payload
# is the constant b"EV\x00\x00").  Whether gen1 PM* units carry any
# manufacturer data is unconfirmed -- which is why the BLE loop keeps a
# slow blind retry underneath the presence gate.
HUGHES_MANUFACTURER_ID = 0x2312

# Our label in registration paths.  Must be a substring of the bus name we
# own (com.victronenergy.switch.power_watchdog); the router uses that as a
# cleanup hint.
SERVICE_LABEL = "power_watchdog"

# runit service directories whose presence means an advertisement router
# is installed on this box.  Installed, not running: see the module doc.
ROUTER_PROVIDER_SERVICE_DIRS = (
    "/service/dbus-ble-sensors-py",
    "/service/dbus-ble-advertisements",
)

PRESENCE_SOURCES = ("auto", "router", "scan")

_HCI_DIR = "/sys/class/bluetooth"
_HCI_RE = re.compile(r"^hci\d+$")


# ── Mode decision ───────────────────────────────────────────────────────────

def router_provider_installed(
    service_dirs: tuple[str, ...] = ROUTER_PROVIDER_SERVICE_DIRS,
) -> str | None:
    """The installed router provider's service directory, or None.

    ``/service/<name>`` is a symlink the provider's installer creates and
    its disable script removes, so it is the one artefact that tracks
    "installed and enabled" without depending on the service being up
    at the moment we look.
    """
    for path in service_dirs:
        if os.path.isdir(path):
            return path
    return None


def resolve_presence_source(raw: str | None, installed: str | None) -> str:
    """Map the ``presence_source`` config value to ``router`` or ``scan``.

    ``auto`` (the default) follows what is installed.  An explicit value
    wins either way: ``router`` on a box without a provider will simply
    wait forever for one (and say so), ``scan`` on a box with one keeps the
    old behaviour for a user who wants it.
    """
    value = (raw or "auto").strip().lower()
    if value not in PRESENCE_SOURCES:
        logger.warning(
            "Ignoring unknown presence_source '%s' (expected one of %s), "
            "using auto",
            raw, ", ".join(PRESENCE_SOURCES),
        )
        value = "auto"
    if value == "auto":
        return "router" if installed else "scan"
    return value


# ── Registration paths ─────────────────────────────────────────────────────

def mac_to_addr_segment(mac: str) -> str:
    """``AA:BB:CC:DD:EE:FF`` (any spelling) -> ``AABBCCDDEEFF``."""
    return re.sub(r"[^0-9A-Fa-f]", "", mac).upper()


def mfgr_registration_path(label: str = SERVICE_LABEL,
                           mfg_id: int = HUGHES_MANUFACTURER_ID) -> str:
    """Every advertisement carrying Hughes' company ID.  Decimal, per contract."""
    return "%s/%s/mfgr/%d" % (ROUTER_ROOT_PATH, label, mfg_id)


def addr_registration_path(mac: str, label: str = SERVICE_LABEL) -> str:
    """Every advertisement from one unit, whatever it carries."""
    return "%s/%s/addr/%s" % (ROUTER_ROOT_PATH, label, mac_to_addr_segment(mac))


def registration_paths(known_macs=(), label: str = SERVICE_LABEL) -> list[str]:
    """The manufacturer registration plus one address registration per unit.

    The manufacturer path is what discovers units we have never seen and
    what carries presence for every gen2 unit.  The address paths add
    nothing for those, but cost nothing and would catch a unit whose
    advertisement carries some other (or no known) company ID.
    """
    paths = [mfgr_registration_path(label)]
    seen: set[str] = set()
    for mac in known_macs:
        path = addr_registration_path(mac, label)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


# ── BlueZ name lookup (discovery) ──────────────────────────────────────────

def present_hci_names(hci_dir: str = _HCI_DIR) -> list[str]:
    """``hciN`` names the kernel exposes right now, sorted by index."""
    try:
        names = [n for n in os.listdir(hci_dir) if _HCI_RE.match(n)]
    except OSError:
        return []
    return sorted(names, key=lambda n: int(n[3:]))


def bluez_device_path(hci: str, mac: str) -> str:
    """``hci5`` + ``24:EC:4A:E4:69:A5`` -> ``/org/bluez/hci5/dev_24_EC_4A_E4_69_A5``."""
    segment = mac_to_addr_segment(mac)
    return "/org/bluez/%s/dev_%s" % (
        hci, "_".join(segment[i:i + 2] for i in range(0, 12, 2)),
    )


def bluez_device_name(bus, mac: str, adapter_hint: str | None = None,
                      hci_names: list[str] | None = None) -> str | None:
    """The name BlueZ has cached for ``mac``, or None if it has none yet.

    The router does not forward the local name (its tap decodes names only
    for the device classes that need them), and our classifier works on
    the name -- ``WD_E7_...`` versus ``PMD...`` decides generation and line
    count.  bluetoothd sees the same advertising reports the tap does and
    keeps a ``Device1`` object per adapter for each unit it has heard, so
    the name is one property read away, no radio time involved.

    ``adapter_hint`` is the ``hciN`` the router said heard the unit (already
    translated by the caller); it is tried first, then every other card.
    """
    candidates: list[str] = []
    if adapter_hint:
        candidates.append(adapter_hint)
    for name in (hci_names if hci_names is not None else present_hci_names()):
        if name not in candidates:
            candidates.append(name)

    for hci in candidates:
        path = bluez_device_path(hci, mac)
        try:
            obj = bus.get_object("org.bluez", path, introspect=False)
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            name = props.Get("org.bluez.Device1", "Name")
        except Exception:
            # No such object on this adapter, or no Name yet: both normal.
            continue
        name = str(name).strip()
        if name:
            return name
    return None


# ── Router client ──────────────────────────────────────────────────────────

class _Registration(dbus.service.Object):
    """Empty introspectable object -- its existence is the registration."""


class AdvertisementRouterClient:
    """Register interest with the router and deliver what it emits.

    Parameters
    ----------
    bus
        The shared system bus connection our switch service lives on.
        Registrations are exported on this connection so they appear in
        the introspection of the bus name it owns.
    on_advertisement
        ``callable(mac, mfg_id, data, rssi, interface)`` with plain Python
        types (upper-case colon MAC, int, bytes, int, str).  Runs on the
        GLib main thread; keep it cheap.
    on_router_state
        ``callable(available: bool)`` -- called from :meth:`start` with the
        initial state and on every change afterwards.
    known_macs
        Units to register address paths for, on top of the manufacturer
        registration.  Pass every unit restored from settings: the router
        only introspects us when our name appears, so paths added later
        (see :meth:`add_device`) are not seen until its next restart.

    Construct this BEFORE the bus name is claimed (``VeDbusService.register``)
    -- see the module doc for why -- and call :meth:`start` after.
    """

    def __init__(self, bus, on_advertisement, on_router_state,
                 known_macs=(), label: str = SERVICE_LABEL):
        self._bus = bus
        self._on_advertisement = on_advertisement
        self._on_router_state = on_router_state
        self._label = label
        self._registrations: dict[str, _Registration] = {}
        self._available = False
        self._started = False

        for path in registration_paths(known_macs, label):
            self._register(path)

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to the router's comings and goings and report its state."""
        if self._started:
            return
        self._started = True
        self._bus.add_signal_receiver(
            self._on_name_owner_changed,
            signal_name="NameOwnerChanged",
            dbus_interface="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            arg0=ROUTER_BUS_NAME,
        )
        try:
            available = bool(self._bus.name_has_owner(ROUTER_BUS_NAME))
        except Exception:
            logger.exception("Could not query the bus for %s", ROUTER_BUS_NAME)
            available = False
        self._set_available(available, initial=True)

    @property
    def available(self) -> bool:
        """True while something owns the router's bus name."""
        return self._available

    @property
    def paths(self) -> list[str]:
        return list(self._registrations)

    def add_device(self, mac: str) -> bool:
        """Register an address path for a unit discovered at runtime.

        Returns True when a new path was exported.  Takes effect at the
        router's next introspection of us -- its next restart -- which is
        fine: the manufacturer registration already carries this unit's
        presence, so nothing waits on it.
        """
        path = addr_registration_path(mac, self._label)
        if path in self._registrations:
            return False
        self._register(path)
        return True

    # ── internals ──────────────────────────────────────────────────────

    def _register(self, path: str) -> None:
        self._registrations[path] = _Registration(conn=self._bus, object_path=path)
        self._bus.add_signal_receiver(
            self._on_signal,
            signal_name="Advertisement",
            dbus_interface=ROUTER_INTERFACE,
            path=path,
        )
        logger.info("Advertisement router registration at %s", path)

    def _on_name_owner_changed(self, name, old_owner, new_owner) -> None:
        if str(name) != ROUTER_BUS_NAME:
            return
        self._set_available(bool(new_owner))

    def _set_available(self, available: bool, initial: bool = False) -> None:
        if not initial and available == self._available:
            return
        self._available = available
        if available:
            logger.info(
                "Advertisement router %s is %s; presence comes from it",
                ROUTER_BUS_NAME, "up" if initial else "back",
            )
        else:
            logger.warning(
                "Advertisement router %s is not on the bus%s; polling the "
                "air for the unit until it appears",
                ROUTER_BUS_NAME, "" if initial else " any more",
            )
        try:
            self._on_router_state(available)
        except Exception:
            logger.exception("Router state callback failed")

    def _on_signal(self, mac, mfg_id, data, rssi, interface, name) -> None:
        try:
            self._on_advertisement(
                str(mac).upper(),
                int(mfg_id),
                bytes(bytearray(data)),
                int(rssi),
                str(interface),
            )
        except Exception:
            logger.exception("Advertisement handler failed for %s", mac)
