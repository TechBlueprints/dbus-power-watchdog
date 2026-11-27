#!/usr/bin/env python3
"""Record what the Power Watchdog grid service publishes, value by value, and
replay a hysteresis gate over it.

The service rounds each measurement to a fixed step and writes it only when
the rounded value changes (``grid_publisher.py``). A reading that sits on a
step boundary flips between two rounded values and every flip is one
``ItemsChanged`` signal, which wakes every BusItem subscriber on the box.
This script measures how much of that churn is flapping and how much is the
load really moving, and what a reference-plus-band gate would leave.

Capture (runs on the Cerbo, fed over stdin, writes nothing there):

    ssh cerbo python3 - 900 < scripts/grid_value_capture.py \\
        > measurements/x.json 2> measurements/x.err

It finds the one ``com.victronenergy.grid.power_watchdog_*`` name, seeds the
full state with one ``GetItems`` on ``/``, then subscribes with a match rule
narrowed to that sender and ``com.victronenergy.BusItem``, so it wakes only
for this service's signals. Every ``ItemsChanged`` / ``PropertiesChanged`` is
recorded with its CLOCK_MONOTONIC time and each path's new value. A GLib
timeout stops it (BusyBox has no timeout(1)). Stdout is JSON only; stderr
gets one progress line a minute.

Analysis (runs anywhere, standard library only):

    python3 scripts/grid_value_capture.py --report x.json
    python3 scripts/grid_value_capture.py --sweep x.json [--full]
    python3 scripts/grid_value_capture.py --replay-publisher x.json

``--sweep`` replays the gate planned for ``grid_publisher.py``: each live
measurement path keeps the reading at the last published frame as its
reference; a frame is published when a path has no reference, when any
reading is at least its quantity's band away from its reference, when a
reading is exactly 0 and its reference is not (or the reverse), or when any
ungated path changed (``/Connected``, ``/NrOfPhases``, ``/ErrorCode``,
``/ErrorMessage``, ``/Alarms/*``, the energy counters, ``/LastUpdate``). A
published frame writes every measurement path and resets every reference.
The captured values are already rounded to today's steps, so they stand in
for the readings; the row with bands equal to today's steps reproduces the
capture. The model gates the captured ``/Ac/*`` totals as readings of their
own, but they are the rounded unrounded sum and often sit a step off the sum
of the rounded legs, so the sweep over-counts: on the 2026-11-27 capture at
10 W / 0.1 A / 1.0 V / 0.1 Hz it says 133 signals where the real code says
122, and deriving the totals from the legs in the model gives 122 exactly.
Its zero rule is also the literal "exactly 0", not the code's displayed-zero
rule (no zero crossings in that capture, so no difference there). Use the
sweep to compare bands, and ``--replay-publisher`` for the figure.

``--replay-publisher`` feeds the capture through the real
``grid_publisher.GridPublisher`` instead of the model above, against an
in-memory service that counts emitted signals. It rebuilds one
``WatchdogData`` per timer tick from the captured leg paths (the real code
derives ``/Ac/*`` totals and the total energy from the legs, so the captured
totals are not inputs), stamps each tick with the capture's wall clock so
``/LastUpdate`` runs on its real 60 s throttle, and starts the publisher from
the seed state and the seed's ``/LastUpdate`` phase. Ticks between captured
signals, when prod published nothing, are fed too, at ``/RefreshTime``
spacing, carrying the unchanged state. Needs the repository root on the
import path (it adds it itself); standard library otherwise.
"""
import json
import sys
import time

SERVICE_PREFIX = "com.victronenergy.grid.power_watchdog_"
BUSITEM = "com.victronenergy.BusItem"

# Today's rounding steps (grid_publisher.py GRID_*_STEP).
STEP = {"voltage": 0.5, "current": 0.05, "power": 5.0, "frequency": 0.1, "energy": 0.01}

MEASUREMENT = {}
for _leg in ("/Ac/L1", "/Ac/L2", "/Ac"):
    MEASUREMENT[_leg + "/Voltage"] = "voltage"
    MEASUREMENT[_leg + "/Current"] = "current"
    MEASUREMENT[_leg + "/Power"] = "power"
    MEASUREMENT[_leg + "/Frequency"] = "frequency"
