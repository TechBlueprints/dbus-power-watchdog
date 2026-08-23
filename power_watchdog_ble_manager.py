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

"""Wiring for bleak-connection-manager v2 (the bleak catcher).

The catcher rebinds ``bleak.BleakClient`` process wide, and a module only
picks the wrapper up through its own ``from bleak import BleakClient`` if
the install already happened when it was imported.  ``power_watchdog_ble``
imports bleak at module scope, so both entry points call
:func:`install_ble_connection_manager` *before* importing it — which is why
this module must not import bleak, or ``power_watchdog_ble``, itself.

Everything here is stdlib only.  The parsing helpers are shared with
``power_watchdog_ble``, which needs the same adapter entries to pick the
adapter its scans run on (the catcher routes connections, not our scans —
see ``ble_wrap_scanner``).
"""

from __future__ import annotations

import configparser
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Established-link capacity is deployment config, not discovery: dongle
# limits are undocumented.  An adapter with no cap is never slot-gated.
DEFAULT_LINK_CAPS = ""

# Claim owner recorded in /run/bt-claims for the main service.  The library
# appends this process's pid, so restarts never collide.
CLAIM_OWNER = "dbus-power-watchdog"


def _ensure_ble_stack() -> None:
    """Put the vendored ext/ BLE stack on sys.path unless already provided.

    In production the shared checkout provides it: the run script execs
    through ``/data/bcm/python3``, whose PYTHONPATH already carries bleak,
    bleak-retry-connector and bleak-connection-manager, so one install
    serves every BLE service on the box.  This is the standalone fallback —
    a bare clone, a dev machine, the test suite — and it must never shadow
    a stack the interpreter already has.

    Order matters.  The ``sys.modules`` check comes first because it is
    cheap and because it is what lets the tests stub the package: a
    ModuleType with ``__spec__ = None`` makes ``find_spec`` raise
    ValueError, which is why that is caught alongside ImportError.  A weird
    interpreter state degrades to "insert the ext paths", the safe
    direction — worst case we shadow the shim with the vendored copy,
    never run with no stack at all.
    """
    if "bleak_connection_manager" in sys.modules:
        return
    try:
        import importlib.util

        if importlib.util.find_spec("bleak_connection_manager") is not None:
            return
    except (ImportError, ValueError):
        pass

    ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
    for sub in [
        os.path.join(ext, "bleak-connection-manager", "src"),
        os.path.join(ext, "bleak-connection-manager", "ext"),
        os.path.join(ext, "bleak-retry-connector", "src"),
        os.path.join(ext, "bluetooth-adapters", "src"),
        os.path.join(ext, "aiooui", "src"),
        os.path.join(ext, "bleak"),
    ]:
        if os.path.isdir(sub) and sub not in sys.path:
            sys.path.insert(0, sub)


def parse_bool(raw: str | None, default: bool = False) -> bool:
    """Parse an ini boolean, falling back to ``default`` when unset/odd."""
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    if value == "":
        return default
    logger.warning("Ignoring unparseable boolean '%s', using %s", raw, default)
    return default


def parse_adapters(raw: str | None) -> list[str]:
    """Split a comma-separated adapter list into verbatim entries.

    Entries are handed to the library unchanged: ``hciX`` joins the shared
    pool, ``MAC@hciX`` pins that device to that adapter (repeat the MAC for
    an ordered preference list).
    """
    if not raw:
        return []
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def parse_link_caps(raw: str | None) -> dict[str, int]:
    """Split ``hciX:N`` entries into ``{adapter: capacity}``.

    Malformed entries are logged and skipped rather than guessed at: a wrong
    cap silently gates connections.
    """
    caps: dict[str, int] = {}
    for entry in parse_adapters(raw):
        adapter, sep, cap = entry.partition(":")
        adapter = adapter.strip()
        try:
            cap_value = int(cap.strip()) if sep else 0
        except ValueError:
            cap_value = 0
        if not adapter or cap_value <= 0:
            logger.warning("Ignoring malformed ble_link_caps entry '%s'", entry)
            continue
        caps[adapter] = cap_value
    return caps


