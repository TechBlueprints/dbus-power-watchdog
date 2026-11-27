# Copyright 2025 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for grid_publisher.GridPublisher.

Use a minimal in-memory fake of VeDbusService that mirrors the
context-manager + ServiceContext + dedup-on-no-change protocol the
publisher relies on.  No actual D-Bus required.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import grid_publisher as gp


# ── In-memory fake of vedbus's VeDbusService + ServiceContext ──────────────


class _FakeServiceContext:
    """Mimics vedbus.ServiceContext for tests.

    On exit, records the changes dict that vedbus would have emitted
    via ItemsChanged so tests can assert on it.
    """

    def __init__(self, parent: "FakeVeDbusService") -> None:
        self.parent = parent
        self.changes: dict[str, object] = {}

    def __contains__(self, path: str) -> bool:
        return path in self.parent._values

    def __setitem__(self, path: str, value) -> None:
        # Mirror vedbus: only record a change if the value actually
        # differs from what the service already holds.  Tests rely on
        # this to verify that the publisher's local cache reduces work
        # *before* it reaches vedbus.
        if self.parent._values.get(path) == value:
            return
        self.parent._values[path] = value
        self.changes[path] = value


class FakeVeDbusService:
    """Minimal vedbus.VeDbusService fake.

    Tracks declared paths, current values, the last batch of changes
    emitted (mirrors vedbus's ItemsChanged), and a count of
    __setitem__ calls dispatched into the service (so we can assert
    that the publisher's local cache short-circuits no-op writes
    *before* reaching us).
    """

    def __init__(self) -> None:
        self._values: dict[str, object] = {}
        self.last_changes: dict[str, object] = {}
        self.emit_count: int = 0
        self.setitem_calls: int = 0
        self._ctx_stack: list[_FakeServiceContext] = []

    def add_path(self, path: str, initial=None) -> None:
        self._values[path] = initial

    def __contains__(self, path: str) -> bool:
        return path in self._values

    def __getitem__(self, path: str):
        return self._values[path]

    # The publisher calls ``with svc as ctx:`` then ``ctx[path] = value``.
    # We count setitem dispatch on the context, not the service, so
    # tests can prove the cache short-circuits at the publisher layer.
    class _CountingContext:
        def __init__(self, parent_ctx: _FakeServiceContext, owner: "FakeVeDbusService") -> None:
            self._inner = parent_ctx
            self._owner = owner

        def __contains__(self, path: str) -> bool:
            return path in self._inner

        def __setitem__(self, path: str, value) -> None:
            self._owner.setitem_calls += 1
            self._inner[path] = value

    def __enter__(self) -> "FakeVeDbusService._CountingContext":
        inner = _FakeServiceContext(self)
        self._ctx_stack.append(inner)
        return FakeVeDbusService._CountingContext(inner, self)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        ctx = self._ctx_stack.pop()
        if ctx.changes:
            self.last_changes = dict(ctx.changes)
            self.emit_count += 1
        # Don't suppress exceptions
        return None


# ── WatchdogData fixture ───────────────────────────────────────────────────


@dataclass
class _Line:
    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0
    energy: float = 0.0
    frequency: float = 0.0
    error_code: int = 0


@dataclass
class _Snapshot:
    l1: _Line = field(default_factory=_Line)
    l2: _Line = field(default_factory=_Line)
    has_l2: bool = False
    timestamp: float = 1.0


def _full_grid_paths(svc: FakeVeDbusService, *, with_error_message: bool = True) -> None:
    """Declare all the paths the publisher might write to."""
    for p in (
        "/Connected", "/NrOfPhases", "/UpdateIndex", "/ErrorCode",
        "/Ac/Power", "/Ac/Current", "/Ac/Voltage", "/Ac/Frequency",
        "/Ac/Energy/Forward",
        "/Ac/L1/Voltage", "/Ac/L1/Current", "/Ac/L1/Power",
        "/Ac/L1/Energy/Forward", "/Ac/L1/Frequency",
        "/Ac/L2/Voltage", "/Ac/L2/Current", "/Ac/L2/Power",
        "/Ac/L2/Energy/Forward", "/Ac/L2/Frequency",
    ):
        svc.add_path(p)
    if with_error_message:
        svc.add_path("/ErrorMessage")
    for alarm_path in gp.GRID_ALARM_PATHS:
        svc.add_path(alarm_path, 0)


# ── Round-to helper (delegates to existing TestRoundTo in test_service.py
#    coverage; here we just sanity-check that the publisher uses it) ───────


class TestRoundTo:
    """Direct coverage for grid_publisher._round_to.

    Keep colocated with the helper definition so any refactor of the
    rounding semantics fails tests in the same file.
    """

    def test_voltage_half_volt_step_collapses_flicker(self):
        # The motivating noise pattern: two readings 0.1 V apart that
        # round to the same 0.5 V bucket.
        assert gp._round_to(119.4, 0.5) == 119.5
        assert gp._round_to(119.5, 0.5) == 119.5

    def test_current_step(self):
        assert gp._round_to(0.48, 0.05) == 0.5
        assert gp._round_to(0.52, 0.05) == 0.5

    def test_power_step(self):
        assert gp._round_to(9, 5) == 10
        assert gp._round_to(12, 5) == 10
        assert gp._round_to(13, 5) == 15

    def test_step_zero_passes_value_through(self):
        assert gp._round_to(123.456, 0) == 123.456

    def test_step_negative_passes_value_through(self):
        assert gp._round_to(123.456, -1) == 123.456

    def test_zero_value(self):
        assert gp._round_to(0.0, 0.5) == 0.0


