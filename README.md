# dbus-power-watchdog

Venus OS service that discovers and manages Hughes Power Watchdog surge
protectors via Bluetooth Low Energy.

## Overview

A discovery service scans BLE for Power Watchdog devices and presents them
as toggles in the Venus OS switches pane.  When a device is enabled, a
dedicated child process connects to it via BLE and publishes L1/L2 AC
voltage, current, power, energy, and frequency to the Venus OS D-Bus.

Supports role assignment as a grid meter, generator meter, or PV inverter
meter.  Settings (role, custom name, position) are persisted via
`com.victronenergy.settings` and survive reboots.

Supports both 30A (single-line) and 50A (dual-line L1+L2) Power Watchdog
models, including gen1 (BT-only) and gen2 (WiFi+BT) hardware.

## Architecture

```
dbus-power-watchdog.py          (discovery / management process)
  ├─ Registers as com.victronenergy.switch.power_watchdog
  ├─ Discovery toggle in Venus OS switches pane
  ├─ Per-device enable/disable toggles
  └─ Spawns child processes for enabled devices:
      │
      ├─ power_watchdog_device.py --mac AA:BB:CC:DD:EE:FF
      │    └─ com.victronenergy.grid.power_watchdog_aabbccddeeff
      │
      └─ power_watchdog_device.py --mac 11:22:33:44:55:66
           └─ com.victronenergy.grid.power_watchdog_112233445566
```

## Sensors

- **Line 1**: Voltage (V), Current (A), Power (W), Energy (kWh), Frequency (Hz)
- **Line 2**: Voltage (V), Current (A), Power (W), Energy (kWh), Frequency (Hz)
- **Combined**: Total Power (W), Total Current (A), Average Voltage (V), Total Energy (kWh)
- **Error Code**: 0-9 (see Hughes documentation)

## D-Bus Paths

Each enabled device registers as `com.victronenergy.grid.power_watchdog_{mac}`
(or `genset`/`pvinverter` depending on role).

| Path | Description |
|------|-------------|
| `/Role` | Current role (writable: `grid`, `pvinverter`, `genset`) |
| `/AllowedRoles` | Available roles |
| `/Position` | PV inverter position (writable, only used when role=pvinverter) |
| `/CustomName` | User-defined name (writable, persisted) |
| `/NrOfPhases` | Number of phases (1 or 2, auto-detected) |
| `/RefreshTime` | Measurement interval in milliseconds |
| `/Ac/L1/*` | Line 1 measurements |
| `/Ac/L2/*` | Line 2 measurements (50A models) |
| `/Ac/Power` | Total AC power (W) |
| `/ErrorCode` | Current error code |

## Device Discovery

The service scans BLE for two naming patterns:

| Generation | BLE Name Pattern | Example |
|------------|-----------------|---------|
| Gen2 (WiFi+BT) | `WD_{type}_{serial}` | `WD_E7_26ec4ae469a5` |
| Gen1 (BT-only) | `PM{S\|D}...` (19 chars) | `PMD...` (50A), `PMS...` (30A) |

Scanning handles BLE InProgress errors with retry and adapter rotation.

## Requirements

- Venus OS (Cerbo GX or similar)
- Hughes Power Watchdog with Bluetooth (gen1 or gen2)
- BLE adapter available on the GX device

## Installation

### One-Line Remote Install

```bash
ssh root@<cerbo-ip> "curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-power-watchdog/main/install.sh | bash"
```

### Manual Installation

```bash
ssh root@<cerbo-ip>
cd /data/apps
git clone https://github.com/TechBlueprints/dbus-power-watchdog.git
cd dbus-power-watchdog
bash enable.sh
```

## Usage

1. Install the service (see above)
2. Open the Venus OS Remote Console or VRM
3. Navigate to **Settings > I/O > Switches** (or the device list)
4. Find "Power Watchdog Manager" and enable **Device Discovery**
5. Discovered Power Watchdog devices will appear as toggles
6. Enable a device to start reading AC data
7. The device will appear as a grid meter (or genset/pvinverter after role change)

## Configuration

Optional: copy `config.default.ini` to `config.ini` to customize:

```ini
[DEFAULT]
scan_interval = 60
bluetooth_adapters = hci0,hci1
update_interval = 5
reconnect_delay = 10
reconnect_max_delay = 120
```

All configuration is optional. By default the service auto-detects adapters
and uses sensible defaults.

## Service Management

```bash
svc -u /service/dbus-power-watchdog  # Start
svc -d /service/dbus-power-watchdog  # Stop
svc -t /service/dbus-power-watchdog  # Restart
svstat /service/dbus-power-watchdog  # Status
tail -f /var/log/dbus-power-watchdog/current | tai64nlocal  # Logs
```

## Credits

- BLE protocol based on prior open-source work by
  [spbrogan](https://github.com/spbrogan) and
  [tango2590](https://github.com/tango2590/Hughes-Power-Watchdog)
- Venus OS D-Bus integration patterns from
  [dbus-ble-advertisements](https://github.com/TechBlueprints/dbus-ble-advertisements)

## Third-Party Software

This project includes [velib_python](https://github.com/victronenergy/velib_python)
by Victron Energy BV, located in `ext/velib_python/`. It is licensed under the
MIT License:

> Copyright (c) 2014 Victron Energy BV
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.

The full MIT license text is available at [`ext/velib_python/LICENSE`](ext/velib_python/LICENSE).

## License

Apache License 2.0 - see [LICENSE](LICENSE)
