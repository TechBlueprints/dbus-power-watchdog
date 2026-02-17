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

The device uses a framed binary protocol over GATT characteristic 0000ff01.
Each BLE notification contains (potentially partial) packet data:

    [0x24797740]  4-byte identifier
    [version]     1 byte
    [msgId]       1 byte
    [cmd]         1 byte  (1=DLReport, 2=ErrorReport, 14=Alarm)
    [dataLen]     2 bytes (big-endian)
    [body]        dataLen bytes
    [0x7121]      2-byte tail

DLReport body (cmd=1):
  - 34 bytes per line (30A = 34 bytes total, 50A = 68 bytes for L1+L2)
  - Each 34-byte DLData block:
      [0:4]   inputVoltage  (big-endian int32, /10000 = V)
      [4:8]   current       (big-endian int32, /10000 = A)
      [8:12]  power         (big-endian int32, /10000 = W)
      [12:16] energy        (big-endian int32, /10000 = kWh)
      [16:20] temp1         (unused)
      [20:24] outputVoltage (big-endian int32, /10000 = V)
      [24]    backlight
      [25]    neutralDetection
      [26]    boost flag
      [27]    temperature
      [28:32] frequency     (big-endian int32, /100 = Hz)
      [32]    error code    (0-9)
      [33]    status

Connection sequence:
  1. Scan and connect (via bleak-connection-manager)
  2. Subscribe to notifications on 0000ff01
  3. Request MTU 230
  4. Send handshake: "!%!%,protocol,open,"
  5. Parse incoming framed packets
