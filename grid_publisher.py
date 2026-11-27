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

"""
Shared publisher for Power Watchdog WatchdogData snapshots.

Both the active discovery service (``dbus-power-watchdog.py``) and the
standalone fallback CLI (``power_watchdog_device.py``) need to push
parsed BLE data to a vedbus grid/genset/pvinverter service.  Without
care, that publish path emits ``__setitem__`` -> per-property
``PropertiesChanged`` storms (one per write) at the user-configured
poll rate (down to 100 ms slider position = up to 250 signals/sec for
a single device).  Each signal fans out to every D-Bus subscriber
(gui-v2, vrmlogger, dbus-systemcalc, mqtt-rpc, ...), so reducing the
emit volume helps the entire bus, not just this process.

This module owns the optimizations stacked on the publish path:

1. **Batched writes** — all per-cycle property writes happen inside a
   single ``with svc:`` context so vedbus's refcounted batching
   coalesces them into one ``ItemsChanged`` signal at exit.
2. **Hysteresis gate on the live measurements** — the twelve live
   paths (V / I / P / Frequency on each leg and the totals) publish as
   one frame, and only when something triggers it: a reading that has
   moved at least one band (``GRID_*_BAND``) from the reading at the
   last *published* frame, a displayed zero/non-zero transition, a path
   with no reference yet (first frame, after ``reset()`` or a
   disconnect, a newly present leg or frequency), or any ungated path
   (``/Connected``, ``/LastUpdate``, energy counters, ``/NrOfPhases``,
   ``/ErrorCode``, ``/ErrorMessage``, ``/Alarms/*``) actually being
   written in the same frame.  References move only on publish, so a
   slow drift accumulates until it crosses the band, and a reading
   flapping across a rounding boundary stays silent — the failure mode
   rounded-equality dedup could not remove.  Every signal carries one
   coherent frame (never a new power beside an old current), and since
   the once-a-minute ``/LastUpdate`` write publishes the frame, a held
   value is at most ``LAST_UPDATE_INTERVAL`` stale.  Nothing is
   re-published on a timer beyond that.
3. **Display rounding** — published values are rounded to a display
   step (``GRID_*_STEP``) and cleaned to the step's decimal places, so
   ``2.9`` goes out as ``2.9`` rather than ``2.9000000000000004``.
4. **Local in-RAM cache** — the last value written to each path is
   stored in a python dict.  Skipping the ``svc[path] = value`` call
   entirely on no-op writes avoids the
   ``__setitem__`` -> ``ServiceContext.__setitem__`` ->
   ``_local_set_value`` Python call chain that vedbus would walk on
   every cycle just to return early.
5. **/UpdateIndex gated on real change** — the rolling counter is only
   bumped when at least one *other* path actually changed.  When the
   grid is steady, no writes get through and vedbus suppresses the
   ItemsChanged signal entirely.  When data does change, /UpdateIndex
   bumps and rides along in the same IC.

Signal count is decided by the triggers alone, not by the poll rate:
on a flat load the service emits one ItemsChanged a minute (the
``/LastUpdate`` tick) plus one per energy-counter tick.  Under the old
rounded-equality gate a reading sitting on a step boundary signalled
on nearly every poll (0.6-0.7 IC/s measured on prod on 2026-11-26 at a
1400 ms poll).

Caller contract:

- Pass a ``VeDbusService`` (or compatible).
- The service must have the relevant grid-meter paths declared via
  ``add_path`` before the first ``publish`` call; paths not present on
  the service are silently skipped (allows older callers without
  ``/ErrorMessage`` to use the same publisher).
- Call ``reset()`` whenever the service is destroyed and recreated
  (e.g. role change) so the dedup cache and ``/UpdateIndex`` start
  fresh against the new service's empty state.
"""

from __future__ import annotations

import decimal
import functools

