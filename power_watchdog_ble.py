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
BLE client for the Hughes Power Watchdog surge protector.

BLE protocol based on prior open-source work by spbrogan and tango2590.

Hughes shipped two generations with completely different BLE protocols:

- **Gen2** (WD_* names, WiFi+BT): custom framed binary protocol over a
  single characteristic (``0000ff01``).  Requires an ASCII handshake to
  start data flow.  See :mod:`power_watchdog_proto_gen2`.

- **Gen1** (PM* names, BT-only): raw Modbus-style 20-byte notification
  pairs over Nordic UART characteristics (``0000ffe2`` / ``0000fff5``).
  Telemetry starts immediately on subscribe.
  See :mod:`power_watchdog_proto_gen1`.

This module contains shared infrastructure: data models, BLE device
discovery, GATT resolution, and the ``PowerWatchdogBLE`` connection
lifecycle.  Protocol-specific parsing and handshake logic lives in the
``power_watchdog_proto_*`` modules.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field

from bleak import BleakClient, BleakError, BleakScanner
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
    get_device,
    get_device_by_adapter,
)

from power_watchdog_ble_manager import scan_adapter_for

try:
    # Stdlib-only in the library (it never imports bleak), but this module
    # has to keep working with the connection manager absent or disabled.
    from bleak_connection_manager import tolerate_late_gatt
except Exception:  # pragma: no cover - the submodule is always present
    tolerate_late_gatt = None

logger = logging.getLogger(__name__)


# ── Discovery name patterns ─────────────────────────────────────────────────

# Gen2 (WiFi+BT) devices advertise as "WD_{type}_{serialhex}"
# Types: E5, E6, E7, E8, E9, V5, V6, V7, V8, V9
GEN2_PREFIX = "WD_"

# Gen1 (BT-only) devices advertise as "PM{S|D}..." (19 or 27 chars)
# S = single/30A, D = double/50A
GEN1_PREFIX = "PM"

# ── GATT UUID constants ────────────────────────────────────────────────────

# Gen2: single characteristic for notify + write
CHARACTERISTIC_UUID_GEN2 = "0000ff01-0000-1000-8000-00805f9b34fb"

# Gen1 (BT-only): Nordic UART-style TX/RX under 0000ffe0
CHARACTERISTIC_UUID_GEN1_TX = "0000ffe2-0000-1000-8000-00805f9b34fb"
CHARACTERISTIC_UUID_GEN1_RX = "0000fff5-0000-1000-8000-00805f9b34fb"

# Backwards-compatible alias — gen2 data path
CHARACTERISTIC_UUID = CHARACTERISTIC_UUID_GEN2

# Notification watchdog: force reconnect if no BLE notifications arrive
# within this window.  Measured on prod 2026-09-09 (btmon on the pinned card,
# 200 s): the Power Watchdog sends one framed packet per second, as two ATT
# notifications ~200 ms apart, with under 100 ms of jitter -- not the "~30s"
# this comment used to claim from reading the source.  Two minutes of
# silence is therefore ~120 missed frames: very loose, and certainly dead.
NOTIFICATION_WATCHDOG_TIMEOUT = 120.0  # seconds

# How often the notification watchdog checks for silence.  Well under the
# timeout so a dead link is caught promptly, cheap enough to ignore.
WATCHDOG_CHECK_INTERVAL = 5.0  # seconds

# How long to scan when the BlueZ cache has no record of the device.
SCAN_TIMEOUT = 20.0  # seconds

# Minimum gap between repeats of the same BLE error kind.  A retry loop
# against a busy or wedged radio can fail every few seconds for hours; the
# first line carries the diagnosis and the rest only bury it.
BLE_ERROR_LOG_INTERVAL = 300.0  # seconds