"""

import asyncio
import logging
import struct
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

# ── Protocol constants ──────────────────────────────────────────────────────

# GATT characteristic (same UUID for notify and write)
CHARACTERISTIC_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

# Handshake payload: ASCII "!%!%,protocol,open,"
HANDSHAKE_PAYLOAD = bytes.fromhex("212521252c70726f746f636f6c2c6f70656e2c")

# Packet framing
PACKET_IDENTIFIER = 0x24797740  # 4-byte magic
PACKET_TAIL = 0x7121  # 2-byte tail
HEADER_SIZE = 9  # 4 (identifier) + 1 (version) + 1 (msgId) + 1 (cmd) + 2 (dataLen)
TAIL_SIZE = 2
MAX_BUFFER_SIZE = 8192

# Command IDs
CMD_DL_REPORT = 1
CMD_ERROR_REPORT = 2
CMD_ALARM = 14

# DLData block size (per line)
DL_DATA_SIZE = 34

# Notification watchdog: force reconnect if no BLE notifications arrive
# within this window.  Power Watchdog sends updates ~every 30s, so 2 minutes
# of silence almost certainly means the radio link is dead.
NOTIFICATION_WATCHDOG_TIMEOUT = 120.0  # seconds


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
    """

    def __init__(
        self,
        address: str,
        reconnect_delay: float = 10.0,
        reconnect_max_delay: float = 120.0,
        lock_config: LockConfig | None = None,
        scan_lock_config: ScanLockConfig | None = None,
    ):
        self.address = address
        self.reconnect_delay = reconnect_delay
        self.reconnect_max_delay = reconnect_max_delay

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

        # Packet reassembly buffer (notifications may be fragmented)
        self._rx_buffer = bytearray()

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
        """Connect, subscribe, handshake, and stay connected.

        Uses bleak-connection-manager for all BLE operations:
        1. managed_find_device — scan with lock + adapter rotation
        2. establish_connection — connect with all workarounds
        3. ConnectionWatchdog — detect dead radio links
        """
        delay = self.reconnect_delay
        adapters = discover_adapters()
        escalation = EscalationPolicy(adapters, config=EscalationConfig(reset_adapter=False))

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
                    scan_lock_config=self._scan_lock_config,
                )

                if device is None:
                    logger.warning(
                        "Power Watchdog %s not found, retrying in %.0fs",
                        self.address, delay,
                    )
                    await self._interruptible_sleep(delay)
                    delay = min(delay * 1.5, self.reconnect_max_delay)
                    continue

                # Step 2: Connect via BCM (handles phantom cleanup,
                # adapter rotation, InProgress, GATT validation, etc.)
                logger.info(
                    "Connecting to Power Watchdog %s...", self.address,
                )

                client = await establish_connection(
                    BleakClient,
                    device,
                    "Power Watchdog %s" % self.address,
                    max_attempts=4,
                    close_inactive_connections=True,
                    try_direct_first=True,
                    validate_connection=validate_gatt_services,
                    lock_config=self._lock_config,
                    escalation_policy=escalation,
                    overall_timeout=300.0,
                )

                # Step 3: Connected — set up notifications and handshake
                logger.info(
                    "Connected to Power Watchdog %s (MTU: %d)",
                    self.address, client.mtu_size,
                )
                self._connected = True
                self._rx_buffer.clear()
                delay = self.reconnect_delay  # reset backoff

                # Log discovered services for debugging
                for svc in client.services:
                    logger.debug("Service: %s", svc.uuid)
                    for char in svc.characteristics:
                        logger.debug(
                            "  Char: %s [%s]",
                            char.uuid,
                            ",".join(char.properties),
                        )

                # Subscribe to notifications
                logger.info(
                    "Subscribing to notifications on %s",
                    CHARACTERISTIC_UUID,
                )
                await asyncio.wait_for(
                    client.start_notify(
                        CHARACTERISTIC_UUID, self._notification_handler
                    ),
                    timeout=5.0,
                )

                # Send handshake to start data flow
                logger.info("Sending handshake...")
                await client.write_gatt_char(
                    CHARACTERISTIC_UUID, HANDSHAKE_PAYLOAD, response=True
                )
                logger.info("Handshake sent, waiting for data...")

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
                logger.info(
                    "Disconnecting from Power Watchdog %s...", self.address
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

    # ── Notification handling and packet parsing ────────────────────────────

    def _notification_handler(self, _sender, data: bytearray):
        """Handle incoming BLE notification: buffer and parse framed packets."""
        self._rx_buffer.extend(data)

        # Safety: prevent unbounded buffer growth
        if len(self._rx_buffer) > MAX_BUFFER_SIZE:
            logger.warning("RX buffer overflow (%d bytes), clearing", len(self._rx_buffer))
            self._rx_buffer.clear()
            return

        # Try to extract complete packets
        while self._try_parse_packet():
            pass

    def _try_parse_packet(self) -> bool:
        """Try to extract and dispatch one complete packet from the RX buffer.

        Returns True if a packet was consumed (even if invalid), False if
        more data is needed.
        """
        buf = self._rx_buffer

        # Scan for the 4-byte identifier
        while len(buf) >= 4:
            ident = struct.unpack_from(">I", buf, 0)[0]
            if ident == PACKET_IDENTIFIER:
                break
            # Not at a packet boundary -- skip one byte
            del buf[0]

        if len(buf) < HEADER_SIZE:
            return False  # need more data for header

        # Parse header
        cmd = buf[6]
        data_len = struct.unpack_from(">H", buf, 7)[0]

        if data_len > MAX_BUFFER_SIZE:
            logger.warning("Invalid dataLen %d, discarding", data_len)
            del buf[:4]  # skip past identifier
            return True

        total_len = HEADER_SIZE + data_len + TAIL_SIZE

        if len(buf) < total_len:
            return False  # need more data for body + tail

        # Extract body
        body = bytes(buf[HEADER_SIZE : HEADER_SIZE + data_len])

        # Verify tail
        tail = struct.unpack_from(">H", buf, HEADER_SIZE + data_len)[0]

        # Save raw hex for debugging before consuming
        raw_hex = buf[:total_len].hex()

        # Consume this packet from the buffer
        del buf[:total_len]

        if tail != PACKET_TAIL:
            logger.debug("Bad packet tail 0x%04X (expected 0x%04X)", tail, PACKET_TAIL)
            return True  # consumed bytes, try next

        # Valid packet — feed the connection watchdog.
        # This is the ONLY place it gets fed, proving the device is
        # sending real, framing-verified data.
        if self._watchdog is not None:
            self._watchdog.notify_activity()

        # Dispatch by command
        if cmd == CMD_DL_REPORT:
            self._parse_dl_report(body, raw_hex)
        elif cmd == CMD_ERROR_REPORT:
            logger.debug("ErrorReport received (%d bytes body)", len(body))
        elif cmd == CMD_ALARM:
            logger.warning("Alarm notification received from Power Watchdog")
        else:
            logger.debug("Unknown cmd %d (%d bytes body)", cmd, len(body))

        return True

    def _parse_dl_report(self, body: bytes, raw_hex: str):
        """Parse a DLReport body (34 bytes = single line, 68 bytes = dual line)."""
        if len(body) == DL_DATA_SIZE:
            # 30A single-line or single-leg report
            l1 = self._parse_dl_data(body, 0)
            with self._data_lock:
                self._data.l1 = l1
                self._data.has_l2 = False
                self._data.timestamp = time.time()
                self._data.raw_hex = raw_hex
            logger.debug(
                "L1: %.1fV %.2fA %.1fW %.3fkWh %.1fHz err=%d",
                l1.voltage, l1.current, l1.power,
                l1.energy, l1.frequency, l1.error_code,
            )

        elif len(body) == DL_DATA_SIZE * 2:
            # 50A dual-line report: first 34 bytes = L1, second 34 bytes = L2
            l1 = self._parse_dl_data(body, 0)
            l2 = self._parse_dl_data(body, DL_DATA_SIZE)
            with self._data_lock:
                self._data.l1 = l1
                self._data.l2 = l2
                self._data.has_l2 = True
                self._data.timestamp = time.time()
                self._data.raw_hex = raw_hex
            logger.debug(
                "L1: %.1fV %.2fA %.1fW | L2: %.1fV %.2fA %.1fW",
                l1.voltage, l1.current, l1.power,
                l2.voltage, l2.current, l2.power,
            )

        else:
            logger.warning(
                "Unexpected DLReport body length: %d (expected %d or %d)",
                len(body), DL_DATA_SIZE, DL_DATA_SIZE * 2,
            )

    @staticmethod
    def _parse_dl_data(body: bytes, offset: int) -> LineData:
        """Parse a single 34-byte DLData block into a LineData object.

        Field layout (all big-endian int32 unless noted):
            [0:4]   inputVoltage  (/10000 = V)
            [4:8]   current       (/10000 = A)
            [8:12]  power         (/10000 = W)
            [12:16] energy        (/10000 = kWh)
            [16:20] temp1         (unused)
            [20:24] outputVoltage (/10000 = V)
            [24]    backlight     (1 byte)
            [25]    neutralDetection (1 byte)
            [26]    boost flag    (1 byte, 1=boosting)
            [27]    temperature   (1 byte)
            [28:32] frequency     (/100 = Hz)
            [32]    error code    (1 byte, 0-9)
            [33]    status        (1 byte)
        """
        o = offset
        voltage_raw = struct.unpack_from(">i", body, o)[0]
        current_raw = struct.unpack_from(">i", body, o + 4)[0]
        power_raw = struct.unpack_from(">i", body, o + 8)[0]
        energy_raw = struct.unpack_from(">i", body, o + 12)[0]
        # temp1 at o+16..o+20 (unused)
        output_v_raw = struct.unpack_from(">i", body, o + 20)[0]
        # backlight at o+24, neutralDetection at o+25
        boost = body[o + 26] == 1
        # temperature at o+27
        freq_raw = struct.unpack_from(">i", body, o + 28)[0]
        error_code = body[o + 32]
        status = body[o + 33]

        return LineData(
            voltage=voltage_raw / 10000.0,
            current=current_raw / 10000.0,
            power=power_raw / 10000.0,
            energy=energy_raw / 10000.0,
            output_voltage=output_v_raw / 10000.0,
            frequency=freq_raw / 100.0,
            error_code=error_code,
            status=status,
            boost=boost,
        )
