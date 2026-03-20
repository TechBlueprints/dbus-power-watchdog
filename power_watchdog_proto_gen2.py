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

"""Gen2 Power Watchdog framed binary protocol.

Gen2 (WiFi+BT) devices use a custom application protocol over a single
BLE characteristic (``0000ff01``):

1. Send ASCII handshake ``"!%!%,protocol,open,"`` to start data flow.
2. Parse incoming framed packets:

   - ``0x24797740`` 4-byte magic header
   - 1 byte version, 1 byte msgId, 1 byte cmd, 2-byte big-endian dataLen
   - ``dataLen`` bytes of body
   - ``0x7121`` 2-byte tail

DLReport body (cmd=1) is 34 bytes per AC line (single-line = 34, dual = 68).
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

HANDSHAKE_PAYLOAD = bytes.fromhex("212521252c70726f746f636f6c2c6f70656e2c")

PACKET_IDENTIFIER = 0x24797740  # 4-byte magic
PACKET_TAIL = 0x7121            # 2-byte tail
HEADER_SIZE = 9   # 4 (identifier) + 1 (version) + 1 (msgId) + 1 (cmd) + 2 (dataLen)
TAIL_SIZE = 2
MAX_BUFFER_SIZE = 8192

CMD_DL_REPORT = 1
CMD_ERROR_REPORT = 2
CMD_ALARM = 14

DL_DATA_SIZE = 34  # bytes per AC line

_RX_NOTIFY_LOG_MAX = 5


# ── Protocol class ──────────────────────────────────────────────────────────

class Gen2Protocol:
    """Gen2 framed binary protocol handler."""

    def init_state(
        self, ble: PowerWatchdogBLE, device_name: str | None = None,
    ) -> None:
        """Reset protocol-specific state on the BLE instance for a new connection."""
        ble._rx_buffer = bytearray()
        ble._rx_notify_log_count = 0
        ble._logged_bad_tail = False
        ble._logged_first_valid_frame = False

    def notification_handler(self, ble: PowerWatchdogBLE, _sender, data: bytearray) -> None:
        """Buffer incoming bytes and extract framed packets."""
        cnt = getattr(ble, "_rx_notify_log_count", 0)
        if cnt < _RX_NOTIFY_LOG_MAX:
            ble._rx_notify_log_count = cnt + 1
            preview = bytes(data[:64]).hex()
            logger.info(
                "RX notify #%d: %d bytes from sender=%s; "
                "first 64 bytes (hex)=%s%s",
                cnt + 1,
                len(data),
                _sender,
                preview,
                "..." if len(data) > 64 else "",
            )

        ble._rx_buffer.extend(data)

        if len(ble._rx_buffer) > MAX_BUFFER_SIZE:
            logger.warning("RX buffer overflow (%d bytes), clearing", len(ble._rx_buffer))
            ble._rx_buffer.clear()
            return

        while self._try_parse_packet(ble):
            pass

    async def after_subscribe(
        self, client: BleakClient, write_uuid: str, write_resp: bool,
    ) -> None:
        """Send the ASCII handshake to start data flow."""
        logger.info(
            "Sending handshake (%d bytes) to %s response=%s",
            len(HANDSHAKE_PAYLOAD),
            write_uuid,
            write_resp,
        )
        try:
            await client.write_gatt_char(
                write_uuid, HANDSHAKE_PAYLOAD, response=write_resp,
            )
        except Exception:
            logger.exception(
                "Handshake write failed: uuid=%s response=%s len=%d",
                write_uuid, write_resp, len(HANDSHAKE_PAYLOAD),
            )
            raise
        logger.info(
            "Handshake completed; waiting for framed packets "
            "(magic 0x%08X)...",
            PACKET_IDENTIFIER,
        )

    # ── Packet parsing ──────────────────────────────────────────────────

    def _try_parse_packet(self, ble: PowerWatchdogBLE) -> bool:
        """Extract and dispatch one complete packet from the RX buffer.

        Returns True if a packet was consumed, False if more data is needed.
        """
        buf = ble._rx_buffer

        while len(buf) >= 4:
            ident = struct.unpack_from(">I", buf, 0)[0]
            if ident == PACKET_IDENTIFIER:
                break
            del buf[0]

        if len(buf) < HEADER_SIZE:
            return False

        cmd = buf[6]
        data_len = struct.unpack_from(">H", buf, 7)[0]

        if data_len > MAX_BUFFER_SIZE:
            logger.warning("Invalid dataLen %d, discarding", data_len)
            del buf[:4]
            return True

        total_len = HEADER_SIZE + data_len + TAIL_SIZE

        if len(buf) < total_len:
            return False

        body = bytes(buf[HEADER_SIZE : HEADER_SIZE + data_len])
        tail = struct.unpack_from(">H", buf, HEADER_SIZE + data_len)[0]
        raw_hex = buf[:total_len].hex()
        del buf[:total_len]

        if tail != PACKET_TAIL:
            if not getattr(ble, "_logged_bad_tail", False):
                ble._logged_bad_tail = True
                hx = raw_hex[:200] + ("..." if len(raw_hex) > 200 else "")
                logger.warning(
                    "Bad packet tail 0x%04X (expected 0x%04X); "
                    "cmd=%d data_len=%d frame_hex_prefix=%s",
                    tail, PACKET_TAIL, cmd, data_len, hx,
                )
            else:
                logger.debug(
                    "Bad packet tail 0x%04X (expected 0x%04X)",
                    tail, PACKET_TAIL,
                )
            return True

        wd = getattr(ble, "_watchdog", None)
        if wd is not None:
            wd.notify_activity()

        if not getattr(ble, "_logged_first_valid_frame", False):
            ble._logged_first_valid_frame = True
            logger.info(
                "First valid framed packet: cmd=%d body_len=%d (magic+tail OK)",
                cmd, len(body),
            )

        if cmd == CMD_DL_REPORT:
            _parse_dl_report(ble, body, raw_hex)
        elif cmd == CMD_ERROR_REPORT:
            logger.debug("ErrorReport received (%d bytes body)", len(body))
        elif cmd == CMD_ALARM:
            logger.warning("Alarm notification received from Power Watchdog")
        else:
            logger.debug("Unknown cmd %d (%d bytes body)", cmd, len(body))

        return True


# ── DLReport parsing ────────────────────────────────────────────────────────

def _parse_dl_report(ble: PowerWatchdogBLE, body: bytes, raw_hex: str) -> None:
    """Parse a DLReport body (34 bytes = single line, 68 bytes = dual line)."""
    if len(body) == DL_DATA_SIZE:
        l1 = parse_dl_data(body, 0)
        with ble._data_lock:
            ble._data.l1 = l1
            ble._data.has_l2 = False
            ble._data.timestamp = time.time()
            ble._data.raw_hex = raw_hex
        logger.debug(
            "L1: %.1fV %.2fA %.1fW %.3fkWh %.1fHz err=%d",
            l1.voltage, l1.current, l1.power,
            l1.energy, l1.frequency, l1.error_code,
        )

    elif len(body) == DL_DATA_SIZE * 2:
        l1 = parse_dl_data(body, 0)
        l2 = parse_dl_data(body, DL_DATA_SIZE)
        with ble._data_lock:
            ble._data.l1 = l1
            ble._data.l2 = l2
            ble._data.has_l2 = True
            ble._data.timestamp = time.time()
            ble._data.raw_hex = raw_hex
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


def parse_dl_data(body: bytes, offset: int) -> LineData:
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
    output_v_raw = struct.unpack_from(">i", body, o + 20)[0]
    boost = body[o + 26] == 1
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
