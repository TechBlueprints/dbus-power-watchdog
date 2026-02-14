# dbus-power-watchdog

Venus OS D-Bus service that reads AC line data from a Hughes Power Watchdog
surge protector via Bluetooth Low Energy.

## Overview

Publishes L1/L2 AC voltage, current, power, energy, and frequency to the
Venus OS D-Bus. Supports role assignment as a grid meter, generator meter,
or PV inverter meter. Settings (role, custom name, position) are persisted
via `com.victronenergy.settings` and survive reboots.

Supports both 30A (single-line) and 50A (dual-line L1+L2) Power Watchdog
models.

## Sensors

- **Line 1**: Voltage (V), Current (A), Power (W), Energy (kWh), Frequency (Hz)
- **Line 2**: Voltage (V), Current (A), Power (W), Energy (kWh), Frequency (Hz)
- **Combined**: Total Power (W), Total Current (A), Average Voltage (V), Total Energy (kWh)
- **Error Code**: 0-9 (see Hughes documentation)

## D-Bus Paths

Registers as `com.victronenergy.grid.power_watchdog` (default) or
`com.victronenergy.genset.power_watchdog` / `com.victronenergy.pvinverter.power_watchdog`
depending on the configured role.

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

## Requirements

- Venus OS (Cerbo GX or similar)
- Hughes Power Watchdog with Bluetooth (PWD/PWS/WD models)
- BLE adapter available on the GX device
- No other BLE clients connected to the Watchdog (single connection only)

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

## Configuration

Copy `config.default.ini` to `config.ini` and edit:

```ini
[DEFAULT]
mac_address = XX:XX:XX:XX:XX:XX
bluetooth_adapter = hci1
update_interval = 5
```

### Finding Your MAC Address

```bash
bluetoothctl scan on
# Look for a device named PMD, PWS, or WD_ followed by a serial number
```

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

## License

Apache License 2.0 - see [LICENSE](LICENSE)
