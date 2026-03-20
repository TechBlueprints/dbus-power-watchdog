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

"""Gen1 Power Watchdog raw Modbus-style BLE protocol.

Gen1 (BT-only, PM* names) devices stream telemetry without any handshake.
Data arrives as pairs of 20-byte BLE notifications on characteristic
``0000ffe2``:

1. First chunk starts with ``01 03 20`` (Modbus read-holding-registers
   response: slave 1, function 3, 32 data bytes).
2. Second chunk is appended to form a 40-byte merged buffer.

Parsed layout (offsets into 40-byte buffer):
    [0:3]   header  (01 03 20)
    [3:7]   voltage   (big-endian uint32 / 10000 = V)
    [7:11]  current   (big-endian uint32 / 10000 = A)
    [11:15] power     (big-endian uint32 / 10000 = W)
    [15:19] energy    (big-endian uint32 / 10000 = kWh)
    [31:35] frequency (big-endian uint32 / 100 = Hz)
    [37:40] line markers (L1/L2 detection for 50A dual-line)

Byte 19 is an error code on v2/v3 devices (0 = no error).  For v1
devices the byte is unused and typically zero.

For 50A dual-line models, L1 and L2 updates alternate as separate
40-byte frames.  Line detection uses bytes [37:40]:
    - v2/v3 devices: (1,1,1) = L2, anything else = L1
    - v1 devices: (0,0,0) = L2, but only after a non-zero marker
      confirms the device is dual-line

Since we cannot know the hardware version from the BLE data stream
alone, we infer it: the first time markers (1,1,1) appear, we set
an internal ``_gen1_is_v2v3`` flag that locks the device into the
v2/v3 line-detection path for the rest of the connection.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bleak import BleakClient
    from power_watchdog_ble import PowerWatchdogBLE

from power_watchdog_ble import LineData

logger = logging.getLogger(__name__)

# ── Protocol constants ──────────────────────────────────────────────────────

GEN1_CHUNK_SIZE = 20           # each BLE notification is exactly 20 bytes
GEN1_MERGED_SIZE = 40          # two chunks merged
GEN1_HEADER = bytes([0x01, 0x03, 0x20])  # first chunk sentinel

_RX_NOTIFY_LOG_MAX = 5


# ── Protocol class ──────────────────────────────────────────────────────────

class Gen1Protocol:
    """Gen1 raw Modbus-style telemetry protocol handler."""

    def init_state(
        self, ble: PowerWatchdogBLE, device_name: str | None = None,
    ) -> None:
        """Reset protocol-specific state on the BLE instance for a new connection.

        If *device_name* is provided, the hardware version and line type
        are derived from the BLE advertised name (matching the official
        app's logic) so that line detection is correct from the very
        first telemetry frame.
        """
        ble._gen1_first_chunk = None
        ble._gen1_is_v2v3 = False
        ble._rx_notify_log_count = 0
        ble._logged_first_valid_frame = False

        if device_name:
            from power_watchdog_ble import classify_device
            classified = classify_device(device_name)
            if classified and classified.generation == 1:
                if classified.hw_version in (2, 3):
                    ble._gen1_is_v2v3 = True
                    logger.info(
                        "Gen1 v%d detected from BLE name — using "
                        "(1,1,1) L2 markers", classified.hw_version,
                    )
                elif (classified.hw_version == 1
                      and classified.line_type == "double"):
                    ble._data.has_l2 = True
                    logger.info(
                        "Gen1 v1 dual-line detected from BLE name — "
                        "pre-setting has_l2",
                    )

    def notification_handler(self, ble: PowerWatchdogBLE, _sender, data: bytearray) -> None:
        """Reassemble 20-byte chunk pairs into 40-byte telemetry frames."""
        cnt = getattr(ble, "_rx_notify_log_count", 0)
        if cnt < _RX_NOTIFY_LOG_MAX:
            ble._rx_notify_log_count = cnt + 1
            logger.info(
                "RX notify #%d (gen1): %d bytes hex=%s",
                cnt + 1,
                len(data),
                bytes(data[:40]).hex(),
            )

        if len(data) != GEN1_CHUNK_SIZE:
            logger.debug(
                "Gen1: ignoring %d-byte notification (expected %d)",
                len(data), GEN1_CHUNK_SIZE,
            )
            return

        if data[:3] == GEN1_HEADER:
            ble._gen1_first_chunk = bytes(data)
            return

        first = ble._gen1_first_chunk
        if first is None or len(first) != GEN1_CHUNK_SIZE:
            return

        ble._gen1_first_chunk = None
        merged = first + bytes(data)

        wd = getattr(ble, "_watchdog", None)
        if wd is not None:
            wd.notify_activity()

        if not getattr(ble, "_logged_first_valid_frame", False):
            ble._logged_first_valid_frame = True
            logger.info(
                "First valid Gen1 telemetry frame: 40 bytes hex=%s",
                merged.hex(),
            )

        parse_gen1_telemetry(ble, merged)

    async def after_subscribe(
        self, client: BleakClient, write_uuid: str, write_resp: bool,
    ) -> None:
        """No-op — Gen1 streams telemetry without a handshake."""
        logger.info(
            "Gen1 UART mode: waiting for raw telemetry "
            "(20+20 byte chunks, header 01 03 20)...",
        )


# ── Telemetry parsing ───────────────────────────────────────────────────────

def parse_gen1_telemetry(ble: PowerWatchdogBLE, buf: bytes) -> None:
    """Parse a 40-byte merged Gen1 telemetry buffer into LineData."""
    if len(buf) != GEN1_MERGED_SIZE:
        logger.warning(
            "Gen1 merged buffer wrong size: %d (expected %d)",
            len(buf), GEN1_MERGED_SIZE,
        )
        return

    voltage = struct.unpack_from(">I", buf, 3)[0] / 10000.0
    current = struct.unpack_from(">I", buf, 7)[0] / 10000.0
    power = struct.unpack_from(">I", buf, 11)[0] / 10000.0
    energy = struct.unpack_from(">I", buf, 15)[0] / 10000.0
    error_code = buf[19]
    frequency = struct.unpack_from(">I", buf, 31)[0] / 100.0

    line_markers = (buf[37], buf[38], buf[39])

    ld = LineData(
        voltage=voltage,
        current=current,
        power=power,
        energy=energy,
        frequency=frequency,
        error_code=error_code,
    )

    raw_hex = buf.hex()
    is_l1 = True

    with ble._data_lock:
        if line_markers == (1, 1, 1):
            # v2/v3 L2 marker — also locks version inference
            ble._gen1_is_v2v3 = True
            ble._data.l2 = ld
            ble._data.has_l2 = True
            is_l1 = False
        elif getattr(ble, "_gen1_is_v2v3", False):
            # v2/v3 device: everything except (1,1,1) is L1
            ble._data.l1 = ld
        elif line_markers == (0, 0, 0):
            # v1: (0,0,0) is L2 only when dual-line is confirmed
            if ble._data.has_l2:
                ble._data.l2 = ld
                is_l1 = False
            else:
                ble._data.l1 = ld
        else:
            # v1: non-zero marker confirms dual-line, frame is L1
            ble._data.l1 = ld
            ble._data.has_l2 = True
        ble._data.timestamp = time.time()
        ble._data.raw_hex = raw_hex

    logger.debug(
        "Gen1 L%s: %.1fV %.2fA %.1fW %.3fkWh %.1fHz err=%d markers=%s",
        "1" if is_l1 else "2",
        voltage, current, power, energy, frequency, error_code,
        line_markers,
    )