# Per-quantity display step.  Published values are rounded to a
# multiple of these.  They decide what the GUI shows, not when a signal
# goes out: that is the hysteresis bands' job (below).
GRID_VOLTAGE_STEP = 0.5     # V — grid is 120 V ±5%; 0.5 V resolution still useful
GRID_CURRENT_STEP = 0.05    # A — 50 mA = ~6 W at 120 V
GRID_POWER_STEP = 5         # W — useful loads are ≥ 5 W anyway
GRID_FREQ_STEP = 0.1        # Hz — frequency is genuinely stable, no coarsening
GRID_ENERGY_STEP = 0.01     # kWh — counter, monotone, fine resolution useful

# Hysteresis bands for the live measurements.  A frame is published when
# any reading has moved at least its band from the reading at the last
# published frame (see GridPublisher._gate_tripped for the other
# triggers).  Each band should sit above the reading's measured
# frame-to-frame noise on a steady load, so noise alone never trips it.
# Energy counters are not banded: they publish on every GRID_ENERGY_STEP.
GRID_POWER_BAND = 10        # W  — PROVISIONAL; measured noise: (pending capture)
GRID_CURRENT_BAND = 0.1     # A  — PROVISIONAL; measured noise: (pending capture)
GRID_VOLTAGE_BAND = 1.0     # V  — PROVISIONAL; measured noise: (pending capture)
GRID_FREQ_BAND = 0.1        # Hz — PROVISIONAL; measured noise: (pending capture)

# How often /LastUpdate is rewritten while parsed telemetry flows.  It is
# the freshness signal the hysteresis-gated measurement paths cannot be:
# a flat load leaves them (and therefore /UpdateIndex) unchanged for as
# long as it stays flat, which is indistinguishable from a parser that has
# stopped producing.  One write a minute is a bounded D-Bus cost and makes
# /UpdateIndex advance at least that often whenever real frames are being
# parsed — and never when they are not.  Because it is an ungated write,
# it also publishes the frame's measurements, so a held reading is never
# more than this stale.
LAST_UPDATE_INTERVAL = 60.0  # seconds

# The live measurement paths, nulled on disconnect and hysteresis-gated
# while connected.
GRID_LIVE_PATHS: tuple[str, ...] = (
    "/Ac/L1/Voltage", "/Ac/L1/Current", "/Ac/L1/Power", "/Ac/L1/Frequency",
    "/Ac/L2/Voltage", "/Ac/L2/Current", "/Ac/L2/Power", "/Ac/L2/Frequency",
    "/Ac/Power", "/Ac/Current", "/Ac/Voltage", "/Ac/Frequency",
)

# Relative slack on band and step comparisons, so a move of exactly one
# band (e.g. 10.1 - 10.0 = 0.09999999999999964) counts as one band.
_CMP_EPS = 1e-9


# /Alarms/* paths surfaced on the grid service.  venus-platform watches
# these and creates notification slots (Cerbo Notifications pane,
# flashing-triangle icon).  Severity is 0=OK, 1=Warning, 2=Alarm.
GRID_ALARM_PATHS: tuple[str, ...] = (
    "/Alarms/LowVoltage",
    "/Alarms/HighVoltage",
    "/Alarms/Overload",
    "/Alarms/HighTemperature",
)


def _alarms_for_state(error_code: int, input_voltage: float | None) -> dict[str, int]:
    """Map the current Power Watchdog error code to /Alarms/* severities.

    The error code is the trigger: codes 1 (L1) and 2 (L2) are the
    device's "voltage error", asserted when input leaves the 104-132 V
    window.  The DLReport input voltage on the faulting leg tells us
    which side of the window we left, so we route to LowVoltage or
    HighVoltage accordingly.  Codes the GUI has no standard /Alarms
    slot for (5-9, 11-12, 14) surface via /ErrorCode only.
    """
    if error_code in (1, 2):
        if input_voltage is not None and input_voltage > 132:
            return {"/Alarms/HighVoltage": 2}
        if input_voltage is not None and input_voltage < 104:
            return {"/Alarms/LowVoltage": 2}
        # Trigger fired but voltage outside our window check (e.g.
        # device cut output and is reporting 0) — default to low,
        # which is the dominant RV failure mode.
        return {"/Alarms/LowVoltage": 2}
    if error_code in (3, 4):
        return {"/Alarms/Overload": 2}
    if error_code == 13:
        return {"/Alarms/HighTemperature": 2}
    return {}


