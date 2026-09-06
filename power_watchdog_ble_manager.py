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

This repo vendors no part of the BLE stack.  bleak, bleak-retry-connector
and the catcher come from the shared ``/data/bcm`` checkout that
``install.sh`` converges, and :func:`install_ble_connection_manager` puts
that checkout on ``sys.path`` itself, through ``ble_stack.py`` — a verbatim
copy of the fleet's reference implementation of the consumer contract
(dbus-serialbattery's ``ble_stack.py``; see the library's
CONSUMER_MIGRATION.md).  Nothing about how the process was launched decides
which stack it runs on: no interpreter shim, no PYTHONPATH, no environment
contract.  The connection manager is imported before bleak, which is what
makes the box-wide autowire hook stand down for this process instead of
installing a generic catcher with a cmdline-derived owner.

A private pin is exactly how one service drifts onto a different version
of the claims convention than the rest of the fleet — and it also meant the
tests ran a different bleak major than production did.
"""

from __future__ import annotations

import configparser
import logging
import os
import re

import ble_stack

logger = logging.getLogger(__name__)

# Where the shared install lives unless config says otherwise.
DEFAULT_SHARED_DIR = ble_stack.DEFAULT_SHARED_DIR

# Established-link capacity is deployment config, not discovery: dongle
# limits are undocumented.  An adapter with no cap is never slot-gated.
DEFAULT_LINK_CAPS = ""

# Claim owner recorded in /run/bt-claims for the main service.  The library
# appends this process's pid, so restarts never collide.
CLAIM_OWNER = "dbus-power-watchdog"

# The only adapter spelling plain bleak understands.
_HCI_NAME = re.compile(r"^hci\d+$")


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


def resolve_adapter(entry: str | None) -> str | None:
    """Translate an adapter entry to the ``hciN`` it answers to right now.

    Adapters should be named by MAC, never by ``hciN``: the index is not an
    identity — a USB replug or a reboot renumbers the cards, and a stale
    number silently points at a different radio.  This service has already
    been bitten by that (a config pinned to hci1 while the unit was heard
    on another card, 2026-08-25).

    But bleak's BlueZ backend only accepts ``adapter="hciN"``, so the
    translation has to happen as late as possible, right at the call.  The
    library's ``claims.hci_for()`` resolves against the live numbering for
    exactly this reason, and is deliberately not cached.

    Returns None — meaning "let bleak choose" — when a configured card is
    not present, which beats handing bleak a MAC it cannot parse or an
    hciN that now belongs to someone else's radio.
    """
    if not entry:
        return None
    try:
        from bleak_connection_manager import claims
    except Exception:
        # No shared stack (a standalone run): touch nothing of the
        # library's, and hand plain bleak only what it understands.  A MAC
        # is the library's adapter syntax; bleak's adapter= wants hciN.
        if _HCI_NAME.match(entry):
            return entry
        logger.warning(
            "Adapter '%s' needs the shared BLE stack to resolve; "
            "letting bleak choose",
            entry,
        )
        return None

    try:
        resolved = claims.hci_for(entry)
    except Exception:
        logger.exception("Adapter lookup failed for '%s'", entry)
        return None

    if resolved is None:
        logger.warning(
            "Configured adapter '%s' is not present; letting bleak choose",
            entry,
        )
        return None
    if resolved != entry:
        logger.info("Adapter %s is currently %s", entry, resolved)
    return resolved


def scan_adapter_for(address: str | None, entries: list[str]) -> str | None:
    """The adapter our own scans should use, or None for bleak's default.

    Connections are routed by the catcher, but with ``ble_wrap_scanner``
    off our scans are plain bleak.  Scanning on the adapter the connection
    will use matters on a GX with a USB dongle: a device seen only by hci1
    resolves to an hci1 D-Bus path, and that path is what bleak connects
    over.  A pinned device gets its first pinned adapter; anything else
    gets the first pool entry.

    Entries name adapters by MAC (``hciN`` still parses, for compatibility);
    the result is translated to the current ``hciN`` by
    :func:`resolve_adapter`, because that is the only form bleak accepts.
    """
    pins, pool = split_adapters(entries)
    chosen = None
    if address:
        pinned = pins.get(str(address).strip().upper())
        if pinned:
            chosen = pinned[0]
    if chosen is None and pool:
        chosen = pool[0]
    return resolve_adapter(chosen)


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


_UNCOORDINATED = (
    "running uncoordinated, no claims, no adapter routing, no card recovery"
)


def _log_stack_state(mode: str, shared_dir: str) -> None:
    """One line per outcome, in the contract's words (anchor
    ``BLE coordination: ``).

    The fleet monitor greps for these strings, so the wording is not ours
    to improve.  The INFO line names the imported package's own directory,
    not the configured key, so it proves which tree actually served.  "No
    shared install" and "shared install unusable" are different operator
    actions and must never log alike.  Called only when coordination is
    wanted: a manager deliberately off logs no line.
    """
    if not shared_dir:
        logger.warning(
            "BLE coordination: ble_connection_manager is on but "
            "ble_connection_manager_dir is empty; %s",
            _UNCOORDINATED,
        )
    elif mode == "shared":
        import bleak_connection_manager as _bcm

        logger.info(
            "BLE coordination: bleak_connection_manager loaded from %s",
            os.path.dirname(getattr(_bcm, "__file__", None) or shared_dir),
        )
    elif mode == "vendored":
        if ble_stack.shared_failure:
            logger.error(
                "BLE coordination: shared install at %s is present but "
                "unusable, running uncoordinated: %s",
                shared_dir, ble_stack.shared_failure,
            )
        else:
            logger.warning(
                "BLE coordination: no shared install at %s; %s",
                shared_dir, _UNCOORDINATED,
            )


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
    and connecting uncoordinated beats not connecting at all.  A shared
    install that is *present but broken* is an ERROR (an operator action);
    one that is simply absent is a WARNING (the normal state of any box
    without it).
    """
    if settings is None:
        settings = load_ble_settings()

    shared_dir = settings.get("ble_connection_manager_dir")
    if shared_dir is None:
        shared_dir = DEFAULT_SHARED_DIR
    shared_dir = shared_dir.strip()
    # Unconditionally, and before the enable check: importability is not a
    # feature flag.  power_watchdog_ble does `from bleak import ...` at
    # module scope and this repo vendors no bleak, so the shared install is
    # the only place it can come from whether or not the catcher is wanted.
    # No vendored fallback: absent means whatever bleak the interpreter has.
    # An EMPTY key means never look -- a deliberate standalone run.
    if shared_dir:
        mode = ble_stack.ensure_ble_stack(shared_dir, vendored_dir=None)
    else:
        mode = "provided"  # whatever the interpreter has; never looked

    if not parse_bool(settings.get("ble_connection_manager"), default=True):
        # Deliberately off: the stack was still made importable above, but
        # the contract's coordination lines are for a manager that is on.
        logger.info("BLE connection manager disabled by config")
        return False
    _log_stack_state(mode, shared_dir)
    if mode == "vendored":
        # Contract rule 7: on the standalone path touch nothing of the
        # library's.  The state was logged above; nothing more to say.
        return False

    adapters = parse_adapters(settings.get("bluetooth_adapters"))
    link_caps = parse_link_caps(settings.get("ble_link_caps", DEFAULT_LINK_CAPS))
    wrap_scanner = parse_bool(settings.get("ble_wrap_scanner"), default=False)
    # Fleet policy: BlueZ StartNotify, never AcquireNotify (the BlueZ 5.72
    # notify_io double-free).  A consumer-side key on the same footing as
    # the location, now that no launcher environment decides it for us.
    force_start_notify = parse_bool(
        settings.get("ble_force_start_notify"), default=True,
    )

    try:
        from bleak_connection_manager import install_bleak_catcher
    except ImportError:
        logger.warning(
            "BLE connection manager is not importable; running uncoordinated",
        )
        return False

    try:
        install_bleak_catcher(
            owner,
            adapters=adapters,
            link_caps=link_caps,
            wrap_scanner=wrap_scanner,
            force_start_notify=force_start_notify,
        )
    except Exception:
        logger.exception(
            "Failed to install the BLE connection manager, "
            "continuing without it",
        )
        return False

    logger.info(
        "BLE connection manager installed (adapters=%s, link_caps=%s, "
        "wrap_scanner=%s, force_start_notify=%s)",
        ", ".join(adapters) if adapters else "all present",
        ", ".join("%s:%d" % kv for kv in sorted(link_caps.items()))
        if link_caps else "uncapped",
        wrap_scanner,
        force_start_notify,
    )
    return True
