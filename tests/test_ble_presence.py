"""Tests for the presence gate in PowerWatchdogBLE (router mode).

The gate decides whether the session loop goes looking for the unit or
waits to be told it is back.  Getting it wrong in one direction means a
scan storm; in the other, a unit that never reconnects.  The instances
here are built without starting the daemon thread.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

import power_watchdog_ble as pwb
from power_watchdog_ble import PowerWatchdogBLE, PRESENCE_FALLBACK_INTERVAL


def _ble(gated: bool = True, offline: bool = True, fallback: float = PRESENCE_FALLBACK_INTERVAL):
    """A PowerWatchdogBLE with only the presence state, no thread."""
    ble = object.__new__(PowerWatchdogBLE)
    ble.address = "24:EC:4A:E4:69:A5"
    ble._ble_adapters = None
    ble._force_scan = False
    ble._running = True
    ble._loop = None
    ble._sleep_task = None
    ble._presence_gated = gated
    ble._presence_fallback_interval = fallback
    ble._last_seen = 0.0
    ble._seen_adapter = None
    ble._last_resolve_attempt = 0.0
    ble._offline = offline
    ble._waiting_for_presence = False
    return ble


class _FakeLoop:
    def __init__(self, closed: bool = False):
        self.calls: list = []
        self._closed = closed

    def call_soon_threadsafe(self, fn, *args):
        self.calls.append(fn)

    def is_closed(self):
        return self._closed


class TestConstants:
    def test_blind_retry_never_shorter_than_offline_poll(self):
        # The gate exists to poll LESS than the offline path does.
        assert PRESENCE_FALLBACK_INTERVAL >= PowerWatchdogBLE.OFFLINE_POLL_INTERVAL


class TestShouldWaitForPresence:
    def test_not_gated_never_waits(self):
        ble = _ble(gated=False)
        ble._last_resolve_attempt = time.monotonic()
        assert ble._should_wait_for_presence() is False

    def test_never_waits_while_the_unit_was_last_found(self):
        # Startup and the reconnect after a drop: go and look.
        ble = _ble(offline=False)
        ble._last_resolve_attempt = time.monotonic()
        assert ble._should_wait_for_presence() is False

    def test_waits_while_offline_and_unheard(self):
        ble = _ble()
        ble._last_resolve_attempt = time.monotonic()
        assert ble._should_wait_for_presence() is True

    def test_a_presence_signal_ends_the_wait(self):
        ble = _ble()
        ble._last_resolve_attempt = time.monotonic()
        ble.notify_present("684E054477B0")
        assert ble._presence_fresh() is True
        assert ble._should_wait_for_presence() is False

    def test_one_signal_buys_one_attempt(self):
        # The stamp is consumed when the next resolve starts, so a scan
        # that fails anyway does not spin on the same signal.
        ble = _ble()
        ble.notify_present()
        ble._last_resolve_attempt = time.monotonic()
        assert ble._presence_fresh() is False
        assert ble._should_wait_for_presence() is True

    def test_blind_retry_when_the_fallback_lapses(self):
        ble = _ble(fallback=100.0)
        ble._last_resolve_attempt = time.monotonic() - 101.0
        assert ble._blind_retry_due() is True
        assert ble._should_wait_for_presence() is False


class TestNotifyPresent:
    def test_records_adapter_verbatim(self):
        ble = _ble()
        ble.notify_present("684E054477B0")
        assert ble._seen_adapter == "684E054477B0"
        ble.notify_present("")
        assert ble._seen_adapter is None

    def test_wakes_only_a_presence_wait(self):
        ble = _ble()
        ble._loop = _FakeLoop()
        ble.notify_present()
        assert ble._loop.calls == []          # a backoff sleep is left alone
        ble._waiting_for_presence = True
        ble.notify_present()
        assert ble._loop.calls == [ble._cancel_sleep]

    def test_no_loop_is_fine(self):
        ble = _ble()
        ble._waiting_for_presence = True
        ble.notify_present()                  # thread not started yet

    def test_closed_loop_is_left_alone(self):
        ble = _ble()
        ble._loop = _FakeLoop(closed=True)
        ble._waiting_for_presence = True
        ble.notify_present()
        assert ble._loop.calls == []


class TestSetPresenceGated:
    def test_toggle_and_wake(self):
        ble = _ble(gated=True)
        ble._loop = _FakeLoop()
        ble._waiting_for_presence = True
        ble.set_presence_gated(False)         # router went away mid-wait
        assert ble.presence_gated is False
        assert ble._loop.calls == [ble._cancel_sleep]

    def test_same_value_is_a_no_op(self):
        ble = _ble(gated=True)
        ble._loop = _FakeLoop()
        ble._waiting_for_presence = True
        ble.set_presence_gated(True)
        assert ble._loop.calls == []


class TestPresenceWaitSlice:
    def test_slice_is_bounded_by_the_offline_poll(self, monkeypatch):
        # connect_cycles must keep advancing at the offline-poll rate or the
        # daemon's liveness backstop would read a long wait as a wedge.
        monkeypatch.setattr(PowerWatchdogBLE, "OFFLINE_POLL_INTERVAL", 0.02)
        ble = _ble(fallback=10_000.0)
        ble._last_resolve_attempt = time.monotonic()
        started = time.monotonic()
        asyncio.run(ble._presence_wait_slice())
        assert time.monotonic() - started < 1.0
        assert ble._waiting_for_presence is False

    def test_slice_ends_at_the_fallback(self):
        ble = _ble(fallback=0.02)
        ble._last_resolve_attempt = time.monotonic()
        started = time.monotonic()
        asyncio.run(ble._presence_wait_slice())
        assert time.monotonic() - started < 1.0

    def test_overdue_fallback_does_not_sleep_negative(self):
        ble = _ble(fallback=1.0)
        ble._last_resolve_attempt = time.monotonic() - 50.0
        asyncio.run(ble._presence_wait_slice())  # max(0, negative) -> returns


class TestResolveDeviceAdapterHint:
    def _run(self, ble, hint):
        return asyncio.run(ble._resolve_device(hint))

    def test_hint_fills_in_when_config_names_no_adapter(self):
        ble = _ble()
        seen = {}

        async def from_cache(adapter):
            seen["adapter"] = adapter
            return "device"

        ble._device_from_cache = from_cache
        with patch.object(pwb, "scan_adapter_for", return_value=None), \
             patch.object(pwb, "resolve_adapter", return_value="hci5") as ra:
            assert self._run(ble, "684E054477B0") == "device"
        ra.assert_called_once_with("684E054477B0")
        assert seen["adapter"] == "hci5"

    def test_configured_adapter_wins_over_the_hint(self):
        ble = _ble()
        seen = {}

        async def from_cache(adapter):
            seen["adapter"] = adapter
            return "device"

        ble._device_from_cache = from_cache
        with patch.object(pwb, "scan_adapter_for", return_value="hci1"), \
             patch.object(pwb, "resolve_adapter") as ra:
            assert self._run(ble, "684E054477B0") == "device"
        ra.assert_not_called()
        assert seen["adapter"] == "hci1"

    def test_no_hint_no_config_lets_bleak_choose(self):
        ble = _ble()
        seen = {}

        async def from_cache(adapter):
            seen["adapter"] = adapter
            return "device"

        ble._device_from_cache = from_cache
        with patch.object(pwb, "scan_adapter_for", return_value=None), \
             patch.object(pwb, "resolve_adapter") as ra:
            assert self._run(ble, None) == "device"
        ra.assert_not_called()
        assert seen["adapter"] is None
