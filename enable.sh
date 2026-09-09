#!/bin/bash
#
# Enable script for dbus-power-watchdog
# This script is run on every boot via rc.local to ensure the service is properly set up
#

INSTALL_DIR="/data/apps/dbus-power-watchdog"
SERVICE_NAME="dbus-power-watchdog"

# Fix permissions
chmod +x "$INSTALL_DIR"/*.py
chmod +x "$INSTALL_DIR"/*.sh
chmod +x "$INSTALL_DIR/service/run"
chmod +x "$INSTALL_DIR/service/log/run"

# velib_python is the only dependency this repo vendors; the BLE stack
# comes from the shared /data/bcm install (see install.sh).
if [ ! -f "$INSTALL_DIR/ext/velib_python/vedbus.py" ]; then
    echo "WARNING: Missing dependency ext/velib_python/vedbus.py"
    cd "$INSTALL_DIR" && git submodule update --init ext/velib_python 2>/dev/null || true
fi
if [ ! -d /data/bcm/src/bleak_connection_manager ]; then
    echo "WARNING: shared BLE stack missing (/data/bcm); run install.sh"
fi

# Create rc.local if it doesn't exist
if [ ! -f /data/rc.local ]; then
    echo "#!/bin/bash" > /data/rc.local
    chmod 755 /data/rc.local
fi

# Add enable script to rc.local (runs on every boot)
RC_ENTRY="bash $INSTALL_DIR/enable.sh"
grep -qxF "$RC_ENTRY" /data/rc.local || echo "$RC_ENTRY" >> /data/rc.local

# Create symlink to service directory
if [ -L "/service/$SERVICE_NAME" ]; then
    rm "/service/$SERVICE_NAME"
fi
ln -s "$INSTALL_DIR/service" "/service/$SERVICE_NAME"

echo "$SERVICE_NAME enabled"