def split_adapters(entries: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Split adapter entries into ``(pins, pool)``.

    Mirrors the library's own parse of the same strings so our scans land on
    the adapter a device's connections are pinned to.  ``pins`` is keyed by
    upper-case MAC; ``pool`` holds the plain ``hciX`` entries.
    """
    pins: dict[str, list[str]] = {}
    pool: list[str] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "@" in entry:
            mac, _, adapter = entry.rpartition("@")
            mac = mac.strip().upper()
            adapter = adapter.strip()
            if not mac or not adapter:
                logger.warning("Ignoring malformed adapter entry '%s'", entry)
                continue
            adapters = pins.setdefault(mac, [])
            if adapter not in adapters:
                adapters.append(adapter)
        else:
            pool.append(entry)
    return pins, pool


def scan_adapter_for(address: str | None, entries: list[str]) -> str | None:
    """The adapter our own scans should use, or None for bleak's default.

    Connections are routed by the catcher, but with ``ble_wrap_scanner``
    off our scans are plain bleak.  Scanning on the adapter the connection
    will use matters on a GX with a USB dongle: a device seen only by hci1
    resolves to an hci1 D-Bus path, and that path is what bleak connects
    over.  A pinned device gets its first pinned adapter; anything else
    gets the first pool entry.
    """
    pins, pool = split_adapters(entries)
    if address:
        pinned = pins.get(str(address).strip().upper())
        if pinned:
            return pinned[0]
    return pool[0] if pool else None


def load_ble_settings(config_dir: str | None = None) -> dict[str, str]:
    """Read the ``[DEFAULT]`` section of config.ini (or config.default.ini).

    Deliberately independent of ``dbus-power-watchdog.py``'s ``load_config``:
    the install has to happen before that module is importable at all, and
    ``power_watchdog_device.py`` has no config loading of its own.
    """
    if config_dir is None:
        config_dir = os.path.dirname(os.path.abspath(__file__))

    config = configparser.ConfigParser()
    for name in ("config.ini", "config.default.ini"):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            config.read(path)
            break

    return dict(config["DEFAULT"]) if "DEFAULT" in config else {}


def install_ble_connection_manager(
    owner: str = CLAIM_OWNER,
    settings: dict[str, str] | None = None,
) -> bool:
    """Install the bleak catcher for this process unless config disables it.

    Returns True when the catcher was installed.  ``bluetooth_adapters`` is
    handed over verbatim — the library parses the same ``MAC@hciX`` and
    plain ``hciX`` forms this repo documents — so one config key drives both
    the catcher's routing and our own scan adapter choice.

    A failed install is logged and swallowed: the catcher is coordination,
    and connecting uncoordinated beats not connecting at all.
    """
    # Before the enabled check: the BLE stack has to be importable even when
    # the catcher is switched off, because power_watchdog_ble imports bleak
    # either way.  Under the shim this is a no-op.
    _ensure_ble_stack()

    if settings is None:
        settings = load_ble_settings()

    if not parse_bool(settings.get("ble_connection_manager"), default=True):
        logger.info("BLE connection manager disabled by config")
        return False

    adapters = parse_adapters(settings.get("bluetooth_adapters"))
    link_caps = parse_link_caps(settings.get("ble_link_caps", DEFAULT_LINK_CAPS))
    wrap_scanner = parse_bool(settings.get("ble_wrap_scanner"), default=False)

    try:
        from bleak_connection_manager import install_bleak_catcher

        install_bleak_catcher(
            owner,
            adapters=adapters,
            link_caps=link_caps,
            wrap_scanner=wrap_scanner,
        )
    except Exception:
        logger.exception(
            "Failed to install the BLE connection manager, "
            "continuing without it",
        )
        return False

    logger.info(
        "BLE connection manager installed (adapters=%s, link_caps=%s, "
        "wrap_scanner=%s)",
        ", ".join(adapters) if adapters else "all present",
        ", ".join("%s:%d" % kv for kv in sorted(link_caps.items()))
        if link_caps else "uncapped",
        wrap_scanner,
    )
    return True
