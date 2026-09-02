# BLE / Bluetooth incident archive — expired 2026-08-27 22:10

Everything below is **closed**. Clint identified the root cause at 22:10 on
2026-08-27; every BLE or Bluetooth connection error recorded before that
moment is expired into this file and is not to be re-investigated,
re-reported, or counted as an open problem.

These were diagnosed during the bleak-connection-manager v1 → v2 migration
of this driver. They are kept because two of them were real defects in this
repo with fixes worth understanding, not because anything here is still
open.

**The root cause itself is not recorded here** — it was found outside this
session and belongs in the fleet's night-watch log. If you are reading this
to understand *why* the errors happened, that is the document to find.

Post-cutoff baseline for this service: pid 3061, up 1957 s, telemetry live
at 225 W, **zero** errors or tracebacks since the 03:58:15Z restart.

---

## 1. Adapter pinned by hciN to a card the device was not on

**2026-08-25, ~02:31–03:13Z. Closed — fixed.**

`config.ini` pinned `bluetooth_adapters = hci1`. The Power Watchdog was
being heard on hci5, a physically different dongle. Two failures compounded:
the BlueZ cache lookup probed `/org/bluez/hci1/dev_24_EC_…`, a path that
could not exist, so it missed every time; and the fallback scan, constrained
to hci1, collided with `dbus-ble-sensors-py`'s discovery and raised
`org.bluez.Error.InProgress` on stop. Net effect: a plugged-in unit that the
service could not see, retrying every ~140 s with a traceback each time.

Fixed by unpinning, then later by pinning correctly — see §6.

## 2. The 21 routed tracebacks

**2026-08-25, ~03:10–03:12Z. Closed — same cause as §1.**

Routed by the night watch as a possible driver defect, with a plausible
alternative theory that hci1 was wedged at the time. Superseded: the scans
named hci1 because §1 pinned them there.

## 3. `notify_activity` crash loop

**2026-08-26, ~23:28–00:07Z. Closed — fixed in 9bee2f1.**

The v2 migration renamed the watchdog stamp method
(`ConnectionWatchdog.notify_activity` → `NotificationWatchdog.record_activity`)
without updating the protocol modules, which reach the watchdog through
`getattr(ble, "_watchdog", None)` — invisible to any grep for the class name.
Every proto test left `_watchdog` unset, so the None-guard skipped the broken
call and the suite stayed green.

In production with the device powered: every frame raised `AttributeError`
before telemetry landed, the exception unwound into dbus-fast's message pump
(logged under *its* name and swallowed), and the raw-frame stamp in the
notify tap kept the 120 s notification watchdog satisfied. The only guard
left was the daemon's 900 s liveness backstop, which killed and respawned the
process every ~15 minutes.

The fix made a structurally valid packet the only liveness signal, contained
proto exceptions under our own logger, and added regression tests that attach
a **real** watchdog through the full parse path — the None-guard is exactly
where this class of bug hides from a green suite.

## 4. 188 `start_notify failed` errors

**2026-08-26, ~21:20Z. Closed — attributed 2026-09-02, stays closed.**

A burst of `start_notify` failures against `0000ff01`. Expired unexplained
at the 22:10 cutoff; the BCM library session later identified the cause:
`[org.freedesktop.DBus.Error.UnknownObject]` from StartNotify after a
reconnect — a stale BlueZ GATT object path. Verified in our own retained
log: **1018 occurrences**, 656 in rotated files and 362 in `current`, and
every one of the 362 falls in a four-hour window:

    2026-08-26 21:00   92
    2026-08-26 22:00  174
    2026-08-26 23:00   90
    2026-08-27 00:00    6
    (nothing after)

They cost retries and log noise, not uptime — the session loop swallowed
them and reconnected.