class NotificationWatchdog:
    """Fire a callback when BLE notifications go quiet.

    The connection manager routes and coordinates connections; noticing
    that an established one has gone silent is the consumer's job, so this
    lives here.  Deliberately narrow: it watches a timestamp and calls
    back.  It does no BlueZ cleanup of its own — the session loop owns
    teardown, and a watchdog that removed the device behind bleak's back is
    what left ``client.is_connected`` stale under the v1 manager.

    ``last_activity`` is public: the session loop reads it directly as its
    own backstop, so a watchdog task that dies cannot wedge the session
    (the 2026-08-09 four-hour wedge).
    """

    def __init__(self, timeout: float, on_timeout, name: str = ""):
        self.timeout = timeout
        self.last_activity = time.monotonic()
        self._on_timeout = on_timeout
        self._name = name
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def record_activity(self) -> None:
        """Mark a notification as having just arrived."""
        self.last_activity = time.monotonic()

    def start(self) -> None:
        if self._task is not None:
            return
        self.last_activity = time.monotonic()
        self._loop = asyncio.get_event_loop()
        self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        """Cancel the watcher.  Safe to call from any thread.

        PowerWatchdogBLE.stop() runs on the main thread while this task
        lives in the BLE thread's loop, and Task.cancel() is not thread
        safe — a cross-thread stop has to go through the owning loop.
        """
        task, self._task = self._task, None
        loop, self._loop = self._loop, None
        if task is None or task.done():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop or loop is None:
            task.cancel()
        elif not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)
                if (time.monotonic() - self.last_activity) <= self.timeout:
                    continue
                try:
                    await self._on_timeout()
                except Exception:
                    # The session loop's staleness check still covers us —
                    # losing the fast path is not worth losing the session.
                    logger.exception(
                        "Notification watchdog callback failed for %s",
                        self._name,
                    )
                return
        except asyncio.CancelledError:
            pass


def format_gatt_snapshot(client: BleakClient) -> str:
    """Human-readable GATT tree for support logs (multi-line string)."""
    lines: list[str] = []
    for svc in client.services:
        suuid = getattr(svc, "uuid", "(unknown service)")
        lines.append("  service %s" % suuid)
        for char in svc.characteristics:
            lines.append(
                "    char %s [%s]"
                % (char.uuid, ",".join(str(p) for p in char.properties)),
            )
    return "\n".join(lines) if lines else "  (no services)"


def resolve_power_watchdog_gatt(client: BleakClient) -> tuple[str, str, bool, str]:
    """Map GATT services to notify UUID, write UUID, and write mode.

    Returns:
        Tuple of ``(notify_uuid, write_uuid, write_with_response, mode)`` where
        ``mode`` is ``\"gen2\"`` or ``\"gen1_uart\"``.

    Raises:
        BleakError: If neither known layout is present.
    """
    char_props: dict[str, list] = {}
    for svc in client.services:
        for char in svc.characteristics:
            char_props[char.uuid.lower()] = list(char.properties)

    u_g2 = CHARACTERISTIC_UUID_GEN2.lower()
    if u_g2 in char_props:
        p = char_props[u_g2]
        if "notify" in p:
            use_resp = "write" in p
            reason = (
                "gen2 ff01 notify+write"
                if use_resp
                else "gen2 ff01 notify (no 'write' prop, using write-without-response)"
            )
            logger.info("GATT pick: %s", reason)
            return (
                CHARACTERISTIC_UUID_GEN2,
                CHARACTERISTIC_UUID_GEN2,
                use_resp,
                "gen2",
            )
        logger.warning(
            "GATT: characteristic %s is present but has no 'notify' property "
            "(props=%s); cannot use gen2 path",
            CHARACTERISTIC_UUID_GEN2,
            ",".join(str(x) for x in p),
        )

    u_tx = CHARACTERISTIC_UUID_GEN1_TX.lower()
    u_rx = CHARACTERISTIC_UUID_GEN1_RX.lower()
    if u_tx in char_props and u_rx in char_props:
        pt, prx = char_props[u_tx], char_props[u_rx]
        if "notify" not in pt:
            logger.warning(
                "GATT: gen1 TX %s missing 'notify' (props=%s)",
                CHARACTERISTIC_UUID_GEN1_TX,
                ",".join(str(x) for x in pt),
            )
        elif "write-without-response" in prx:
            logger.info(
                "GATT pick: gen1 UART TX notify + RX write-without-response",
            )
            return (
                CHARACTERISTIC_UUID_GEN1_TX,
                CHARACTERISTIC_UUID_GEN1_RX,
                False,
                "gen1_uart",
            )
        elif "write" in prx:
            logger.info("GATT pick: gen1 UART TX notify + RX write (with response)")
            return (
                CHARACTERISTIC_UUID_GEN1_TX,
                CHARACTERISTIC_UUID_GEN1_RX,
                True,
                "gen1_uart",
            )
        else:
            logger.warning(
                "GATT: gen1 RX %s has no write props (props=%s)",
                CHARACTERISTIC_UUID_GEN1_RX,
                ",".join(str(x) for x in prx),
            )
    elif u_tx in char_props or u_rx in char_props:
        logger.warning(
            "GATT: partial gen1 UART (TX present=%s RX present=%s)",
            u_tx in char_props,
            u_rx in char_props,
        )

    found: list[str] = []
    for svc in client.services:
        for char in svc.characteristics:
            found.append(
                "%s[%s]" % (char.uuid, ",".join(char.properties)),
            )
    detail = "; ".join(found)
    logger.error(
        "GATT resolution failed; full table:\n%s",
        format_gatt_snapshot(client),
    )
    raise BleakError(
        "Power Watchdog GATT not recognized: need %s (gen2) or %s+%s (gen1). "
        "Found: %s"
        % (
            CHARACTERISTIC_UUID_GEN2,
            CHARACTERISTIC_UUID_GEN1_TX,
            CHARACTERISTIC_UUID_GEN1_RX,
            detail,
        ),
    )


