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

# Verify critical submodules are present
DEPS_OK=true
for dep in velib_python/vedbus.py bleak/bleak/__init__.py bleak-connection-manager/src/bleak_connection_manager/__init__.py; do
    if [ ! -f "$INSTALL_DIR/ext/$dep" ]; then
        echo "WARNING: Missing dependency ext/$dep"
        DEPS_OK=false
    fi
done
if [ "$DEPS_OK" = false ]; then
    echo "Attempting to initialize submodules..."
    cd "$INSTALL_DIR" && git submodule update --init --recursive 2>/dev/null || true
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
