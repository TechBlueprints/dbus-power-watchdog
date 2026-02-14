#!/bin/bash
#
# Disable script for dbus-power-watchdog
# Cleanly stops and removes the service
#

INSTALL_DIR="/data/apps/dbus-power-watchdog"
SERVICE_NAME="dbus-power-watchdog"

echo
echo "Disabling $SERVICE_NAME..."

# Remove service symlink
rm -rf "/service/$SERVICE_NAME" 2>/dev/null || true

# Kill any remaining processes
pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "multilog .* /var/log/$SERVICE_NAME" 2>/dev/null || true
pkill -f "python.*$SERVICE_NAME" 2>/dev/null || true

# Remove enable script from rc.local
sed -i "/.*$SERVICE_NAME.*/d" /data/rc.local 2>/dev/null || true

echo "Service stopped and rc.local cleaned"
echo
echo "Note: To completely remove, also delete: $INSTALL_DIR"
echo