async def validate_power_watchdog_gatt(client: BleakClient) -> bool:
    """Post-connect validator: is this link actually a Power Watchdog?

    A connect that returns success is not always a usable link — GATT can
    come back empty, or resolved with only the Generic Attribute service.
    Rejecting here is a connection failure to the connection manager, so
    bleak-retry-connector attempts again on the next radio, instead of the
    session tearing all the way down and waiting out the backoff.
    """
    try:
        resolve_power_watchdog_gatt(client)
        return True
    except BleakError:
        return False


# The waits were implicit around every v1 validator; v2 makes them explicit
# because the catcher itself never retries.  Keeping them preserves what
# this service did before: give a device that registers its vendor services
# late a chance before writing the link off.
VALIDATE_CONNECTION = (
    tolerate_late_gatt(validate_power_watchdog_gatt)
    if tolerate_late_gatt is not None
    else validate_power_watchdog_gatt
)


# ── Data model ──────────────────────────────────────────────────────────────

@dataclass
class LineData:
    """Parsed power data for a single AC line."""
    voltage: float = 0.0        # Volts (input)
    current: float = 0.0        # Amps
    power: float = 0.0          # Watts
    energy: float = 0.0         # kWh (cumulative)
    output_voltage: float = 0.0 # Volts (output, after regulation)
    frequency: float = 0.0      # Hz
    error_code: int = 0         # 0-9
    status: int = 0
    boost: bool = False


@dataclass
class WatchdogData:
    """Parsed Power Watchdog data with L1/L2 support."""
    l1: LineData = field(default_factory=LineData)
    l2: LineData = field(default_factory=LineData)
    has_l2: bool = False
    timestamp: float = 0.0
    raw_hex: str = ""          # last raw notification for debugging


# ── Discovery ────────────────────────────────────────────────────────────────

@dataclass
class DiscoveredDevice:
    """A Power Watchdog device found during BLE scanning."""
    mac: str               # MAC address (e.g., "AA:BB:CC:DD:EE:FF")
    name: str              # BLE advertised name (e.g., "WD_E7_aabbccddeeff")
    generation: int = 0    # 1 = gen1 (BT-only), 2 = gen2 (WiFi+BT)
    device_type: str = ""  # e.g., "E7" for gen2, "PMD" for gen1 50A
    line_type: str = ""    # "single" (30A) or "double" (50A)
    hw_version: int = 0    # Gen1 only: 1/2/3 from name[15:17] E2/E3/E4
    has_booster: bool = False  # Gen2 only: E8/V8, E9/V9 have voltage booster


def _gen2_has_booster(device_type: str) -> bool:
    """True for Gen2 models with a voltage booster (E8/V8, E9/V9)."""
    return len(device_type) == 2 and device_type[1] in ("8", "9")


def classify_device(name: str) -> DiscoveredDevice | None:
    """Classify a BLE device name as a Power Watchdog, or return None.

    Gen2 (WiFi+BT): Name starts with "WD_", format "WD_{type}_{serialhex}".
    Gen1 (BT-only): Name starts with "PM", 19 or 27 chars, "PMS"=30A, "PMD"=50A.
    """
    if not name:
        return None

    # Gen2: WD_{type}_{serialhex}
    if name.startswith(GEN2_PREFIX):
        parts = name.split("_")
        if len(parts) == 3:
            device_type = parts[1]
            # E-types and V-types: 5/6=30A, 7/8/9=50A (based on product line)
            line_type = "unknown"
            if device_type and len(device_type) == 2:
                model_num = device_type[1]
                if model_num in ("5", "6"):
                    line_type = "single"
                elif model_num in ("7", "8", "9"):
                    line_type = "double"
            return DiscoveredDevice(
                mac="",  # filled in by caller
                name=name,
                generation=2,
                device_type=device_type,
                line_type=line_type,
                has_booster=_gen2_has_booster(device_type),
            )

    # Gen1: PM{S|D}... (19 chars, or 27 with trailing spaces)
    if name.startswith(GEN1_PREFIX):
        effective_name = name.rstrip()
        if len(effective_name) == 19:
            third_char = effective_name[2] if len(effective_name) > 2 else ""
            if third_char == "S":
                line_type = "single"
            elif third_char == "D":
                line_type = "double"
            else:
                line_type = "unknown"
            # Hardware version from name[15:17]: "E2"→1, "E3"→2, "E4"→3
            _GEN1_VERSIONS = {"E2": 1, "E3": 2, "E4": 3}
            hw_version = _GEN1_VERSIONS.get(effective_name[15:17], 0)
            return DiscoveredDevice(
                mac="",  # filled in by caller
                name=name,
                generation=1,
                device_type=effective_name[:3],  # e.g., "PMD", "PMS"
                line_type=line_type,
                hw_version=hw_version,
            )

    return None