ENERGY = ("/Ac/L1/Energy/Forward", "/Ac/L2/Energy/Forward", "/Ac/Energy/Forward")
LAST_UPDATE = "/LastUpdate"
DERIVED = ("/UpdateIndex",)     # bumped by the publisher itself, not an input
EPS = 1e-6


# ---------------------------------------------------------------- capture --

def _plain(v):
    """dbus value -> JSON value; an empty array is vedbus's 'invalid'."""
    import dbus
    if isinstance(v, (dbus.Array, list)):
        return [_plain(x) for x in v] if len(v) else None
    if isinstance(v, (dbus.Dictionary, dict)):
        return dict((str(k), _plain(x)) for k, x in v.items())
    if isinstance(v, dbus.Boolean):
        return bool(v)
    if isinstance(v, (dbus.String, dbus.ObjectPath, str)):
        return str(v)
    if isinstance(v, float):
        return float(v)
    if isinstance(v, int):
        return int(v)
    return str(v)


def _loadavg():
    with open("/proc/loadavg") as f:
        return f.read().strip()


def capture(duration):
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    mono = time.CLOCK_MONOTONIC
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    daemon = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
    names = [str(n) for n in daemon.ListNames(dbus_interface="org.freedesktop.DBus")
             if str(n).startswith(SERVICE_PREFIX)]
    if len(names) != 1:
        sys.stderr.write("expected one %s* name, found %r\n" % (SERVICE_PREFIX, names))
        sys.exit(2)
    service = names[0]
    owner = str(daemon.GetNameOwner(service, dbus_interface="org.freedesktop.DBus"))

    events = []                 # [t_mono, member, {path: value}]
    owner_changes = []          # [t_mono, old, new]

    def on_message(_bus, msg):
        t = time.clock_gettime(mono)
        if msg.get_type() != 4:                 # signals only
            return
        iface = msg.get_interface()
        member = msg.get_member()
        if iface == "org.freedesktop.DBus" and member == "NameOwnerChanged":
            a = msg.get_args_list()
            if len(a) == 3 and str(a[0]) == service:
                owner_changes.append([t, str(a[1]), str(a[2])])
            return
        if iface != BUSITEM or msg.get_sender() != owner:
            return
        try:
            args = msg.get_args_list()
            if member == "ItemsChanged":
                items = dict((str(p), _plain(d.get("Value")))
                             for p, d in args[0].items())
            elif member == "PropertiesChanged":
                items = {str(msg.get_path()): _plain(args[0].get("Value"))}
            else:
                return
        except Exception as e:                  # keep the event, flag it
            items = {"?error": str(e)}
        events.append([t, member, items])

    bus.add_match_string("type='signal',sender='%s',interface='%s'" % (owner, BUSITEM))
    bus.add_match_string("type='signal',sender='org.freedesktop.DBus',"
                         "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                         "arg0='%s'" % service)
    bus.add_message_filter(on_message)

    load0 = _loadavg()
    t0w = time.time()
    t0m = time.clock_gettime(mono)
    cpu0 = time.process_time()
    root = bus.get_object(service, "/", introspect=False)
    try:
        seed = dict((str(p), _plain(d.get("Value")))
                    for p, d in root.GetItems(dbus_interface=BUSITEM).items())
        seed_method = "GetItems"
    except dbus.DBusException:
        seed = dict(("/" + str(p).lstrip("/"), _plain(v))
                    for p, v in root.GetValue(dbus_interface=BUSITEM).items())
        seed_method = "GetValue"
    t_seed = time.clock_gettime(mono)

    loop = GLib.MainLoop()

    def progress():
        sys.stderr.write("%s  %4.0f s  %d signals\n" % (
            time.strftime(fmt), time.clock_gettime(mono) - t0m, len(events)))
        sys.stderr.flush()
        return True

    sys.stderr.write("%s  start: %s (%s), RefreshTime %s ms, %d s\n" % (
        time.strftime(fmt, time.gmtime(t0w)), service, owner,
        seed.get("/RefreshTime"), duration))
    sys.stderr.flush()
    GLib.timeout_add_seconds(60, progress)
    GLib.timeout_add(int(duration * 1000), loop.quit)
    loop.run()
    t1m = time.clock_gettime(mono)
    t1w = time.time()
    cpu = time.process_time() - cpu0
    load1 = _loadavg()
    bus.remove_message_filter(on_message)
    sys.stderr.write("%s  end: %d signals in %.1f s\n" % (
        time.strftime(fmt, time.gmtime(t1w)), len(events), t1m - t0m))

    json.dump({
        "start_utc": time.strftime(fmt, time.gmtime(t0w)),
        "end_utc": time.strftime(fmt, time.gmtime(t1w)),
        "duration": t1m - t0m,
        "t0_mono": t0m, "t_seed_mono": t_seed,
        "wall_minus_mono": t1w - t1m,
        "service": service, "owner": owner,
        "refresh_time_ms": seed.get("/RefreshTime"),
        "seed_method": seed_method, "seed": seed,
        "loadavg_start": load0, "loadavg_end": load1,
        "sampler_cpu_pct": 100.0 * cpu / (t1m - t0m),
        "owner_changes": owner_changes,
        "events": events,
    }, sys.stdout, sort_keys=True)


# --------------------------------------------------------------- analysis --

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _same(a, b):
    if _num(a) and _num(b):
        return abs(a - b) < EPS
    return a == b


def _load(path):
    with open(path) as f:
        d = json.load(f)
    # Events dispatched before the seed reply came back carry older state.
    d["events"] = [e for e in d["events"] if e[0] >= d.get("t_seed_mono", 0)]
    return d


def _header(d):
    print("%s .. %s  (%.0f s)  %s" % (d["start_utc"], d["end_utc"], d["duration"], d["service"]))
    print("loadavg start: %s   end: %s   sampler cpu %.2f%%" % (
        d["loadavg_start"], d["loadavg_end"], d.get("sampler_cpu_pct", 0)))
    if d.get("owner_changes"):
        print("WARNING: service owner changed during the capture: %r" % d["owner_changes"])


def _series(d):
    """path -> [(t, value)] starting with the seed value at t_seed."""
    s = {}
    for p, v in d["seed"].items():
        s[p] = [(d.get("t_seed_mono", d["t0_mono"]), v)]
    for t, _m, items in d["events"]:
        for p, v in items.items():
            s.setdefault(p, []).append((t, v))
    return s


def report(path):
    d = _load(path)
    dur = d["duration"]
    ev = d["events"]
    rt = d.get("refresh_time_ms") or 0
    _header(d)
    n = len(ev)
    print("\nsignals: %d = %.3f/s" % (n, n / dur))
    if rt:
        ticks = dur * 1000.0 / rt
        print("timer ticks (duration / RefreshTime %d ms): %.0f; share that signalled: %.1f%%"
              % (rt, ticks, 100.0 * n / ticks))
    if n > 1:
        gaps = sorted(b[0] - a[0] for a, b in zip(ev, ev[1:]))
        print("inter-signal gap: min %.2f  median %.2f  p90 %.2f  max %.1f s" % (
            gaps[0], gaps[len(gaps) // 2], gaps[int(len(gaps) * 0.9)], gaps[-1]))
    sizes = {}
    for e in ev:
        sizes[len(e[2])] = sizes.get(len(e[2]), 0) + 1
    print("items per signal: " + "  ".join("%d:%d" % kv for kv in sorted(sizes.items())))

    per = {}
    for _t, _m, items in ev:
        for p in items:
            per[p] = per.get(p, 0) + 1
    print("\nper path: signals/s carrying it")
    for p, c in sorted(per.items(), key=lambda kv: (-kv[1], kv[0])):
        print("  %7.3f/s  %5d  %s" % (c / dur, c, p))

    # Which paths in each signal, as a pattern: how the churn groups.
    pat = {}
    for _t, _m, items in ev:
        k = " ".join(sorted(p for p in items if p not in DERIVED)) or "(UpdateIndex only)"
        pat[k] = pat.get(k, 0) + 1
    print("\nsignal contents (excluding /UpdateIndex), top 12")
    for k, c in sorted(pat.items(), key=lambda kv: -kv[1])[:12]:
        print("  %7.3f/s  %5d  %s" % (c / dur, c, k))

    series = _series(d)
    print("\nmeasurement paths: changes, flaps (back to the value two changes earlier, one step"
          "\naway), moves (the rest); change size in steps; min / max / peak-to-peak; median dwell")
    print("  %-22s %6s %6s %6s  %-26s %10s %10s %8s %7s" % (
        "path", "chg", "flap", "move", "size(steps):count", "min", "max", "p2p", "dwell"))
    for p in sorted(set(MEASUREMENT) | set(ENERGY), key=lambda x: (x.count("/"), x)):
        if p not in series:
            continue
        q = MEASUREMENT.get(p, "energy")
        step = STEP[q]
        pts = series[p]
        vals = [v for _t, v in pts]
        nums = [v for v in vals if _num(v)]
        chg = flap = 0
        hist = {}
        times = []
        prev2 = None
        prev = vals[0]
        for (t, v) in pts[1:]:
            if _same(v, prev):
                continue
            chg += 1
            times.append(t)
            if _num(v) and _num(prev):
                k = int(round(abs(v - prev) / step))
                hist[k] = hist.get(k, 0) + 1
                if prev2 is not None and _num(prev2) and _same(v, prev2) and k == 1:
                    flap += 1
            else:
                hist["null"] = hist.get("null", 0) + 1
            prev2, prev = prev, v
        dwell = sorted(b - a for a, b in zip(times, times[1:]))
        print("  %-22s %6d %6d %6d  %-26s %10s %10s %8s %7s" % (
            p, chg, flap, chg - flap,
            " ".join("%s:%d" % kv for kv in sorted(hist.items(), key=lambda kv: str(kv[0]).zfill(4))),
            "%.4g" % min(nums) if nums else "-", "%.4g" % max(nums) if nums else "-",
            "%.4g" % (max(nums) - min(nums)) if nums else "-",
            "%.1f s" % dwell[len(dwell) // 2] if dwell else "-"))
    print("\nper quantity (L1, L2 and total paths together)")
    for q in ("power", "current", "voltage", "frequency"):
        chg = flap = 0
        for p, qq in MEASUREMENT.items():
            if qq != q or p not in series:
                continue
            vals = [v for _t, v in series[p]]
            prev2, prev = None, vals[0]
            for v in vals[1:]:
                if _same(v, prev):
                    continue
                chg += 1
                if (prev2 is not None and _num(v) and _num(prev) and _num(prev2)
                        and _same(v, prev2) and int(round(abs(v - prev) / STEP[q])) == 1):
                    flap += 1
                prev2, prev = prev, v
        print("  %-9s changes %5d  flaps %5d (%4.1f%%)  moves %5d" % (
            q, chg, flap, 100.0 * flap / chg if chg else 0, chg - flap))
    l2 = [p for p in MEASUREMENT if p.startswith("/Ac/L2/")]
    print("\nL2 paths, every value seen: " + "; ".join(
        "%s %s" % (p.split("/")[-1], sorted(set(repr(v) for _t, v in series.get(p, []))))
        for p in sorted(l2)))
    other = sorted(p for p in per if p not in MEASUREMENT and p not in ENERGY
                   and p not in DERIVED and p != LAST_UPDATE)
    print("ungated non-energy paths that changed: %s" % (", ".join(other) or "none"))


def _frames(d):
    """State after each signal, as a list of (t, frame dict, changed paths)."""
    state = dict(d["seed"])
    out = []
    for t, _m, items in d["events"]:
        changed = [p for p, v in items.items()
                   if p not in DERIVED and not _same(state.get(p, "\0missing"), v)]
        state.update(items)
        out.append((t, dict(state), changed))
    return out


CAUSES = ("power", "current", "voltage", "frequency", "zero", "energy", "lastupdate", "other")


def replay(d, frames, band):
    """Signals the gate publishes, with the causes of each (a frame may have several)."""
    ref = dict((p, d["seed"].get(p)) for p in MEASUREMENT if p in d["seed"])
    published = 0
    causes = dict((c, 0) for c in CAUSES)
    sole = dict((c, 0) for c in CAUSES)
    for _t, frame, changed in frames:
        why = set()
        for p in changed:
            if p in MEASUREMENT:
                continue
            why.add("energy" if p in ENERGY else "lastupdate" if p == LAST_UPDATE else "other")
        for p, q in MEASUREMENT.items():
            if p not in frame:
                continue
            r, v = ref.get(p, "\0missing"), frame[p]
            if not (_num(r) and _num(v)):
                if not _same(r, v):
                    why.add("other")            # appears, disappears, goes invalid
                continue
            if (v == 0) != (r == 0):
                why.add("zero")
            elif abs(v - r) >= band[q] - EPS:
                why.add(q)
        if why:
            published += 1
            for c in why:
                causes[c] += 1
            if len(why) == 1:
                sole[next(iter(why))] += 1
            for p in MEASUREMENT:
                if p in frame:
                    ref[p] = frame[p]
    return published, causes, sole


def sweep(path, full=False):
    d = _load(path)
    dur = d["duration"]
    _header(d)
    frames = _frames(d)
    print("\n%d frames (one per captured signal), %.3f/s" % (len(frames), len(frames) / dur))
    today = (STEP["power"], STEP["current"], STEP["voltage"], STEP["frequency"])
    grid_p = (5, 10, 15, 20, 30)
    grid_i = (0.05, 0.1, 0.15, 0.2, 0.3)
    grid_v = (0.5, 1.0, 1.5, 2.0)
    grid_f = (0.1, 0.2)
    if full:
        rows = [(p, i, v, f) for p in grid_p for i in grid_i for v in grid_v for f in grid_f]
    else:
        rows = [today]
        rows += [(p, today[1], today[2], today[3]) for p in grid_p[1:]]
        rows += [(today[0], i, today[2], today[3]) for i in grid_i[1:]]
        rows += [(today[0], today[1], v, today[3]) for v in grid_v[1:]]
        rows += [(today[0], today[1], today[2], f) for f in grid_f[1:]]
        # Joint rows: power and current bands kept about equal at ~100-120 V
        # (10 W ~ 0.1 A), since one load change moves both.
        rows += [(p, p / 100.0, v, 0.1) for p in grid_p[1:] for v in (1.0, 1.5)]
        rows += [(p, p / 100.0, 2.0, 0.2) for p in grid_p[1:]]
    seen = set()
    print("\nbands P W / I A / V V / F Hz -> signals/s, then signals/s in which each cause fired"
          "\n(a signal can have several causes; 'sole' = signals with that cause alone)")
    print("  %5s %5s %4s %4s  %7s %6s |" % ("P", "I", "V", "F", "sig/s", "vs now")
          + "".join(" %7s" % c[:7] for c in CAUSES) + " | sole: P / I / V / E+LU")
    base = None
    for r in rows:
        if r in seen:
            continue
        seen.add(r)
        band = {"power": r[0], "current": r[1], "voltage": r[2], "frequency": r[3]}
        n, causes, sole = replay(d, frames, band)
        if base is None:
            base = n
        print("  %5g %5g %4g %4g  %7.3f %5.0f%% |" % (r[0], r[1], r[2], r[3], n / dur,
                                                     100.0 * n / base if base else 0)
              + "".join(" %7.3f" % (causes[c] / dur) for c in CAUSES)
              + " | %.3f / %.3f / %.3f / %.3f" % (
                  sole["power"] / dur, sole["current"] / dur, sole["voltage"] / dur,
                  (sole["energy"] + sole["lastupdate"]) / dur))
        if r == today and n != len([f for f in frames if f[2]]):
            print("  NOTE: today row (%d) differs from frames that changed something (%d)"
                  % (n, len([f for f in frames if f[2]])))


# ------------------------------------------------- replay through real code --

class _CountingService:
    """Just enough of vedbus.VeDbusService: declared paths, ``with svc as
    ctx`` batching, dedup on equal values, one emit per non-empty batch."""

    def __init__(self, paths):
        self.values = dict((p, None) for p in paths)
        self.emits = []             # [(t, {path: value})]
        self.now = 0.0
        self._batch = None

    def __contains__(self, path):
        return path in self.values

    def __setitem__(self, path, value):
        if self.values.get(path) == value:
            return
        self.values[path] = value
        self._batch[path] = value

    def __enter__(self):
        self._batch = {}
        return self

    def __exit__(self, *exc):
        if self._batch:
            self.emits.append((self.now, self._batch))
        self._batch = None
        return None


def _watchdog_data(state, timestamp):
    """A WatchdogData stand-in from captured path values (None reads as 0)."""
    from types import SimpleNamespace

    def leg(prefix, error_code):
        def g(name):
            v = state.get(prefix + name)
            return v if _num(v) else 0.0
        return SimpleNamespace(voltage=g("/Voltage"), current=g("/Current"),
                               power=g("/Power"), energy=g("/Energy/Forward"),
                               frequency=g("/Frequency"), error_code=error_code)

    code = state.get("/ErrorCode") or 0
    has_l2 = state.get("/NrOfPhases") == 2
    return SimpleNamespace(l1=leg("/Ac/L1", code), l2=leg("/Ac/L2", 0),
                           has_l2=has_l2, timestamp=timestamp)


def replay_publisher(path):
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import grid_publisher as gp

    d = _load(path)
    dur = d["duration"]
    _header(d)
    wall = d["wall_minus_mono"]
    rt = (d.get("refresh_time_ms") or 1000) / 1000.0
    paths = (list(gp.GRID_LIVE_PATHS) + list(ENERGY) + list(gp.GRID_ALARM_PATHS)
             + ["/Connected", "/NrOfPhases", "/UpdateIndex", "/ErrorCode",
                "/ErrorMessage", LAST_UPDATE])
    svc = _CountingService(paths)
    pub = gp.GridPublisher()

    def tick(t, state):
        svc.now = t
        pub.publish(svc, _watchdog_data(state, t + wall), state.get("/Connected") == 1)

    # Prime from the seed (not counted), then take prod's /LastUpdate phase.
    state = dict(d["seed"])
    t_seed = d.get("t_seed_mono", d["t0_mono"])
    tick(t_seed, state)
    if _num(state.get(LAST_UPDATE)):
        pub._last_update_published = float(state[LAST_UPDATE])
    svc.emits = []

    ticks = 0
    prev_t = t_seed
    for t, _m, items in d["events"]:
        # Ticks prod ran without signalling: unchanged state, RefreshTime apart.
        k = 1
        while prev_t + k * rt < t - rt / 2:
            tick(prev_t + k * rt, state)
            ticks += 1
            k += 1
        state.update(items)
        tick(t, state)
        ticks += 1
        prev_t = t
    end = d["t0_mono"] + dur
    k = 1
    while prev_t + k * rt < end:
        tick(prev_t + k * rt, state)
        ticks += 1
        k += 1

    n = len(svc.emits)
    print("\nreal grid_publisher bands: P %g W  I %g A  V %g V  F %g Hz" % (
        gp.GRID_POWER_BAND, gp.GRID_CURRENT_BAND, gp.GRID_VOLTAGE_BAND, gp.GRID_FREQ_BAND))
    print("timer ticks fed: %d (%d captured signals, the rest unchanged state)" % (
        ticks, len(d["events"])))
    print("signals: %d = %.3f/s  (capture: %d = %.3f/s, %.0f%%)" % (
        n, n / dur, len(d["events"]), len(d["events"]) / dur,
        100.0 * n / len(d["events"]) if d["events"] else 0))
    carried = {}
    for _t, batch in svc.emits:
        kinds = set()
        for p in batch:
            if p in ENERGY:
                kinds.add("energy")
            elif p == LAST_UPDATE:
                kinds.add("lastupdate")
            elif p in MEASUREMENT:
                kinds.add("measurement")
            elif p not in DERIVED:
                kinds.add("other")
        k = "+".join(sorted(kinds)) or "(UpdateIndex only)"
        carried[k] = carried.get(k, 0) + 1
    print("signals by what they carried:")
    for k, c in sorted(carried.items(), key=lambda kv: -kv[1]):
        print("  %7.3f/s  %5d  %s" % (c / dur, c, k))


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "--report":
        report(a[1])
    elif len(a) >= 2 and a[0] == "--sweep":
        sweep(a[1], full="--full" in a[2:])
    elif len(a) >= 2 and a[0] == "--replay-publisher":
        replay_publisher(a[1])
    else:
        capture(float(a[0]) if a else 60.0)
