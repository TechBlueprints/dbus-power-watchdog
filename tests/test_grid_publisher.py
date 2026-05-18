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


# ── Coarse rounding suppresses noise flicker ───────────────────────────────


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

    def test_voltage_jump_across_step_does_emit(self):
        svc = FakeVeDbusService()
        _full_grid_paths(svc)
        pub = gp.GridPublisher()

        snap1 = _Snapshot(l1=_Line(voltage=119.4, current=10.0, power=1200.0))
        pub.publish(svc, snap1, connected=True)

        # 119.4 rounds to 119.5; 120.0 rounds to 120.0 — different bucket
        snap2 = _Snapshot(l1=_Line(voltage=120.0, current=10.0, power=1200.0))
        any_changed = pub.publish(svc, snap2, connected=True)

        assert any_changed is True
        assert svc.emit_count == 2
        assert svc._values["/Ac/L1/Voltage"] == 120.0
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
        # increments large enough that the rounding step doesn't
        # collapse them.
        for i in range(257):
            snap = _Snapshot(l1=_Line(
                voltage=120.0,
                current=i * gp.GRID_CURRENT_STEP * 2,
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