async def scan_for_devices(
    timeout: float = 15.0,
    ble_adapters: list[str] | None = None,
) -> list[DiscoveredDevice]:
    """Scan for Power Watchdog BLE devices.

    Discovery uses plain ``BleakScanner``.  With ``ble_wrap_scanner`` on,
    the connection manager has rebound it to its adapter-bound, claiming
    scanner and this call is coordinated with other BLE services on the
    device; with it off (the default) this is an ordinary scan, which is
    why the adapter is chosen here rather than left to bleak.

    Args:
        timeout: Scan timeout in seconds.
        ble_adapters: Configured adapter entries.  Only the pool entries
            matter for a discovery scan — it is not looking for one MAC.

    Returns:
        List of all unique DiscoveredDevice instances found.
    """
    adapter = scan_adapter_for(None, ble_adapters or [])
    kwargs = {"adapter": adapter} if adapter else {}

    try:
        devices = await BleakScanner.discover(timeout=timeout, **kwargs)
    except BleakError:
        logger.exception(
            "BLE discovery scan failed%s",
            " on %s" % adapter if adapter else "",
        )
        return []

    found: list[DiscoveredDevice] = []
    seen_macs: set[str] = set()

    for device in devices:
        name = device.name or ""
        classified = classify_device(name)
        if classified is not None and device.address not in seen_macs:
            classified.mac = device.address
            seen_macs.add(device.address)
            found.append(classified)
            logger.info(
                "Discovered Power Watchdog: %s (%s) gen%d %s %s",
                name, device.address, classified.generation,
                classified.device_type, classified.line_type,
            )

    if found:
        logger.info(
            "Discovery complete: found %d Power Watchdog device(s)", len(found)
        )
    else:
        logger.info("Discovery complete: no Power Watchdog devices found")

    return found


# ── BLE client ──────────────────────────────────────────────────────────────