# ── publish() — happy path: first cycle writes everything ──────────────────


class TestFirstCycle:
    def test_first_cycle_writes_all_declared_paths_and_emits_one_ic(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        snap = _Snapshot(
            l1=_Line(voltage=120.0, current=10.0, power=1200.0,
                     energy=5.0, frequency=60.0),
            has_l2=False,
        )

        pub = gp.GridPublisher()
        any_changed = pub.publish(svc, snap, connected=True)

        assert any_changed is True
        # exactly one ItemsChanged emit at __exit__
        assert svc.emit_count == 1
        # Every relevant path landed
        assert svc._values["/Connected"] == 1
        assert svc._values["/Ac/L1/Voltage"] == 120.0
        assert svc._values["/Ac/L1/Power"] == 1200.0  # rounded to 5W; already on bucket
        assert svc._values["/UpdateIndex"] == 1
        # The L2 paths declared on the service stay at their initial
        # value because has_l2 is False
        assert svc._values["/Ac/L2/Voltage"] is None

    def test_first_cycle_l2_writes_l2_paths(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        snap = _Snapshot(
            l1=_Line(voltage=120.0, current=10.0, power=1200.0,
                     energy=5.0, frequency=60.0),
            l2=_Line(voltage=121.0, current=8.0, power=968.0,
                     energy=3.0, frequency=60.0),
            has_l2=True,
        )

        pub = gp.GridPublisher()
        pub.publish(svc, snap, connected=True)

        assert svc._values["/NrOfPhases"] == 2
        assert svc._values["/Ac/L2/Voltage"] == 121.0
        # Aggregate paths use rounded sums/averages
        assert svc._values["/Ac/Power"] == gp._round_to(2168.0, gp.GRID_POWER_STEP)
        assert svc._values["/Ac/Voltage"] == gp._round_to((120.0 + 121.0) / 2.0,
                                                          gp.GRID_VOLTAGE_STEP)


# ── Noise flicker inside one step / one band does not emit ────────────────


class TestRoundingSuppressesNoise:
    def test_voltage_flicker_within_one_step_does_not_emit(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()

        # Cycle 1: establish baseline
        snap1 = _Snapshot(
            l1=_Line(voltage=119.4, current=10.0, power=1200.0,
                     energy=5.0, frequency=60.0))
        pub.publish(svc, snap1, connected=True)

        emits_after_first = svc.emit_count
        assert emits_after_first == 1

        # Cycle 2: voltage flickers 119.4 → 119.5 (within 0.5V step,
        # both round to 119.5).  Nothing else changes.
        snap2 = _Snapshot(
            l1=_Line(voltage=119.5, current=10.0, power=1200.0,
                     energy=5.0, frequency=60.0))
        any_changed = pub.publish(svc, snap2, connected=True)

        assert any_changed is False
        # No new IC fired — empty changes dict at __exit__
        assert svc.emit_count == emits_after_first
        # /UpdateIndex must NOT advance when nothing else changed
        assert svc._values["/UpdateIndex"] == 1

    def test_voltage_jump_of_one_band_does_emit(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()

        snap1 = _Snapshot(l1=_Line(voltage=119.4, current=10.0, power=1200.0))
        pub.publish(svc, snap1, connected=True)

        # A move of one full hysteresis band publishes, and lands in a
        # different display bucket.
        v2 = 119.4 + gp.GRID_VOLTAGE_BAND
        snap2 = _Snapshot(l1=_Line(voltage=v2, current=10.0, power=1200.0))
        any_changed = pub.publish(svc, snap2, connected=True)

        assert any_changed is True
        assert svc.emit_count == 2
        assert svc._values["/Ac/L1/Voltage"] == gp._round_to(v2, gp.GRID_VOLTAGE_STEP)
        assert svc._values["/Ac/L1/Voltage"] != gp._round_to(119.4, gp.GRID_VOLTAGE_STEP)
        assert svc._values["/UpdateIndex"] == 2  # bumped


# ── Local cache short-circuits svc.__setitem__ on no-op writes ─────────────


class TestLocalCache:
    def test_steady_cycles_dispatch_no_setitem_calls_after_first(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        snap = _Snapshot(
            l1=_Line(voltage=120.0, current=10.0, power=1200.0,
                     energy=5.0, frequency=60.0))

        # Cycle 1: full write — many setitem calls
        pub.publish(svc, snap, connected=True)
        first_calls = svc.setitem_calls
        assert first_calls > 0

        # Cycles 2-10 with identical data: cache should kick in,
        # no further setitem dispatch
        for _ in range(9):
            pub.publish(svc, snap, connected=True)

        assert svc.setitem_calls == first_calls, (
            "publisher did %d extra setitem calls on no-op cycles; "
            "local cache failed to short-circuit"
            % (svc.setitem_calls - first_calls)
        )
        # And no further IC emits
        assert svc.emit_count == 1


# ── /UpdateIndex gating ────────────────────────────────────────────────────


class TestUpdateIndexGating:
    def test_update_index_does_not_advance_on_steady_data(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0, power=1200.0))

        pub.publish(svc, snap, connected=True)
        assert svc._values["/UpdateIndex"] == 1

        for _ in range(20):
            pub.publish(svc, snap, connected=True)

        assert svc._values["/UpdateIndex"] == 1

    def test_update_index_wraps_at_256(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()

        # Force 257 distinct cycles by varying current each time.  Use
        # increments of one hysteresis band so the gate publishes every
        # one of them, each into a different display bucket.
        for i in range(257):
            snap = _Snapshot(l1=_Line(
                voltage=120.0,
                current=i * max(gp.GRID_CURRENT_BAND, gp.GRID_CURRENT_STEP * 2),
                power=1200.0,
            ))
            pub.publish(svc, snap, connected=True)

        # 257 successful bumps mod 256 = 1
        assert svc._values["/UpdateIndex"] == 1


# ── Connected toggling ─────────────────────────────────────────────────────


class TestConnectedToggle:
    def test_connect_change_alone_emits_one_ic(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0, power=1200.0))

        pub.publish(svc, snap, connected=True)
        e1 = svc.emit_count

        # Same data but disconnect — /Connected flips, IC should fire
        any_changed = pub.publish(svc, snap, connected=False)

        assert any_changed is True
        assert svc.emit_count == e1 + 1
        assert svc._values["/Connected"] == 0

    def test_disconnect_nulls_live_ac_paths(self):
        """The Hughes Power Watchdog is shore-power-powered — when shore
        is unplugged the device powers down and stops advertising.
        Before this behaviour, the publisher kept re-emitting the
        last-known ``/Ac/L1/Power`` etc., which dbus-systemcalc-py
        happily summed into the system-wide "AC Loads" reading.  The
        GUI then showed phantom ~1.5 kW of consumption with nothing
        actually plugged in.  Verify the live paths now go to None on
        disconnect so systemcalc excludes them."""
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        # First publish a real snapshot while connected — values land.
        snap = _Snapshot(
            l1=_Line(voltage=120.0, current=12.5, power=1500.0,
                     energy=2.5, frequency=60.0),
            l2=_Line(voltage=123.0, current=0.5, power=75.0,
                     energy=0.1, frequency=60.0),
            has_l2=True,
        )
        pub.publish(svc, snap, connected=True)
        assert svc._values["/Ac/L1/Power"] == 1500
        assert svc._values["/Ac/Power"] == 1575

        # Now disconnect with the same stale snapshot.
        pub.publish(svc, snap, connected=False)

        # Every live-measurement path is None — systemcalc will skip them.
        for path in (
            "/Ac/L1/Voltage", "/Ac/L1/Current", "/Ac/L1/Power",
            "/Ac/L1/Frequency",
            "/Ac/L2/Voltage", "/Ac/L2/Current", "/Ac/L2/Power",
            "/Ac/L2/Frequency",
            "/Ac/Power", "/Ac/Current", "/Ac/Voltage", "/Ac/Frequency",
        ):
            assert svc._values[path] is None, (
                f"{path} should be None when disconnected, got {svc._values[path]!r}")
        assert svc._values["/Connected"] == 0

    def test_disconnect_preserves_cumulative_energy(self):
        """``/Ac/{L1,L2,}/Energy/Forward`` are persistent counters of
        total energy delivered, not live readings.  Don't blow them
        away on a transient BLE outage — they represent state that
        survives the disconnect and the GUI's energy-history panels
        depend on a monotonically increasing value."""
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        snap = _Snapshot(
            l1=_Line(voltage=120.0, current=10.0, power=1200.0,
                     energy=42.0, frequency=60.0),
            l2=_Line(voltage=123.0, current=5.0, power=600.0,
                     energy=15.0, frequency=60.0),
            has_l2=True,
        )
        pub.publish(svc, snap, connected=True)
        assert svc._values["/Ac/L1/Energy/Forward"] == pytest.approx(42.0, abs=1)
        assert svc._values["/Ac/Energy/Forward"] == pytest.approx(57.0, abs=1)

        pub.publish(svc, snap, connected=False)
        # Energy counters keep their last-known values across the
        # disconnect rather than being nulled.
        assert svc._values["/Ac/L1/Energy/Forward"] == pytest.approx(42.0, abs=1)
        assert svc._values["/Ac/L2/Energy/Forward"] == pytest.approx(15.0, abs=1)
        assert svc._values["/Ac/Energy/Forward"] == pytest.approx(57.0, abs=1)

    def test_disconnect_repeats_are_deduped(self):
        """Once the live paths have been nulled, subsequent disconnected
        publishes should be silent — the local cache notices the
        target values match what's already on the bus and skips the
        write.  Avoids spurious ItemsChanged emits while the device
        remains offline."""
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        snap = _Snapshot(
            l1=_Line(voltage=120.0, current=10.0, power=1200.0),
        )
        pub.publish(svc, snap, connected=True)
        pub.publish(svc, snap, connected=False)
        e1 = svc.emit_count

        # Second disconnected publish — nothing changes, no emit.
        any_changed = pub.publish(svc, snap, connected=False)
        assert any_changed is False
        assert svc.emit_count == e1


# ── reset() clears state ───────────────────────────────────────────────────


class TestReset:
    def test_reset_makes_next_publish_full_again(self):
        svc1 = FakeVeDbusService()
        _full_grid_paths(svc1)
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0, power=1200.0))

        pub.publish(svc1, snap, connected=True)
        assert svc1._values["/UpdateIndex"] == 1

        # Simulate a service teardown + recreation (e.g. role change)
        pub.reset()
        svc2 = FakeVeDbusService()
        _full_grid_paths(svc2)

        pub.publish(svc2, snap, connected=True)

        assert svc2._values["/UpdateIndex"] == 1  # restarted from 0
        # Every path got written to the fresh service (cache empty)
        assert svc2._values["/Ac/L1/Voltage"] == 120.0


# ── /ErrorMessage path is optional (legacy callers don't declare it) ───────


class TestOptionalErrorMessagePath:
    def test_legacy_service_without_error_message_path_does_not_crash(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc, with_error_message=False)
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0,
                                  power=1200.0, error_code=5))

        # Must not raise even though /ErrorMessage isn't declared
        pub.publish(svc, snap, connected=True)

        # /ErrorCode is declared, so it does land
        assert svc._values["/ErrorCode"] == 5
        # /ErrorMessage is NOT declared, so the cache shouldn't pretend
        # it wrote there either
        assert "/ErrorMessage" not in svc._values

    def test_modern_service_with_error_message_writes_it(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc, with_error_message=True)
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0,
                                  power=1200.0, error_code=7))

        pub.publish(svc, snap, connected=True)

        assert svc._values["/ErrorMessage"] == "Missing Ground"