# Map Power Watchdog Gen2 error codes to display strings.  Used for
# /ErrorMessage where the service exposes that path.  Codes 0 and 10
# are "no error" / unused; remaining codes from device manual.
ERROR_MESSAGES = {
    0: "",
    1: "Line 1 Voltage Error",
    2: "Line 2 Voltage Error",
    3: "Line 1 Over Current",
    4: "Line 2 Over Current",
    5: "Line 1 Neutral Reversed",
    6: "Line 2 Neutral Reversed",
    7: "Missing Ground",
    8: "Neutral Missing",
    9: "Surge Protection Used Up",
    11: "Line 1 Frequency Error",
    12: "Line 2 Frequency Error",
    13: "Over Temperature",
    14: "Boost Error",
}


@functools.lru_cache(maxsize=None)
def _step_decimals(step: float) -> int:
    """Decimal places of *step* as written: 0.05 -> 2, 0.5 -> 1, 5 -> 0."""
    return max(0, -decimal.Decimal(str(step)).as_tuple().exponent)


def _round_to(value: float, step: float) -> float:
    """Round *value* to the nearest multiple of *step*, as a clean decimal.

    ``round(value / step) * step`` picks the bucket; the product is then
    rounded to the step's own decimal places, which removes binary
    noise (``58 * 0.05`` is ``2.9000000000000004``) without moving the
    value to a different bucket.  An int step on an int-valued bucket
    stays an int.

    Example: a US-grid voltage of 119.4 V rounds to 119.5 V at a 0.5 V
    step.  Rounding only decides what is displayed; whether the value
    is published is the hysteresis gate's decision.
    """
    if step <= 0:
        return value
    bucketed = round(value / step) * step
    if isinstance(step, int):
        return bucketed
    return round(bucketed, _step_decimals(step))