class PowerWatchdogBLE:
    """BLE client that runs in a daemon thread and exposes data to the main thread.

    BLE operations, in order:
    - resolve the device from the BlueZ cache, falling back to a scan
    - establish_connection (bleak-retry-connector) drives the retries; the
      client class it is handed is the connection manager's routed wrapper
      whenever the catcher was installed, so adapter selection, link slots
      and claim coordination happen underneath the connect
    - NotificationWatchdog for detecting a silent link

    Protocol-specific notification handling and handshake logic is delegated
    to :class:`~power_watchdog_proto_gen2.Gen2Protocol` or
    :class:`~power_watchdog_proto_gen1.Gen1Protocol` based on GATT resolution.
    """

    # When the device is completely offline (not found during scan),
    # retry at this fixed interval rather than using exponential backoff.
    # The device may be unplugged and will come back at any time.
    OFFLINE_POLL_INTERVAL = 300.0  # 5 minutes

    # Multiple of NOTIFICATION_WATCHDOG_TIMEOUT after which the session
    # loop tears down on its own, without waiting for the watchdog.
    STALE_DATA_FACTOR = 1.5

    def __init__(
        self,
        address: str,
        reconnect_delay: float = 10.0,
        reconnect_max_delay: float = 120.0,
        ble_adapters: list[str] | None = None,
    ):
        self.address = address
        self.reconnect_delay = reconnect_delay
        self.reconnect_max_delay = reconnect_max_delay
        # Configured adapter entries, verbatim ("hciX" pool, "MAC@hciX" pin).
        # The connection manager routes connects with these; we use them to
        # pick the adapter our own scan runs on. None/empty = bleak default.
        self._ble_adapters = ble_adapters

        self._data = WatchdogData()
        self._data_lock = threading.Lock()
        self._connected = False
        self._running = True

        # Set by the notification watchdog to tear the session down. Kept
        # separate from client.is_connected on purpose: a link BlueZ still
        # believes in can be silent, and that is exactly the case the
        # watchdog exists to catch.
        self._reconnect_requested = False

        # Consecutive "resolved, gone by connect" failures. The device was
        # advertising seconds ago, so the first one is treated as a blip and
        # only a repeat concedes it is offline.
        self._notfound_streak = 0

        # Rate-limit state for expected BLE errors: {(type, dbus name):
        # (last log monotonic, suppressed count)}.  See _log_ble_error.
        self._ble_error_log: dict[tuple[str, str], tuple[float, int]] = {}

        # Skip the BlueZ cache on the next resolve. BlueZ remembers devices
        # that have gone away, and connecting to a stale entry fails the
        # same way every time — without this, a stale entry would keep us
        # off the air forever, never scanning to find out otherwise.
        self._force_scan = False

        # Advances once per session-loop iteration. Read by the daemon as a
        # liveness signal — see the connect_cycles property.
        self._connect_cycles = 0

        # asyncio event loop reference (set by daemon thread)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sleep_task: asyncio.Task | None = None

        # Notification watchdog (set when connected)
        self._watchdog: NotificationWatchdog | None = None

        # Start BLE daemon thread
        self._thread = threading.Thread(
            name="PowerWatchdog_BLE",
            target=self._run_loop,
            daemon=True,
        )
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def connect_cycles(self) -> int:
        """Number of session-loop iterations so far.

        Advances once per connect attempt, including the slow poll while
        the unit is offline. A frozen counter combined with stale telemetry
        is how the daemon tells a wedged BLE thread from a device that is
        simply unplugged: an unplugged unit still advances this every
        ``OFFLINE_POLL_INTERVAL``, and a healthy connected one sits in a
        single long session with a frozen counter but flowing data.
        """
        return self._connect_cycles

    def get_data(self) -> WatchdogData:
        """Return a snapshot of the latest data (thread-safe)."""
        with self._data_lock:
            return WatchdogData(
                l1=LineData(
                    voltage=self._data.l1.voltage,
                    current=self._data.l1.current,
                    power=self._data.l1.power,
                    energy=self._data.l1.energy,
                    output_voltage=self._data.l1.output_voltage,
                    frequency=self._data.l1.frequency,
                    error_code=self._data.l1.error_code,
                    status=self._data.l1.status,
                    boost=self._data.l1.boost,
                ),
                l2=LineData(
                    voltage=self._data.l2.voltage,
                    current=self._data.l2.current,
                    power=self._data.l2.power,
                    energy=self._data.l2.energy,
                    output_voltage=self._data.l2.output_voltage,
                    frequency=self._data.l2.frequency,
                    error_code=self._data.l2.error_code,
                    status=self._data.l2.status,
                    boost=self._data.l2.boost,
                ),
                has_l2=self._data.has_l2,
                timestamp=self._data.timestamp,
                raw_hex=self._data.raw_hex,
            )

    def stop(self, timeout: float = 5.0):
        """Signal the BLE thread to stop and wait for a clean BLE disconnect.

        Args:
            timeout: Maximum seconds to wait for the BLE thread to finish
                     its clean disconnect sequence.
        """
        if not self._running:
            return
        self._running = False

        # Stop the watchdog if active
        if self._watchdog is not None:
            self._watchdog.stop()

        # If there's a running asyncio loop, cancel the sleep so the
        # disconnect happens immediately rather than waiting up to 1s.
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._cancel_sleep)

        # Wait for the daemon thread to finish its disconnect.
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "BLE thread did not stop within %.1fs, "
                "connection may not be cleanly closed", timeout
            )

    # ── Daemon thread ───────────────────────────────────────────────────────

    def _cancel_sleep(self):
        """Cancel the current sleep task so the thread can exit promptly."""
        if self._sleep_task is not None and not self._sleep_task.done():
            self._sleep_task.cancel()

    def _run_loop(self):
        """Daemon thread entry point: run asyncio event loop."""
        while self._running:
            try:
                loop = asyncio.new_event_loop()
                self._loop = loop
                loop.run_until_complete(self._async_main())
                loop.close()
            except Exception:
                logger.exception("BLE daemon loop crashed, restarting...")
                time.sleep(self.reconnect_delay)
            finally:
                self._loop = None

    async def _async_main(self):
        """Connect, subscribe, and stay connected.

        1. resolve a BLEDevice — BlueZ cache first, then a scan
        2. establish_connection — bleak-retry-connector drives the retries,
           the connection manager routes each attempt underneath it
        3. NotificationWatchdog — detect a link that has gone silent

        Protocol-specific notification handling and handshake logic is
        delegated to a protocol object selected after GATT resolution.
        """
        from power_watchdog_proto_gen1 import Gen1Protocol
        from power_watchdog_proto_gen2 import Gen2Protocol

        delay = self.reconnect_delay
        if self._ble_adapters:
            logger.info(
                "BLE adapter config active for %s: %s",
                self.address,
                ", ".join(self._ble_adapters),
            )

        while self._running:
            client: BleakClient | None = None
            device: BLEDevice | None = None
            # Set by handlers that own their own retry cadence; None means
            # fall through to the generic disconnect backoff.
            next_delay: float | None = None
            self._reconnect_requested = False
            self._connect_cycles += 1

            try:
                # Step 1: Resolve a BLEDevice — BlueZ cache, then a scan
                device = await self._resolve_device()

                if device is None:
                    logger.warning(
                        "Power Watchdog %s not found (offline?), "
                        "retrying in %.0fs",
                        self.address, self.OFFLINE_POLL_INTERVAL,
                    )
                    await self._interruptible_sleep(self.OFFLINE_POLL_INTERVAL)
                    continue

                # Step 2: Connect.  bleak-retry-connector owns the retry
                # cadence and the error classification; BleakClient here is
                # the connection manager's routed wrapper whenever the
                # catcher is installed, so each attempt picks its adapter,
                # takes its claims and tunes its connection parameters.
                logger.info(
                    "Connecting to Power Watchdog %s...", self.address,
                )

                # validate_connection is a surplus kwarg, forwarded to the
                # client class: the routed wrapper acts on it, and plain
                # bleak ignores it (so with the manager off, a bad GATT
                # table is still caught below, just without the retry).
                client = await establish_connection(
                    BleakClient,
                    device,
                    "Power Watchdog %s" % self.address,
                    max_attempts=4,
                    validate_connection=VALIDATE_CONNECTION,
                )

                # Step 3: Connected — resolve GATT and pick protocol
                logger.info(
                    "Connected to Power Watchdog %s (MTU: %d)",
                    self.address, client.mtu_size,
                )
                self._connected = True
                self._notfound_streak = 0
                delay = self.reconnect_delay

                n_svc = sum(1 for _ in client.services)
                n_char = sum(
                    len(svc.characteristics) for svc in client.services
                )
                logger.debug(
                    "GATT table for %s (%d services, %d characteristics):\n%s",
                    self.address,
                    n_svc,
                    n_char,
                    format_gatt_snapshot(client),
                )

                notify_uuid, write_uuid, write_resp, gatt_mode = (
                    resolve_power_watchdog_gatt(client)
                )
                logger.info(
                    "GATT mode %s: notify=%s write=%s (write_response=%s)",
                    gatt_mode,
                    notify_uuid,
                    write_uuid,
                    write_resp,
                )

                # Select and initialize protocol handler
                device_name = getattr(device, "name", None)
                if gatt_mode == "gen1_uart":
                    proto = Gen1Protocol()
                else:
                    proto = Gen2Protocol()
                proto.init_state(self, device_name=device_name)

                # Watchdog before subscribe, so a frame arriving during the
                # handshake already counts as a sign of life.
                self._watchdog = NotificationWatchdog(
                    timeout=NOTIFICATION_WATCHDOG_TIMEOUT,
                    on_timeout=self._on_watchdog_timeout,
                    name=self.address,
                )

                # Subscribe to notifications.  The watchdog is stamped by
                # the protocol handlers on structurally valid packets, not
                # here on raw frames: a link streaming garbage is as dead
                # as a silent one, and a raw-frame stamp would hide a parse
                # failure from the watchdog — with only the daemon's 900s
                # process-restart backstop left to notice (2026-08-26).
                #
                # The try/except is the other half of the same lesson: an
                # exception here otherwise unwinds into dbus_fast's message
                # pump, which logs it under its own name and swallows it,
                # leaving this session loop running and none the wiser.
                # First failure logs the traceback; the rest are counted,
                # because at polling rates a per-frame traceback churns the
                # log rotation fast enough to destroy the evidence.
                parse_failures = {"count": 0}

                def handler(sender, data, _proto=proto, _pf=parse_failures):
                    try:
                        _proto.notification_handler(self, sender, data)
                    except Exception:
                        _pf["count"] += 1
                        if _pf["count"] == 1:
                            logger.exception(
                                "Notification handler failed for %s "
                                "(further failures this session are "
                                "counted, not logged)",
                                self.address,
                            )

                logger.debug("Subscribing to notifications on %s", notify_uuid)
                # No log-and-raise here: the session handler below logs it
                # once, with the right severity for the error type.  Logging
                # in both places is how one BlueZ refusal became two ~15-line
                # stacks in the service log.
                await asyncio.wait_for(
                    client.start_notify(notify_uuid, handler),
                    timeout=5.0,
                )
                logger.debug("Notifications enabled on %s", notify_uuid)

                # Protocol-specific post-subscribe action (handshake or no-op)
                await proto.after_subscribe(client, write_uuid, write_resp)

                # Step 4: Arm the notification watchdog
                self._watchdog.start()

                # Step 5: Stay connected while data is actually flowing.
                # client.is_connected alone is not a safe liveness signal —
                # a link BlueZ still believes in can carry nothing.
                while (
                    client.is_connected
                    and self._running
                    and not self._reconnect_requested
                    and not self._data_is_stale()
                ):
                    await self._interruptible_sleep(1.0)

                self._connected = False
                if parse_failures["count"] > 1:
                    logger.warning(
                        "%d notification handler failures during this "
                        "session for %s (first was logged with traceback)",
                        parse_failures["count"], self.address,
                    )
                if not self._running:
                    logger.info(
                        "BLE session end for %s: service stopping",
                        self.address,
                    )
                elif self._reconnect_requested:
                    logger.warning(
                        "BLE session end for %s: watchdog forced reconnect",
                        self.address,
                    )
                elif self._data_is_stale():
                    logger.warning(
                        "BLE session end for %s: no notifications for over "
                        "%.0fs and the watchdog never fired",
                        self.address,
                        NOTIFICATION_WATCHDOG_TIMEOUT * self.STALE_DATA_FACTOR,
                    )
                elif not client.is_connected:
                    logger.warning(
                        "BLE link dropped for %s (BlueZ/peripheral closed "
                        "connection)",
                        self.address,
                    )
                else:
                    logger.info(
                        "Disconnecting from Power Watchdog %s...",
                        self.address,
                    )

            except BleakOutOfConnectionSlotsError:
                # Every eligible adapter is at its configured link cap, or
                # the controller itself refused another link. Not our
                # device's fault and not something retrying harder fixes:
                # wait for somebody else's link to end.
                self._connected = False
                next_delay = self.reconnect_delay
                logger.warning(
                    "No BLE connection slot free for %s, retrying in %.0fs",
                    self.address, next_delay,
                )

            except BleakNotFoundError:
                # We resolved the device but it was gone by the time the
                # connect completed — bleak-retry-connector exhausted its
                # attempts against a device that stopped answering, so this
                # is not an error worth a traceback. It was advertising
                # seconds ago, so assume a blip once before conceding to the
                # slow offline poll.
                self._connected = False
                self._notfound_streak += 1
                # Whatever we connected to is not there. If it came from the
                # cache, that entry is stale; scan next time rather than
                # failing the same way forever.
                self._force_scan = True
                next_delay = (
                    self.reconnect_delay
                    if self._notfound_streak == 1
                    else self.OFFLINE_POLL_INTERVAL
                )
                logger.warning(
                    "Power Watchdog %s vanished between resolve and connect, "
                    "retrying in %.0fs",
                    self.address, next_delay,
                )

            except BleakError as exc:
                # A BlueZ refusal is an expected operating condition on a
                # shared radio, not a program defect: the adapter is busy,
                # the object path went stale, bluetoothd restarted. One
                # WARNING line naming the error carries everything the stack
                # would, and a 15-line trace per retry buries the history of
                # the very storm you would read the log to understand — 65%
                # of this service's retained log was once one such trace,
                # repeated 362 times.
                self._connected = False
                self._log_ble_error(exc, delay)

            except Exception:
                self._connected = False
                logger.exception(
                    "BLE connection error for %s, retrying in %.0fs",
                    self.address, delay,
                )

            finally:
                # Stop watchdog
                if self._watchdog is not None:
                    self._watchdog.stop()
                    self._watchdog = None

                # Explicit disconnect with timeout
                if client is not None:
                    try:
                        await asyncio.wait_for(
                            client.disconnect(), timeout=5.0
                        )
                    except Exception:
                        pass
                    client = None

            if not self._running:
                break

            if next_delay is None:
                logger.warning(
                    "Power Watchdog %s disconnected", self.address,
                )
                next_delay = delay
                delay = min(delay * 1.5, self.reconnect_max_delay)

            await self._interruptible_sleep(next_delay)

    def _log_ble_error(self, exc: BaseException, delay: float) -> None:
        """Log one line for an expected BLE error, rate-limited per kind.

        Keyed by error type plus its D-Bus error name, so a genuinely new
        failure is never suppressed by an unrelated one already repeating.
        While a kind is being suppressed the occurrences are counted and
        reported with the next line it does emit, because "this happened
        847 times" is the fact worth having — not 847 copies of it.
        """
        detail = str(exc).strip().splitlines()
        detail = detail[0] if detail else exc.__class__.__name__
        # D-Bus errors lead with a bracketed name; that is the useful key.
        name = detail.split("]")[0].lstrip("[") if detail.startswith("[") else ""
        key = (exc.__class__.__name__, name)

        now = time.monotonic()
        last, suppressed = self._ble_error_log.get(key, (0.0, 0))
        if now - last < BLE_ERROR_LOG_INTERVAL:
            self._ble_error_log[key] = (last, suppressed + 1)
            return

        self._ble_error_log[key] = (now, 0)
        repeat = (
            " (%d more since the last report)" % suppressed if suppressed else ""
        )
        logger.warning(
            "BLE error for %s: %s: %s%s — retrying in %.0fs",
            self.address, exc.__class__.__name__, detail, repeat, delay,
        )

    async def _resolve_device(self) -> BLEDevice | None:
        """Resolve a BLEDevice for our address: BlueZ cache first, then scan.

        The cache lookup costs no radio time and is usually enough — BlueZ
        remembers what it has seen — and it yields a device carrying its
        D-Bus path, which is what decides the adapter the link is made on.
        A scan is the fallback for a device BlueZ has forgotten, or for the
        first connect after a bluetoothd restart.
        """
        adapter = scan_adapter_for(self.address, self._ble_adapters or [])

        if self._force_scan:
            self._force_scan = False
            device = None
        else:
            device = await self._device_from_cache(adapter)

        if device is not None:
            logger.info(
                "Resolved Power Watchdog %s from the BlueZ cache%s",
                self.address,
                " on %s" % adapter if adapter else "",
            )
            return device

        logger.info(
            "Scanning for Power Watchdog %s%s...",
            self.address,
            " on %s" % adapter if adapter else "",
        )
        kwargs = {"adapter": adapter} if adapter else {}
        return await BleakScanner.find_device_by_address(
            self.address, timeout=SCAN_TIMEOUT, **kwargs
        )

    async def _device_from_cache(self, adapter: str | None) -> BLEDevice | None:
        """Look our address up in BlueZ's device cache, or None."""
        try:
            return (
                await get_device_by_adapter(self.address, adapter)
                if adapter
                else await get_device(self.address)
            )
        except Exception:
            # A cache miss is normal; a cache *failure* (no D-Bus, no
            # bluetoothd) is worth saying once, then falling through to the
            # scan, which fails with a better message if it is really down.
            logger.exception(
                "BlueZ lookup failed for %s, falling back to a scan",
                self.address,
            )
            return None

    def _data_is_stale(self) -> bool:
        """Return True if notifications have been absent for too long.

        Checked inline by the session loop so that teardown never depends
        on the NotificationWatchdog task still being alive. The watchdog is
        the fast path; this is the backstop for the watchdog itself dying
        — cancelled, or its callback raising. A dead watchdog plus a loop
        that trusted ``client.is_connected`` is what wedged this service
        for four hours on 2026-08-09.
        """
        wd = self._watchdog
        if wd is None:
            return False
        limit = NOTIFICATION_WATCHDOG_TIMEOUT * self.STALE_DATA_FACTOR
        return (time.monotonic() - wd.last_activity) > limit

    async def _on_watchdog_timeout(self):
        """Called by NotificationWatchdog after 2 minutes of silence.

        Sets the flag the session loop actually reads and wakes it at once.
        Declared async because the watchdog awaits it — a plain def would
        return None and `await None` is a TypeError, which is precisely how
        the watchdog died on 2026-08-09.
        """
        logger.warning(
            "BLE watchdog: no notifications for %.0fs from %s, "
            "forcing reconnect",
            NOTIFICATION_WATCHDOG_TIMEOUT, self.address,
        )
        self._connected = False
        self._reconnect_requested = True
        self._cancel_sleep()

    async def _interruptible_sleep(self, seconds: float):
        """Sleep that can be cancelled by stop()."""
        try:
            self._sleep_task = asyncio.ensure_future(asyncio.sleep(seconds))
            await self._sleep_task
        except asyncio.CancelledError:
            pass
        finally:
            self._sleep_task = None
