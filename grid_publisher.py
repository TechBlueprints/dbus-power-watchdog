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

This module owns the four optimizations stacked on the publish path:

1. **Batched writes** — all per-cycle property writes happen inside a
   single ``with svc:`` context so vedbus's refcounted batching
   coalesces them into one ``ItemsChanged`` signal at exit.
2. **Coarse rounding** — values are rounded to a step coarser than the
   device's noise floor.  A steady underlying load produces a steady
   rounded value, so vedbus's per-path "value didn't change" dedup
   actually catches it.
3. **Local in-RAM cache** — the last value written to each path is
   stored in a python dict.  Skipping the ``svc[path] = value`` call
   entirely on no-op writes avoids the
   ``__setitem__`` -> ``ServiceContext.__setitem__`` ->
   ``_local_set_value`` Python call chain that vedbus would walk on
   every cycle just to return early.
4. **/UpdateIndex gated on real change** — the rolling counter is only
   bumped when at least one *other* path actually changed.  When the
   grid is steady, no writes get through and vedbus suppresses the
   ItemsChanged signal entirely.  When data does change, /UpdateIndex
   bumps and rides along in the same IC.

Combined effect on a busy Cerbo running at 500 ms poll: pre-batching
emits ~5 PropertiesChanged/sec; with everything on, ~0.5 IC/sec — a
~10× drop in bus traffic just from this one device.

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

# Per-quantity rounding step.  Tuned to the noise floor of the Power
# Watchdog's reported values: voltages flicker ±0.1 V, currents ±0.01 A,
# power ±1-2 W when the grid is steady.  Rounding to a multiple of
# these values lets vedbus's per-path dedup eliminate the flicker.
GRID_VOLTAGE_STEP = 0.5     # V — grid is 120 V ±5%; 0.5 V resolution still useful
GRID_CURRENT_STEP = 0.05    # A — kills 10-50 mA noise; 50 mA = ~6 W at 120 V
GRID_POWER_STEP = 5         # W — kills 1-2 W noise; useful loads are ≥ 5 W anyway
GRID_FREQ_STEP = 0.1        # Hz — frequency is genuinely stable, no coarsening
GRID_ENERGY_STEP = 0.01     # kWh — counter, monotone, fine resolution useful


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


def _round_to(value: float, step: float) -> float:
    """Round *value* to the nearest multiple of *step*.

    Coarser-than-decimal rounding kills measurement-noise flicker that
    would otherwise defeat vedbus's per-path "value didn't change"
    dedup, causing every poll cycle to emit an ItemsChanged signal
    even when the underlying load is steady.

    Example: a US-grid voltage reading flickering between 119.4 V and
    119.5 V every cycle survives ``round(x, 1)`` (returns 119.4 vs
    119.5) but collapses under ``_round_to(x, 0.5)`` (both round to
    119.5).
    """
    if step <= 0:
        return value
    return round(value / step) * step