Two corrections to the account that reached us. First, the errors stopped
on **2026-08-27 ~00:00**, not at the Aug 29 boot they were credited to —
two days earlier, and before the hci2 pin of §6. What stopped them is not
established by this evidence, and is not worth reopening to find out.
Second, BCM's Sep 1 StartNotify retry (deploy 8aeb9de) reaches this service
— confirmed, we run the wrapped client — but it is **preventive here, not
curative**: zero occurrences since Aug 27, so the retry has had nothing to
catch, and its log line `stale GATT object path ... retrying once` has
fired zero times.

Worth one caution if it ever recurs: this was also the error I briefly
miscounted as "764 errors since restart" on 2026-08-27 by grepping the whole
retained log instead of the post-restart window. Scope the grep to the
window before treating a count as current.

## 5. Incident 1 — "power_watchdog restarts silently"

**Open on the watch board since the engagement began. Closed — explained.**

Two distinct things wore the same signature from outside:

- Before 2026-08-26 00:07Z: the §3 crash loop, self-inflicted `os._exit(1)`
  every ~900 s.
- After: nine starts across ~25 h of retained log, **every one** a clean
  external SIGTERM (`Received signal 15`), **zero** `made no progress`
  liveness exits. Fleet restarts and installer runs, not self-kills.

The discriminator is the log line `BLE thread has made no progress for <N>s`
— present means the service killed itself, absent means something stopped it.
Answerable only because log retention went from 100 kB (~3 h) to 30 MB
(~25 h+); at the old depth the evidence spanned less time than the interval
between the failures.

## 6. Card allocation and MAC-named adapters

**2026-08-27, 03:58Z. Closed — not an error, the resolution of §1.**

Clint assigned this service `00:1A:7D:DA:71:07` (hci2 at time of writing),
shared with the two easytouch thermostats — safe because all tenants there
are connect-only, unlike the accept-list-filtering scanner on hci0/hci1.

Config now pins by **MAC**, never by `hciN`, since the index is not an
identity and renumbers across replugs and reboots — the mechanism behind §1.
`resolve_adapter()` translates to the live index at the point of use, because
bleak's `adapter=` accepts nothing else.

## 7. Hourly `No matching connection` bursts

**Closed — attributed elsewhere, never ours.**

Attributed by live D-Bus and HCI capture to `dbus-ble-sensors-py`'s Orion TR
key-provisioner subprocesses, whose cleanup issues `Device1.Disconnect` on
paths that were never connected. Five swept MACs, all appearing in that
service's log alone. Not a dbus-power-watchdog behaviour: this driver has no
hourly timer and addresses exactly one MAC.

---

## Formerly open — resolved 2026-09-02 on Clint's rulings

- **`_grid_bus.close()` from the signal handler** — fixed. Both entry points
  now register SIGTERM/SIGINT with `GLib.unix_signal_add`, so shutdown runs
  as a mainloop source at a dispatch boundary, never from inside
  `dbus_connection_dispatch`. Ruling: "we should fix."
- **Offline poll cadence** — stays 300 s. Ruling: "300 is ok, stick with it."
- **Last-good-data timestamp** — implemented as `/LastUpdate` on the grid
  service: epoch of the last PARSED frame, written at most once per 60 s.
  BCM's notify work does not cover this — its "observed traffic" is
  link-level (a raw notification arriving), which is exactly the signal
  that read healthy during the §3 crash loop while nothing parsed. Only
  the parser knows a frame parsed. `/UpdateIndex` was already truthful
  (gated on real change, so it stood still during §3) but is throttled
  by the flicker-suppression steps; `/LastUpdate` makes it tick at least
  once a minute whenever real frames flow, and never when they do not.
- **`InProgress` → "not found"** — no change. With `ble_wrap_scanner = true`
  our scans hold the card's `.scan` claim, so BCM-aware co-tenants no
  longer collide; only non-participants (Victron's C `dbus-ble-sensors`,
  `bluetoothctl`) can still trigger it. A pinned device does not rotate
  by design — one owner per card — so a collision retries the same card
  after backoff, and since 60bbb39 that is one WARNING per 300 s, not a
  traceback per attempt.