class GridPublisher:
    """Publishes a ``WatchdogData`` snapshot to a vedbus grid service.

    Owns the rolling ``/UpdateIndex`` counter, an in-RAM cache of the
    last value written to each path, and the hysteresis references (the
    live readings at the last published frame).  All reset on
    ``reset()``.

    Thread-safety: not thread-safe.  Call from the GLib mainloop only.
    """

    def __init__(self) -> None:
        self._update_index: int = 0
        self._last_values: dict[str, object] = {}
        # Epoch of the last /LastUpdate we wrote; 0 forces the first write.
        self._last_update_published: float = 0.0
        # Live path -> unrounded reading at the last published frame.
        # Empty means "no reference": the next data frame publishes.
        self._references: dict[str, float] = {}

    def reset(self) -> None:
        """Drop the dedup cache and references, and reset ``/UpdateIndex``.

        Call this when the underlying ``VeDbusService`` is destroyed
        and recreated (e.g. role change), so a fresh service doesn't
        get its first writes deduped against stale cache entries.
        """
        self._update_index = 0
        self._last_values.clear()
        self._last_update_published = 0.0
        self._references.clear()

    def _set_if_changed(self, ctx, path: str, value) -> bool:
        """Write *value* to *ctx[path]* iff the local cache shows it
        differs from our last write.

        Returns True iff the write was actually issued.

        Silently skips paths not declared on the underlying service —
        lets the older standalone CLI call this publisher even if it
        doesn't declare every optional path the active service does
        (e.g. ``/ErrorMessage``).
        """
        if self._last_values.get(path) == value:
            return False
        if path not in ctx:
            return False
        self._last_values[path] = value
        ctx[path] = value
        return True

    def _gate_tripped(self, readings) -> bool:
        """True iff this frame's live readings must be published.

        *readings* is a list of ``(path, reading, band, step)``.  Triggers:

        - the path has no reference (first frame, after ``reset()`` or a
          disconnect, or a path that newly applies);
        - ``abs(reading - reference) >= band``;
        - the displayed value goes to zero from non-zero, or leaves zero
          by at least one display step.

        The zero rule works on displayed values so that idle-leg noise
        that displays as 0 (e.g. 0.0 / 0.3 W) stays silent, and it is
        asymmetric so noise between zero and one step cannot flap it:
        once a displayed zero is published, a reading must reach a full
        step to leave it.
        """
        refs = self._references
        for path, reading, band, step in readings:
            ref = refs.get(path)
            if ref is None:
                return True
            if abs(reading - ref) >= band * (1.0 - _CMP_EPS):
                return True
            ref_shown_zero = _round_to(ref, step) == 0
            if not ref_shown_zero and _round_to(reading, step) == 0:
                return True
            if ref_shown_zero and abs(reading) >= step * (1.0 - _CMP_EPS):
                return True
        return False

    def publish(self, svc, data, connected: bool) -> bool:
        """Push one ``WatchdogData`` snapshot to *svc*.

        Wraps the writes in ``with svc:`` so vedbus emits at most one
        ``ItemsChanged`` signal at __exit__.  When the cache shows
        nothing actually changed, ``/UpdateIndex`` stays put and vedbus
        suppresses the ItemsChanged entirely (empty changes dict).

        Ungated paths (``/Connected``, ``/LastUpdate``, ``/NrOfPhases``,
        ``/ErrorCode``, ``/ErrorMessage``, ``/Alarms/*``, the energy
        counters) are written whenever their value changes.  The live
        measurements are written as one frame, only when
        ``_gate_tripped`` says so or an ungated path was written in the
        same frame; see the module docstring.

        When ``connected`` is False, the instantaneous-measurement
        paths (V / I / P / Frequency on each line and the totals) are
        explicitly nulled so dbus-systemcalc-py's Grid/Consumption
        rollup stops aggregating stale readings.  The Hughes Power
        Watchdog is shore-power-powered — when the user unplugs shore,
        the device powers down and stops advertising; without this
        null-on-disconnect the GUI keeps showing the last-known
        ``/Ac/L1/Power`` (e.g. "AC Loads: 1450 W") forever, which is
        misleading.  Cumulative energy counters
        (``/Ac/{L1,L2,}/Energy/Forward``) are preserved across the
        disconnect because they represent persistent state, not a
        live measurement.  The hysteresis references are dropped, so
        the first connected frame afterwards publishes in full.

        Returns True iff at least one path was actually written.
        """
        with svc as ctx:
            any_changed = self._set_if_changed(
                ctx, "/Connected", 1 if connected else 0
            )

            if not connected:
                # Null out everything that represents a live reading.
                # vedbus treats None as "no value" and dbus-systemcalc-py
                # excludes None-valued paths from its sums.
                for path in GRID_LIVE_PATHS:
                    any_changed |= self._set_if_changed(ctx, path, None)
                self._references.clear()

                # /Alarms/* and /ErrorCode are NOT cleared on disconnect
                # — the last-known fault is more useful to the user than
                # a false "all clear" while BLE is dropped.  They'll be
                # re-evaluated from the next DLReport on reconnect.

                if any_changed:
                    self._update_index = (self._update_index + 1) % 256
                    self._set_if_changed(
                        ctx, "/UpdateIndex", self._update_index)
                return any_changed

            if data.timestamp > 0:
                # ── Ungated paths: written whenever they change ──
                # Freshness, throttled.  data.timestamp is set only when a
                # frame actually PARSED, so this is the one path that can
                # not read as healthy while the parser is dead — the
                # failure that kept /Connected=1 and a live link green for
                # 15-minute cycles on 2026-08-26.
                if data.timestamp - self._last_update_published >= LAST_UPDATE_INTERVAL:
                    any_changed |= self._set_if_changed(
                        ctx, "/LastUpdate", int(data.timestamp))
                    self._last_update_published = data.timestamp

                l1 = data.l1
                has_l2 = data.has_l2
                l2 = data.l2 if has_l2 else None

                total_power = l1.power
                total_current = l1.current
                total_energy = l1.energy
                error_code = l1.error_code
                if has_l2:
                    total_power += l2.power
                    total_current += l2.current
                    total_energy += l2.energy
                    if l2.error_code > error_code:
                        error_code = l2.error_code

                any_changed |= self._set_if_changed(
                    ctx, "/Ac/L1/Energy/Forward",
                    _round_to(l1.energy, GRID_ENERGY_STEP))
                if has_l2:
                    any_changed |= self._set_if_changed(
                        ctx, "/Ac/L2/Energy/Forward",
                        _round_to(l2.energy, GRID_ENERGY_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/Energy/Forward",
                    _round_to(total_energy, GRID_ENERGY_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/NrOfPhases", 2 if has_l2 else 1)
                any_changed |= self._set_if_changed(
                    ctx, "/ErrorCode", error_code)
                any_changed |= self._set_if_changed(
                    ctx, "/ErrorMessage",
                    ERROR_MESSAGES.get(error_code, "Unknown Error %d" % error_code))

                # Pick the faulting line's input voltage so the
                # low/high split uses the leg that actually tripped.
                alarm_voltage = l1.voltage
                if has_l2 and l2.error_code in (1, 2):
                    alarm_voltage = l2.voltage
                active_alarms = _alarms_for_state(error_code, alarm_voltage)
                for path in GRID_ALARM_PATHS:
                    any_changed |= self._set_if_changed(
                        ctx, path, active_alarms.get(path, 0))

                # ── Gated paths: the live measurements, as one frame ──
                readings = self._live_readings(
                    l1, l2, total_power, total_current)
                if any_changed or self._gate_tripped(readings):
                    for path, reading, _band, step in readings:
                        any_changed |= self._set_if_changed(
                            ctx, path, _round_to(reading, step))
                    self._references = {
                        path: reading for path, reading, _b, _s in readings}

                if any_changed:
                    self._update_index = (self._update_index + 1) % 256
                    self._set_if_changed(
                        ctx, "/UpdateIndex", self._update_index)

            return any_changed

    @staticmethod
    def _live_readings(l1, l2, total_power: float, total_current: float):
        """This frame's ``(path, reading, band, step)`` for every live
        path that applies: L2 only when present, a frequency only when
        its leg reports one above 0."""
        readings = []

        def leg(prefix, line):
            readings.append((prefix + "/Voltage", line.voltage,
                             GRID_VOLTAGE_BAND, GRID_VOLTAGE_STEP))
            readings.append((prefix + "/Current", line.current,
                             GRID_CURRENT_BAND, GRID_CURRENT_STEP))
            readings.append((prefix + "/Power", line.power,
                             GRID_POWER_BAND, GRID_POWER_STEP))
            if line.frequency > 0:
                readings.append((prefix + "/Frequency", line.frequency,
                                 GRID_FREQ_BAND, GRID_FREQ_STEP))

        leg("/Ac/L1", l1)
        if l2 is not None:
            leg("/Ac/L2", l2)

        avg_voltage = l1.voltage
        if l2 is not None and l2.voltage > 0:
            avg_voltage = (l1.voltage + l2.voltage) / 2.0
        readings.append(("/Ac/Power", total_power,
                         GRID_POWER_BAND, GRID_POWER_STEP))
        readings.append(("/Ac/Current", total_current,
                         GRID_CURRENT_BAND, GRID_CURRENT_STEP))
        readings.append(("/Ac/Voltage", avg_voltage,
                         GRID_VOLTAGE_BAND, GRID_VOLTAGE_STEP))
        if l1.frequency > 0:
            readings.append(("/Ac/Frequency", l1.frequency,
                             GRID_FREQ_BAND, GRID_FREQ_STEP))
        return readings
