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

from bleak import BleakClient, BleakError
from bleak.backends.device import BLEDevice

from bleak_connection_manager import (
    ConnectionWatchdog,
    EscalationConfig,
    EscalationPolicy,
    LockConfig,
    ScanLockConfig,
    discover_adapters,
    establish_connection,
    managed_discover,
    managed_find_device,
    validate_gatt_services,
)

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
# within this window.  Power Watchdog sends updates ~every 30s, so 2 minutes
# of silence almost certainly means the radio link is dead.
NOTIFICATION_WATCHDOG_TIMEOUT = 120.0  # seconds


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
            return DiscoveredDevice(
                mac="",  # filled in by caller
                name=name,
                generation=1,
                device_type=effective_name[:3],  # e.g., "PMD", "PMS"
                line_type=line_type,
            )

    return None


async def scan_for_devices(
    timeout: float = 15.0,
    scan_lock_config: ScanLockConfig | None = None,
) -> list[DiscoveredDevice]:
    """Scan for Power Watchdog BLE devices.

    Uses bleak-connection-manager's managed_discover for automatic
    adapter rotation, scan locking, and InProgress retry.

    Args:
        timeout: Scan timeout per attempt in seconds.
        scan_lock_config: Cross-process scan lock config.  If None,
            a default enabled config is used.

    Returns:
        List of all unique DiscoveredDevice instances found.
    """
    if scan_lock_config is None:
        scan_lock_config = ScanLockConfig(enabled=True)

    devices = await managed_discover(
        timeout=timeout,
        scan_lock_config=scan_lock_config,
    )

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

    Uses bleak-connection-manager for all BLE operations:
    - managed_find_device for scanning (with scan lock + adapter rotation)
    - establish_connection for connecting (with connect lock + all workarounds)
    - ConnectionWatchdog for detecting dead connections

    Protocol-specific notification handling and handshake logic is delegated
    to :class:`~power_watchdog_proto_gen2.Gen2Protocol` or
    :class:`~power_watchdog_proto_gen1.Gen1Protocol` based on GATT resolution.
    """

    # When the device is completely offline (not found during scan),
    # retry at this fixed interval rather than using exponential backoff.
    # The device may be unplugged and will come back at any time.
    OFFLINE_POLL_INTERVAL = 300.0  # 5 minutes

    def __init__(
        self,
        address: str,
        reconnect_delay: float = 10.0,
        reconnect_max_delay: float = 120.0,
        lock_config: LockConfig | None = None,
        scan_lock_config: ScanLockConfig | None = None,
        ble_adapters: list[str] | None = None,
    ):
        self.address = address
        self.reconnect_delay = reconnect_delay
        self.reconnect_max_delay = reconnect_max_delay
        # Pin scans + connections to these HCIs (e.g. ["hci1"]). None = BCM default.
        self._ble_adapters = ble_adapters

        # BCM lock configs — default to enabled
        self._lock_config = lock_config or LockConfig(enabled=True)
        self._scan_lock_config = scan_lock_config or ScanLockConfig(enabled=True)

        self._data = WatchdogData()
        self._data_lock = threading.Lock()
        self._connected = False
        self._running = True

        # asyncio event loop reference (set by daemon thread)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sleep_task: asyncio.Task | None = None

        # Connection watchdog (set when connected)
        self._watchdog: ConnectionWatchdog | None = None

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

        Uses bleak-connection-manager for all BLE operations:
        1. managed_find_device — scan with lock + adapter rotation
        2. establish_connection — connect with all workarounds
        3. ConnectionWatchdog — detect dead radio links

        Protocol-specific notification handling and handshake logic is
        delegated to a protocol object selected after GATT resolution.
        """
        from power_watchdog_proto_gen1 import Gen1Protocol
        from power_watchdog_proto_gen2 import Gen2Protocol

        delay = self.reconnect_delay
        policy_adapters = (
            self._ble_adapters
            if self._ble_adapters
            else discover_adapters()
        )
        if self._ble_adapters:
            logger.info(
                "BLE adapter pin active for %s: %s",
                self.address,
                ", ".join(self._ble_adapters),
            )
        escalation = EscalationPolicy(
            policy_adapters,
            config=EscalationConfig(reset_adapter=False),
        )

        while self._running:
            client: BleakClient | None = None
            device: BLEDevice | None = None

            try:
                # Step 1: Find the device via managed scan
                logger.info(
                    "Scanning for Power Watchdog %s...", self.address,
                )

                device = await managed_find_device(
                    self.address,
                    timeout=20.0,
                    max_attempts=3,
                    adapters=self._ble_adapters,
                    scan_lock_config=self._scan_lock_config,
                )

                if device is None:
                    logger.warning(
                        "Power Watchdog %s not found (offline?), "
                        "retrying in %.0fs",
                        self.address, self.OFFLINE_POLL_INTERVAL,
                    )
                    await self._interruptible_sleep(self.OFFLINE_POLL_INTERVAL)
                    continue

                # Step 2: Connect via BCM
                logger.info(
                    "Connecting to Power Watchdog %s...", self.address,
                )

                client = await establish_connection(
                    BleakClient,
                    device,
                    "Power Watchdog %s" % self.address,
                    max_attempts=4,
                    adapters=self._ble_adapters,
                    close_inactive_connections=True,
                    try_direct_first=True,
                    validate_connection=validate_gatt_services,
                    lock_config=self._lock_config,
                    escalation_policy=escalation,
                    overall_timeout=300.0,
                )

                # Step 3: Connected — resolve GATT and pick protocol
                logger.info(
                    "Connected to Power Watchdog %s (MTU: %d)",
                    self.address, client.mtu_size,
                )
                self._connected = True
                delay = self.reconnect_delay

                n_svc = sum(1 for _ in client.services)
                n_char = sum(
                    len(svc.characteristics) for svc in client.services
                )
                logger.info(
                    "GATT table for %s (%d services, %d characteristics):\n%s",
                    self.address,
                    n_svc,
                    n_char,
                    format_gatt_snapshot(client),
                )

                for svc in client.services:
                    logger.debug("Service: %s", svc.uuid)
                    for char in svc.characteristics:
                        logger.debug(
                            "  Char: %s [%s]",
                            char.uuid,
                            ",".join(char.properties),
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
                if gatt_mode == "gen1_uart":
                    proto = Gen1Protocol()
                else:
                    proto = Gen2Protocol()
                proto.init_state(self)

                # Subscribe to notifications
                handler = lambda sender, data: proto.notification_handler(
                    self, sender, data,
                )
                logger.info("Subscribing to notifications on %s", notify_uuid)
                try:
                    await asyncio.wait_for(
                        client.start_notify(notify_uuid, handler),
                        timeout=5.0,
                    )
                except Exception:
                    logger.exception(
                        "start_notify failed for %s (mode=%s)",
                        notify_uuid,
                        gatt_mode,
                    )
                    raise
                logger.info("Notifications enabled on %s", notify_uuid)

                # Protocol-specific post-subscribe action (handshake or no-op)
                await proto.after_subscribe(client, write_uuid, write_resp)

                # Step 4: Start connection watchdog
                self._watchdog = ConnectionWatchdog(
                    timeout=NOTIFICATION_WATCHDOG_TIMEOUT,
                    on_timeout=self._on_watchdog_timeout,
                    client=client,
                    device=device,
                )
                self._watchdog.start()

                # Step 5: Stay connected while client is alive
                while client.is_connected and self._running:
                    await self._interruptible_sleep(1.0)

                self._connected = False
                if not self._running:
                    logger.info(
                        "BLE session end for %s: service stopping",
                        self.address,
                    )
                elif not client.is_connected:
                    logger.warning(
                        "BLE link dropped for %s (BlueZ/peripheral closed "
                        "connection); saw_valid_frame=%s",
                        self.address,
                        getattr(self, "_logged_first_valid_frame", False),
                    )
                else:
                    logger.info(
                        "Disconnecting from Power Watchdog %s...",
                        self.address,
                    )

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

            logger.warning("Power Watchdog %s disconnected", self.address)
            await self._interruptible_sleep(delay)
            delay = min(delay * 1.5, self.reconnect_max_delay)

    def _on_watchdog_timeout(self):
        """Called by ConnectionWatchdog when no notifications for 2 minutes."""
        logger.warning(
            "BLE watchdog: no notifications for %.0fs from %s, "
            "forcing reconnect",
            NOTIFICATION_WATCHDOG_TIMEOUT, self.address,
        )
        self._connected = False

    async def _interruptible_sleep(self, seconds: float):
        """Sleep that can be cancelled by stop()."""
        try:
            self._sleep_task = asyncio.ensure_future(asyncio.sleep(seconds))
            await self._sleep_task
        except asyncio.CancelledError:
            pass
        finally:
            self._sleep_task = None
