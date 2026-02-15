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
  1. Scan and connect
  2. Subscribe to notifications on 0000ff01
  3. Request MTU 230
  4. Send handshake: "!%!%,protocol,open,"
  5. Parse incoming framed packets
"""

import asyncio
import logging
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

# Use bleak from dbus-serialbattery's vendored copy if not installed system-wide
_serialbattery_ext = "/data/apps/dbus-serialbattery/ext"
if os.path.isdir(_serialbattery_ext) and _serialbattery_ext not in sys.path:
    sys.path.insert(0, _serialbattery_ext)

from bleak import BleakClient, BleakScanner, BleakError  # noqa: E402

logger = logging.getLogger(__name__)


# ── Discovery name patterns ─────────────────────────────────────────────────

# Gen2 (WiFi+BT) devices advertise as "WD_{type}_{serialhex}"
# Types: E5, E6, E7, E8, E9, V5, V6, V7, V8, V9
GEN2_PREFIX = "WD_"

# Gen1 (BT-only) devices advertise as "PM{S|D}..." (19 or 27 chars)
# S = single/30A, D = double/50A
GEN1_PREFIX = "PM"

# Maximum retries when BLE scanner reports InProgress
SCAN_MAX_RETRIES = 3
SCAN_RETRY_DELAY = 5.0  # seconds between retries

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
    mac: str               # MAC address (e.g., "24:EC:4A:E4:69:A5")
    name: str              # BLE advertised name (e.g., "WD_E7_26ec4ae469a5")
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


async def _scan_once(
    adapter: str = "",
    timeout: float = 15.0,
) -> list[DiscoveredDevice]:
    """Perform a single BLE scan and return discovered Power Watchdog devices.

    Args:
        adapter: Bluetooth adapter to use (e.g., "hci0"). Empty for default.
        timeout: Scan timeout in seconds.

    Returns:
        List of DiscoveredDevice instances found during this scan.

    Raises:
        BleakError: If the scanner reports an error (e.g., InProgress).
    """
    kwargs = {}
    if adapter:
        kwargs["adapter"] = adapter

    devices = await BleakScanner.discover(timeout=timeout, **kwargs)
    found: list[DiscoveredDevice] = []

    for device in devices:
        name = device.name or ""
        classified = classify_device(name)
        if classified is not None:
            classified.mac = device.address
            found.append(classified)
            logger.info(
                "Discovered Power Watchdog: %s (%s) gen%d %s %s",
                name, device.address, classified.generation,
                classified.device_type, classified.line_type,
            )

    return found


async def scan_for_devices(
    adapters: list[str] | None = None,
    timeout: float = 15.0,
) -> list[DiscoveredDevice]:
    """Scan for Power Watchdog BLE devices with retry and adapter rotation.

    Handles BLE InProgress errors by retrying with exponential backoff.
    If multiple adapters are specified, rotates through them on failure.

    Args:
        adapters: List of adapters to try (e.g., ["hci0", "hci1"]).
                  None or empty list means use the default adapter.
        timeout: Scan timeout per attempt in seconds.

    Returns:
        List of all unique DiscoveredDevice instances found.
    """
    if not adapters:
        adapters = [""]  # empty string = default adapter

    seen_macs: set[str] = set()
    all_found: list[DiscoveredDevice] = []

    for adapter in adapters:
        adapter_label = adapter or "default"
        retries = 0
        delay = SCAN_RETRY_DELAY

        while retries < SCAN_MAX_RETRIES:
            try:
                logger.info(
                    "Scanning for Power Watchdog devices on %s (attempt %d/%d)...",
                    adapter_label, retries + 1, SCAN_MAX_RETRIES,
                )
                found = await _scan_once(adapter=adapter, timeout=timeout)
                for dev in found:
                    if dev.mac not in seen_macs:
                        seen_macs.add(dev.mac)
                        all_found.append(dev)
                # Successful scan -- break retry loop for this adapter
                break

            except BleakError as e:
                err_str = str(e).lower()
                if "inprogress" in err_str or "in progress" in err_str:
                    retries += 1
                    if retries < SCAN_MAX_RETRIES:
                        logger.warning(
                            "BLE scan InProgress on %s, retrying in %.0fs (%d/%d)",
                            adapter_label, delay, retries, SCAN_MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * 1.5, 30.0)
                    else:
                        logger.warning(
                            "BLE scan InProgress on %s after %d retries, "
                            "moving to next adapter",
                            adapter_label, SCAN_MAX_RETRIES,
                        )
                else:
                    logger.error("BLE scan error on %s: %s", adapter_label, e)
                    break  # non-retryable error

            except Exception:
                logger.exception("Unexpected error scanning on %s", adapter_label)
                break

    if all_found:
        logger.info(
            "Discovery complete: found %d Power Watchdog device(s)", len(all_found)
        )
    else:
        logger.info("Discovery complete: no Power Watchdog devices found")

    return all_found


# ── BLE client ──────────────────────────────────────────────────────────────

class PowerWatchdogBLE:
    """BLE client that runs in a daemon thread and exposes data to the main thread."""

    def __init__(
        self,
        address: str,
        adapter: str = "",
        reconnect_delay: float = 10.0,
        reconnect_max_delay: float = 120.0,
    ):
        self.address = address
        self.adapter = adapter
        self.reconnect_delay = reconnect_delay
        self.reconnect_max_delay = reconnect_max_delay

        self._data = WatchdogData()
        self._data_lock = threading.Lock()
        self._connected = False
        self._running = True

        # asyncio event loop reference (set by daemon thread)
        self._loop = None
        self._sleep_task = None

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

        # If there's a running asyncio loop, cancel the sleep so the
        # disconnect happens immediately rather than waiting up to 1s.
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._cancel_sleep)

        # Wait for the daemon thread to finish its disconnect.
        # Without this, the process exits and the daemon thread is killed
        # before BleakClient's context manager can call disconnect(),
        # leaving BlueZ with a stale connection.
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
        """Connect, subscribe, handshake, and stay connected."""
        delay = self.reconnect_delay

        while self._running:
            try:
                logger.info("Scanning for Power Watchdog %s...", self.address)

                kwargs = {}
                if self.adapter:
                    kwargs["adapter"] = self.adapter

                device = await BleakScanner.find_device_by_address(
                    self.address, timeout=20.0, **kwargs
                )
                if device is None:
                    logger.warning(
                        "Power Watchdog %s not found, retrying in %.0fs",
                        self.address, delay,
                    )
                    try:
                        self._sleep_task = asyncio.ensure_future(
                            asyncio.sleep(delay)
                        )
                        await self._sleep_task
                    except asyncio.CancelledError:
                        break
                    finally:
                        self._sleep_task = None
                    delay = min(delay * 1.5, self.reconnect_max_delay)
                    continue

                logger.info("Connecting to Power Watchdog %s...", self.address)

                async with BleakClient(device, **kwargs) as client:
                    logger.info(
                        "Connected to Power Watchdog %s (MTU: %d)",
                        self.address,
                        client.mtu_size,
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
                    await client.start_notify(
                        CHARACTERISTIC_UUID, self._notification_handler
                    )

                    # Send handshake to start data flow
                    logger.info("Sending handshake...")
                    await client.write_gatt_char(
                        CHARACTERISTIC_UUID, HANDSHAKE_PAYLOAD, response=True
                    )
                    logger.info("Handshake sent, waiting for data...")

                    # Stay connected while client is alive
                    while client.is_connected and self._running:
                        try:
                            self._sleep_task = asyncio.ensure_future(
                                asyncio.sleep(1.0)
                            )
                            await self._sleep_task
                        except asyncio.CancelledError:
                            break
                        finally:
                            self._sleep_task = None

                    self._connected = False
                    logger.info("Disconnecting from Power Watchdog...")
                    # BleakClient context manager calls disconnect() here

                logger.warning("Power Watchdog disconnected")

            except Exception:
                self._connected = False
                logger.exception(
                    "BLE connection error, retrying in %.0fs", delay,
                )
                try:
                    self._sleep_task = asyncio.ensure_future(
                        asyncio.sleep(delay)
                    )
                    await self._sleep_task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._sleep_task = None
                delay = min(delay * 1.5, self.reconnect_max_delay)

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