class GridPublisher:
    """Publishes a ``WatchdogData`` snapshot to a vedbus grid service.

    Owns the rolling ``/UpdateIndex`` counter and an in-RAM cache of
    the last value written to each path.  Both reset on ``reset()``.

    Thread-safety: not thread-safe.  Call from the GLib mainloop only.
    """

    def __init__(self) -> None:
        self._update_index: int = 0
        self._last_values: dict[str, object] = {}

    def reset(self) -> None:
        """Drop the dedup cache and reset ``/UpdateIndex``.

        Call this when the underlying ``VeDbusService`` is destroyed
        and recreated (e.g. role change), so a fresh service doesn't
        get its first writes deduped against stale cache entries.
        """
        self._update_index = 0
        self._last_values.clear()

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

    def publish(self, svc, data, connected: bool) -> bool:
        """Push one ``WatchdogData`` snapshot to *svc*.

        Wraps the writes in ``with svc:`` so vedbus emits at most one
        ``ItemsChanged`` signal at __exit__.  When the cache shows
        nothing actually changed, ``/UpdateIndex`` stays put and vedbus
        suppresses the ItemsChanged entirely (empty changes dict).

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
        live measurement.

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
                for path in (
                    "/Ac/L1/Voltage", "/Ac/L1/Current", "/Ac/L1/Power",
                    "/Ac/L1/Frequency",
                    "/Ac/L2/Voltage", "/Ac/L2/Current", "/Ac/L2/Power",
                    "/Ac/L2/Frequency",
                    "/Ac/Power", "/Ac/Current", "/Ac/Voltage",
                    "/Ac/Frequency",
                ):
                    any_changed |= self._set_if_changed(ctx, path, None)

                if any_changed:
                    self._update_index = (self._update_index + 1) % 256
                    self._set_if_changed(
                        ctx, "/UpdateIndex", self._update_index)
                return any_changed

            if data.timestamp > 0:
                l1 = data.l1
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/L1/Voltage",
                    _round_to(l1.voltage, GRID_VOLTAGE_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/L1/Current",
                    _round_to(l1.current, GRID_CURRENT_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/L1/Power",
                    _round_to(l1.power, GRID_POWER_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/L1/Energy/Forward",
                    _round_to(l1.energy, GRID_ENERGY_STEP))
                if l1.frequency > 0:
                    any_changed |= self._set_if_changed(
                        ctx, "/Ac/L1/Frequency",
                        _round_to(l1.frequency, GRID_FREQ_STEP))

                total_power = l1.power
                total_current = l1.current
                total_energy = l1.energy
                error_code = l1.error_code

                if data.has_l2:
                    l2 = data.l2
                    any_changed |= self._set_if_changed(
                        ctx, "/Ac/L2/Voltage",
                        _round_to(l2.voltage, GRID_VOLTAGE_STEP))
                    any_changed |= self._set_if_changed(
                        ctx, "/Ac/L2/Current",
                        _round_to(l2.current, GRID_CURRENT_STEP))
                    any_changed |= self._set_if_changed(
                        ctx, "/Ac/L2/Power",
                        _round_to(l2.power, GRID_POWER_STEP))
                    any_changed |= self._set_if_changed(
                        ctx, "/Ac/L2/Energy/Forward",
                        _round_to(l2.energy, GRID_ENERGY_STEP))
                    if l2.frequency > 0:
                        any_changed |= self._set_if_changed(
                            ctx, "/Ac/L2/Frequency",
                            _round_to(l2.frequency, GRID_FREQ_STEP))
                    total_power += l2.power
                    total_current += l2.current
                    total_energy += l2.energy
                    if l2.error_code > error_code:
                        error_code = l2.error_code

                any_changed |= self._set_if_changed(
                    ctx, "/NrOfPhases", 2 if data.has_l2 else 1)
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/Power",
                    _round_to(total_power, GRID_POWER_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/Current",
                    _round_to(total_current, GRID_CURRENT_STEP))
                avg_voltage = l1.voltage
                if data.has_l2 and data.l2.voltage > 0:
                    avg_voltage = (l1.voltage + data.l2.voltage) / 2.0
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/Voltage",
                    _round_to(avg_voltage, GRID_VOLTAGE_STEP))
                if l1.frequency > 0:
                    any_changed |= self._set_if_changed(
                        ctx, "/Ac/Frequency",
                        _round_to(l1.frequency, GRID_FREQ_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/Ac/Energy/Forward",
                    _round_to(total_energy, GRID_ENERGY_STEP))
                any_changed |= self._set_if_changed(
                    ctx, "/ErrorCode", error_code)
                any_changed |= self._set_if_changed(
                    ctx, "/ErrorMessage",
                    ERROR_MESSAGES.get(error_code, "Unknown Error %d" % error_code))

                if any_changed:
                    self._update_index = (self._update_index + 1) % 256
                    self._set_if_changed(
                        ctx, "/UpdateIndex", self._update_index)

            return any_changed