# ── No data yet (timestamp == 0) ───────────────────────────────────────────


class TestAlarms:
    def test_undervoltage_trip_sets_low_voltage_alarm(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        # Device tripped on L1 with input voltage below 104 V.
        snap = _Snapshot(l1=_Line(voltage=98.0, current=0.0, power=0.0,
                                  error_code=1))
        pub.publish(svc, snap, connected=True)
        assert svc._values["/Alarms/LowVoltage"] == 2
        assert svc._values["/Alarms/HighVoltage"] == 0

    def test_overvoltage_trip_sets_high_voltage_alarm(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        # Device tripped on L1 with input voltage above 132 V.
        snap = _Snapshot(l1=_Line(voltage=140.0, current=0.0, power=0.0,
                                  error_code=1))
        pub.publish(svc, snap, connected=True)
        assert svc._values["/Alarms/HighVoltage"] == 2
        assert svc._values["/Alarms/LowVoltage"] == 0

    def test_l2_voltage_trip_uses_l2_input_voltage(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        # L1 happy at 120 V, L2 tripped low at 100 V — direction must
        # be picked from the faulting leg, not L1.
        snap = _Snapshot(
            l1=_Line(voltage=120.0, current=10.0, power=1200.0, error_code=0),
            l2=_Line(voltage=100.0, current=0.0, power=0.0, error_code=2),
            has_l2=True,
        )
        pub.publish(svc, snap, connected=True)
        assert svc._values["/ErrorCode"] == 2
        assert svc._values["/Alarms/LowVoltage"] == 2

    def test_overcurrent_sets_overload_alarm(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=35.0, power=4200.0,
                                  error_code=3))
        pub.publish(svc, snap, connected=True)
        assert svc._values["/Alarms/Overload"] == 2

    def test_alarm_clears_when_error_returns_to_zero(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        # Trip
        pub.publish(svc, _Snapshot(l1=_Line(voltage=98.0, error_code=1)),
                    connected=True)
        assert svc._values["/Alarms/LowVoltage"] == 2
        # Recover
        pub.publish(svc, _Snapshot(l1=_Line(voltage=120.0, current=10.0,
                                            power=1200.0, error_code=0)),
                    connected=True)
        assert svc._values["/Alarms/LowVoltage"] == 0
        assert svc._values["/ErrorCode"] == 0

    def test_alarms_persist_across_disconnect(self):
        # Matches /ErrorCode behaviour: a known-tripped state survives
        # a BLE drop so the user keeps seeing the alarm until either
        # the device recovers or they take action.
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        pub.publish(svc, _Snapshot(l1=_Line(voltage=98.0, error_code=1)),
                    connected=True)
        assert svc._values["/Alarms/LowVoltage"] == 2
        pub.publish(svc, _Snapshot(timestamp=0.0), connected=False)
        assert svc._values["/Alarms/LowVoltage"] == 2

    def test_unrelated_error_codes_leave_alarms_at_zero(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        # Surge-protection-used-up (9) — no /Alarms slot in the GUI
        # spec; surfaces via /ErrorCode only.
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0, power=1200.0,
                                  error_code=9))
        pub.publish(svc, snap, connected=True)
        assert svc._values["/ErrorCode"] == 9
        for alarm_path in gp.GRID_ALARM_PATHS:
            assert svc._values[alarm_path] == 0


class TestPreData:
    def test_only_connected_writes_when_no_data(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()

        # timestamp == 0 means BLE hasn't delivered the first frame
        snap = _Snapshot(timestamp=0.0)
        pub.publish(svc, snap, connected=True)

        # /Connected went through, but no Ac/* paths got values
        assert svc._values["/Connected"] == 1
        assert svc._values["/Ac/L1/Voltage"] is None
        # /UpdateIndex stays at 0 (gated inside the data branch)
        assert svc._values["/UpdateIndex"] is None or svc._values["/UpdateIndex"] == 0


# ── /LastUpdate: the freshness flag that cannot lie ───────────────────────
#
# /Connected reads 1 while a link carries nothing, and /UpdateIndex is gated
# behind the hysteresis gate, so a flat load holds it still even
# with fresh frames.  /LastUpdate is written from data.timestamp — set only
# when a frame actually PARSED — at most once per LAST_UPDATE_INTERVAL.


def _lastupdate_svc() -> FakeVeDbusService:
    svc = FakeVeDbusService()
    _full_grid_paths(svc)
    svc.add_path("/LastUpdate")
    return svc


class TestLastUpdate:
    def test_first_parsed_frame_writes_it(self):
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        pub.publish(svc, _Snapshot(timestamp=1000.7), connected=True)
        assert svc._values["/LastUpdate"] == 1000

    def test_not_rewritten_within_the_interval(self):
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0, power=1200.0))
        snap.timestamp = 1000.0
        pub.publish(svc, snap, connected=True)
        idx = svc._values["/UpdateIndex"]
        for t in range(1, int(gp.LAST_UPDATE_INTERVAL)):
            snap.timestamp = 1000.0 + t
            pub.publish(svc, snap, connected=True)
        assert svc._values["/LastUpdate"] == 1000
        # Flat data + throttled stamp = no D-Bus churn at all.
        assert svc._values["/UpdateIndex"] == idx

    def test_rewritten_after_the_interval_and_bumps_update_index(self):
        # A flat load must still prove liveness at least once a minute.
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        snap = _Snapshot(l1=_Line(voltage=120.0, current=10.0, power=1200.0))
        snap.timestamp = 1000.0
        pub.publish(svc, snap, connected=True)
        idx = svc._values["/UpdateIndex"]
        snap.timestamp = 1000.0 + gp.LAST_UPDATE_INTERVAL
        pub.publish(svc, snap, connected=True)
        assert svc._values["/LastUpdate"] == 1000 + int(gp.LAST_UPDATE_INTERVAL)
        assert svc._values["/UpdateIndex"] == idx + 1

    def test_dead_parser_never_advances_it(self):
        # The 2026-08-26 shape: connected, link alive, zero frames parsed.
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        for _ in range(100):
            pub.publish(svc, _Snapshot(timestamp=0.0), connected=True)
        assert svc._values["/LastUpdate"] is None

    def test_not_written_while_disconnected(self):
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        pub.publish(svc, _Snapshot(timestamp=1000.0), connected=False)
        assert svc._values["/LastUpdate"] is None

    def test_reset_allows_an_immediate_rewrite(self):
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        pub.publish(svc, _Snapshot(timestamp=1000.0), connected=True)
        pub.reset()
        svc._values["/LastUpdate"] = None
        pub.publish(svc, _Snapshot(timestamp=1001.0), connected=True)
        assert svc._values["/LastUpdate"] == 1001

    def test_undeclared_path_is_skipped(self):
        # The standalone CLI may not declare it; must not raise.
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()
        pub.publish(svc, _Snapshot(timestamp=1000.0), connected=True)
        assert "/LastUpdate" not in svc._values


# ── Hysteresis gate on the live measurements ──────────────────────────────
#
# The twelve live measurement paths (V / I / P / Frequency on each leg and
# the totals) publish as one frame, and only when something triggers it:
# a reading that moved a full band from the reading at the last published
# frame, a displayed zero/non-zero transition, a path with no reference
# yet, or any ungated path (/Connected, /LastUpdate, energy, /NrOfPhases,
# /ErrorCode, /ErrorMessage, /Alarms/*) actually being written.  Tests are
# written against the band and step constants, not their numbers, so the
# bands can be retuned from a measurement without touching this file.

_LIVE_PATHS = (
    "/Ac/L1/Voltage", "/Ac/L1/Current", "/Ac/L1/Power", "/Ac/L1/Frequency",
    "/Ac/Voltage", "/Ac/Current", "/Ac/Power", "/Ac/Frequency",
)

_BASE_V = 120.0
_BASE_I = 10.0
_BASE_P = 1200.0
_BASE_F = 60.0
_BASE_E = 5.0


def _frame(t: float = 1000.0, **kw) -> _Snapshot:
    """One L1 frame at a steady base load, with fields overridden by *kw*."""
    line = dict(voltage=_BASE_V, current=_BASE_I, power=_BASE_P,
                energy=_BASE_E, frequency=_BASE_F, error_code=0)
    line.update(kw)
    return _Snapshot(l1=_Line(**line), timestamp=t)


def _primed(*frames_kw) -> tuple[FakeVeDbusService, gp.GridPublisher]:
    """A service with /LastUpdate declared and one base frame published."""
    svc = _lastupdate_svc()
    pub = gp.GridPublisher()
    pub.publish(svc, _frame(), connected=True)
    assert svc.emit_count == 1
    return svc, pub


def _sub_band(band: float, step: float) -> float:
    """A move under one band that still changes the displayed value.

    Without a display change a suppressed move would be indistinguishable
    from one that was written and deduped, so the precondition is checked.
    """
    move = band * 0.9
    assert move >= step, "band %r too close to step %r for this test" % (band, step)
    return move


class TestHysteresisFlapping:
    def test_power_flapping_across_a_step_boundary_publishes_once(self):
        # e.g. 1202 / 1203 W around the 1202.5 W boundary of a 5 W step.
        step, band = gp.GRID_POWER_STEP, gp.GRID_POWER_BAND
        boundary = _BASE_P + step / 2.0
        delta = min(step, band) / 5.0
        svc, pub = _primed()
        start = svc.emit_count
        for i in range(30):
            p = boundary - delta if i % 2 == 0 else boundary + delta
            pub.publish(svc, _frame(t=1001.0 + i, power=p), connected=True)
        # The first flapping frame crosses no band either (it is within
        # step/2 + delta of the base), so the whole run is silent.
        assert svc.emit_count == start

    def test_voltage_flapping_across_a_step_boundary_publishes_once(self):
        # e.g. 119.74 / 119.76 V around the 119.75 V boundary of a 0.5 V step.
        step, band = gp.GRID_VOLTAGE_STEP, gp.GRID_VOLTAGE_BAND
        boundary = 119.5 + step / 2.0
        delta = min(step, band) / 25.0
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        for i in range(30):
            v = boundary - delta if i % 2 == 0 else boundary + delta
            pub.publish(svc, _frame(t=1000.0 + i, voltage=v), connected=True)
        assert svc.emit_count == 1


class TestHysteresisBand:
    @pytest.mark.parametrize("field_name,path,band", [
        ("power", "/Ac/L1/Power", gp.GRID_POWER_BAND),
        ("current", "/Ac/L1/Current", gp.GRID_CURRENT_BAND),
        ("voltage", "/Ac/L1/Voltage", gp.GRID_VOLTAGE_BAND),
        ("frequency", "/Ac/L1/Frequency", gp.GRID_FREQ_BAND),
    ])
    def test_move_of_exactly_one_band_publishes(self, field_name, path, band):
        svc, pub = _primed()
        base = {"power": _BASE_P, "current": _BASE_I,
                "voltage": _BASE_V, "frequency": _BASE_F}[field_name]
        assert pub.publish(svc, _frame(t=1001.0, **{field_name: base + band}),
                           connected=True) is True
        assert svc.emit_count == 2
        assert path in svc.last_changes

    @pytest.mark.parametrize("field_name,band,step", [
        ("power", gp.GRID_POWER_BAND, gp.GRID_POWER_STEP),
        ("current", gp.GRID_CURRENT_BAND, gp.GRID_CURRENT_STEP),
        ("voltage", gp.GRID_VOLTAGE_BAND, gp.GRID_VOLTAGE_STEP),
    ])
    def test_move_just_under_one_band_does_not_publish(self, field_name, band, step):
        svc, pub = _primed()
        base = {"power": _BASE_P, "current": _BASE_I, "voltage": _BASE_V}[field_name]
        move = _sub_band(band, step)
        assert pub.publish(svc, _frame(t=1001.0, **{field_name: base + move}),
                           connected=True) is False
        assert svc.emit_count == 1

    def test_slow_drift_publishes_when_the_total_reaches_the_band(self):
        band = gp.GRID_CURRENT_BAND
        svc, pub = _primed()
        quarter = band / 4.0
        for k in (1, 2, 3):
            pub.publish(svc, _frame(t=1000.0 + k, current=_BASE_I + k * quarter),
                        connected=True)
            assert svc.emit_count == 1, "published after %d sub-band steps" % k
        pub.publish(svc, _frame(t=1004.0, current=_BASE_I + 4 * quarter),
                    connected=True)
        assert svc.emit_count == 2
        assert svc._values["/Ac/L1/Current"] == gp._round_to(
            _BASE_I + band, gp.GRID_CURRENT_STEP)

    def test_reference_moves_only_on_publish(self):
        # After a publish the band is measured from the new frame, not the
        # original one: a second full band is needed for the next signal.
        band = gp.GRID_POWER_BAND
        svc, pub = _primed()
        pub.publish(svc, _frame(t=1001.0, power=_BASE_P + band), connected=True)
        assert svc.emit_count == 2
        pub.publish(svc, _frame(t=1002.0, power=_BASE_P + band * 1.5), connected=True)
        assert svc.emit_count == 2
        pub.publish(svc, _frame(t=1003.0, power=_BASE_P + band * 2), connected=True)
        assert svc.emit_count == 3


class TestHysteresisCoherentFrame:
    def test_power_crossing_carries_the_frames_current_and_voltage(self):
        svc, pub = _primed()
        dv = _sub_band(gp.GRID_VOLTAGE_BAND, gp.GRID_VOLTAGE_STEP)
        di = _sub_band(gp.GRID_CURRENT_BAND, gp.GRID_CURRENT_STEP)
        # Sub-band drift on V and I alone: held back.
        pub.publish(svc, _frame(t=1001.0, voltage=_BASE_V + dv,
                                current=_BASE_I + di), connected=True)
        assert svc.emit_count == 1
        # Power crosses: the same signal carries this frame's V and I.
        pub.publish(svc, _frame(t=1002.0, voltage=_BASE_V + dv,
                                current=_BASE_I + di,
                                power=_BASE_P + gp.GRID_POWER_BAND),
                    connected=True)
        assert svc.emit_count == 2
        ch = svc.last_changes
        assert ch["/Ac/L1/Power"] == gp._round_to(
            _BASE_P + gp.GRID_POWER_BAND, gp.GRID_POWER_STEP)
        assert ch["/Ac/L1/Voltage"] == gp._round_to(_BASE_V + dv, gp.GRID_VOLTAGE_STEP)
        assert ch["/Ac/L1/Current"] == gp._round_to(_BASE_I + di, gp.GRID_CURRENT_STEP)
        assert ch["/Ac/Voltage"] == ch["/Ac/L1/Voltage"]
        assert ch["/Ac/Current"] == ch["/Ac/L1/Current"]

    def test_current_crossing_refreshes_power(self):
        svc, pub = _primed()
        dp = _sub_band(gp.GRID_POWER_BAND, gp.GRID_POWER_STEP)
        pub.publish(svc, _frame(t=1001.0, power=_BASE_P + dp), connected=True)
        assert svc.emit_count == 1
        pub.publish(svc, _frame(t=1002.0, power=_BASE_P + dp,
                                current=_BASE_I + gp.GRID_CURRENT_BAND),
                    connected=True)
        assert svc.emit_count == 2
        assert svc.last_changes["/Ac/L1/Power"] == gp._round_to(
            _BASE_P + dp, gp.GRID_POWER_STEP)
        assert svc.last_changes["/Ac/Power"] == svc.last_changes["/Ac/L1/Power"]

    def test_l2_and_totals_publish_together(self):
        # A band crossing on L2 refreshes L1 and the totals from the same frame.
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()

        def snap(t, p1, p2):
            return _Snapshot(
                l1=_Line(voltage=120.0, current=10.0, power=p1, energy=1.0, frequency=60.0),
                l2=_Line(voltage=121.0, current=8.0, power=p2, energy=1.0, frequency=60.0),
                has_l2=True, timestamp=t)

        pub.publish(svc, snap(1000.0, 1200.0, 960.0), connected=True)
        dp = _sub_band(gp.GRID_POWER_BAND, gp.GRID_POWER_STEP)
        pub.publish(svc, snap(1001.0, 1200.0 + dp, 960.0), connected=True)
        assert svc.emit_count == 1
        pub.publish(svc, snap(1002.0, 1200.0 + dp, 960.0 + gp.GRID_POWER_BAND),
                    connected=True)
        assert svc.emit_count == 2
        ch = svc.last_changes
        assert ch["/Ac/L1/Power"] == gp._round_to(1200.0 + dp, gp.GRID_POWER_STEP)
        assert ch["/Ac/L2/Power"] == gp._round_to(960.0 + gp.GRID_POWER_BAND,
                                                   gp.GRID_POWER_STEP)
        assert ch["/Ac/Power"] == gp._round_to(
            1200.0 + dp + 960.0 + gp.GRID_POWER_BAND, gp.GRID_POWER_STEP)


class TestHysteresisImmediate:
    def test_first_frame_after_construction_publishes(self):
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        assert pub.publish(svc, _frame(), connected=True) is True
        for path in _LIVE_PATHS:
            assert svc._values[path] is not None, path

    def test_first_frame_after_reset_publishes(self):
        svc, pub = _primed()
        pub.reset()
        svc2 = _lastupdate_svc()
        assert pub.publish(svc2, _frame(t=1001.0), connected=True) is True
        for path in _LIVE_PATHS:
            assert svc2._values[path] is not None, path

    def test_first_frame_after_disconnect_publishes_in_full(self):
        svc, pub = _primed()
        pub.publish(svc, _frame(t=1001.0), connected=False)
        for path in _LIVE_PATHS:
            assert svc._values[path] is None
        pub.publish(svc, _frame(t=1002.0), connected=True)
        for path in _LIVE_PATHS:
            assert svc._values[path] is not None, path
        assert svc._values["/Ac/L1/Power"] == _BASE_P

    def test_reading_going_to_zero_and_back_publishes_under_the_band(self):
        band, step = gp.GRID_POWER_BAND, gp.GRID_POWER_STEP
        small = _sub_band(band, step)   # under the band, displays non-zero
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        pub.publish(svc, _frame(t=1000.0, power=small), connected=True)
        assert svc._values["/Ac/L1/Power"] != 0
        pub.publish(svc, _frame(t=1001.0, power=0.0), connected=True)
        assert svc.emit_count == 2
        assert svc._values["/Ac/L1/Power"] == 0
        assert svc._values["/Ac/Power"] == 0
        pub.publish(svc, _frame(t=1002.0, power=small), connected=True)
        assert svc.emit_count == 3
        assert svc._values["/Ac/L1/Power"] == gp._round_to(small, step)

    def test_connected_change_publishes(self):
        svc, pub = _primed()
        assert pub.publish(svc, _frame(t=1001.0), connected=False) is True
        assert svc.last_changes["/Connected"] == 0

    @pytest.mark.parametrize("error_code", [1, 3, 7, 13])
    def test_error_and_alarm_change_publishes_with_the_frame(self, error_code):
        svc, pub = _primed()
        dp = _sub_band(gp.GRID_POWER_BAND, gp.GRID_POWER_STEP)
        assert pub.publish(svc, _frame(t=1001.0, error_code=error_code,
                                       power=_BASE_P + dp),
                           connected=True) is True
        ch = svc.last_changes
        assert ch["/ErrorCode"] == error_code
        assert ch["/ErrorMessage"] == gp.ERROR_MESSAGES[error_code]
        # The frame's measurements ride along in the same signal.
        assert ch["/Ac/L1/Power"] == gp._round_to(_BASE_P + dp, gp.GRID_POWER_STEP)

    def test_alarm_change_alone_publishes(self):
        svc, pub = _primed()
        pub.publish(svc, _frame(t=1001.0, error_code=13), connected=True)
        assert svc.last_changes["/Alarms/HighTemperature"] == 2

    def test_nr_of_phases_change_publishes(self):
        svc, pub = _primed()
        snap = _frame(t=1001.0)
        snap.has_l2 = True
        snap.l2 = _Line(voltage=_BASE_V, current=0.0, power=0.0,
                        energy=0.0, frequency=_BASE_F)
        assert pub.publish(svc, snap, connected=True) is True
        assert svc.last_changes["/NrOfPhases"] == 2
        assert svc._values["/Ac/L2/Voltage"] == _BASE_V


class TestHysteresisNearZero:
    @pytest.mark.parametrize("field_name,noise", [
        ("power", 0.3),
        # Above half a display step: old rounding flipped 0 <-> one step.
        ("power", 0.6 * gp.GRID_POWER_STEP),
        ("current", 0.6 * gp.GRID_CURRENT_STEP),
    ])
    def test_idle_leg_noise_inside_the_band_is_silent(self, field_name, noise):
        band = {"power": gp.GRID_POWER_BAND, "current": gp.GRID_CURRENT_BAND}[field_name]
        assert noise < band
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        idle = dict(power=0.0, current=0.0)
        pub.publish(svc, _frame(t=1000.0, **idle), connected=True)
        assert svc.emit_count == 1
        for i in range(1, 40):
            kw = dict(idle)
            kw[field_name] = noise if i % 2 else 0.0
            pub.publish(svc, _frame(t=1000.0 + i, **kw), connected=True)
        assert svc.emit_count == 1


class TestHysteresisUngatedRideAlong:
    def test_energy_tick_publishes_at_once_with_the_frame(self):
        svc, pub = _primed()
        dp = _sub_band(gp.GRID_POWER_BAND, gp.GRID_POWER_STEP)
        di = _sub_band(gp.GRID_CURRENT_BAND, gp.GRID_CURRENT_STEP)
        # Held back first, so the energy tick is what releases them.
        pub.publish(svc, _frame(t=1001.0, power=_BASE_P + dp,
                                current=_BASE_I + di), connected=True)
        assert svc.emit_count == 1
        pub.publish(svc, _frame(t=1002.0, power=_BASE_P + dp,
                                current=_BASE_I + di,
                                energy=_BASE_E + gp.GRID_ENERGY_STEP),
                    connected=True)
        assert svc.emit_count == 2
        ch = svc.last_changes
        assert ch["/Ac/L1/Energy/Forward"] == gp._round_to(
            _BASE_E + gp.GRID_ENERGY_STEP, gp.GRID_ENERGY_STEP)
        assert ch["/Ac/L1/Power"] == gp._round_to(_BASE_P + dp, gp.GRID_POWER_STEP)
        assert ch["/Ac/L1/Current"] == gp._round_to(_BASE_I + di, gp.GRID_CURRENT_STEP)

    def test_last_update_tick_refreshes_a_drifted_reading(self):
        svc, pub = _primed()
        idx = svc._values["/UpdateIndex"]
        dv = _sub_band(gp.GRID_VOLTAGE_BAND, gp.GRID_VOLTAGE_STEP)
        interval = int(gp.LAST_UPDATE_INTERVAL)
        for s in range(1, interval):
            pub.publish(svc, _frame(t=1000.0 + s, voltage=_BASE_V + dv),
                        connected=True)
        # Between ticks: nothing on the bus, /UpdateIndex still.
        assert svc.emit_count == 1
        assert svc._values["/UpdateIndex"] == idx
        assert svc._values["/Ac/L1/Voltage"] == _BASE_V
        pub.publish(svc, _frame(t=1000.0 + interval, voltage=_BASE_V + dv),
                    connected=True)
        assert svc.emit_count == 2
        ch = svc.last_changes
        assert ch["/LastUpdate"] == 1000 + interval
        assert ch["/Ac/L1/Voltage"] == gp._round_to(_BASE_V + dv, gp.GRID_VOLTAGE_STEP)
        assert ch["/UpdateIndex"] == idx + 1

    def test_flat_load_between_ticks_emits_nothing(self):
        svc, pub = _primed()
        idx = svc._values["/UpdateIndex"]
        for s in range(1, int(gp.LAST_UPDATE_INTERVAL)):
            assert pub.publish(svc, _frame(t=1000.0 + s), connected=True) is False
        assert svc.emit_count == 1
        assert svc._values["/UpdateIndex"] == idx


class TestCleanDecimals:
    def test_round_to_returns_the_clean_decimal(self):
        assert gp._round_to(2.9, 0.05) == 2.9
        assert repr(gp._round_to(2.9, 0.05)) == "2.9"

    @pytest.mark.parametrize("step", [
        gp.GRID_VOLTAGE_STEP, gp.GRID_CURRENT_STEP, gp.GRID_POWER_STEP,
        gp.GRID_FREQ_STEP, gp.GRID_ENERGY_STEP,
    ])
    def test_buckets_unchanged_and_reprs_short(self, step):
        decimals = max(0, -__import__("decimal").Decimal(str(step)).as_tuple().exponent)
        for i in range(-2000, 2000):
            v = i * 0.0137
            got = gp._round_to(v, step)
            assert got == pytest.approx(round(v / step) * step, abs=1e-9)
            assert got == round(got, decimals), (v, step, got)

    def test_published_current_is_a_clean_decimal(self):
        svc = _lastupdate_svc()
        pub = gp.GridPublisher()
        pub.publish(svc, _frame(current=2.9), connected=True)
        assert repr(svc._values["/Ac/L1/Current"]) == "2.9"
        assert repr(svc._values["/Ac/Current"]) == "2.9"
