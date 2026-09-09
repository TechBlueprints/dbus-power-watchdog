#!/bin/bash
#
# Remote installer for dbus-power-watchdog on Venus OS
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-power-watchdog/main/install.sh | bash
#

set -e

REPO_URL="https://github.com/TechBlueprints/dbus-power-watchdog.git"
INSTALL_DIR="/data/apps/dbus-power-watchdog"
SERVICE_NAME="dbus-power-watchdog"

echo "========================================"
echo "Power Watchdog BLE Grid Meter Installer"
echo "========================================"
echo ""

# Check if running on Venus OS
if [ ! -d "/data/apps" ]; then
    echo "Error: /data/apps not found. This script must run on Venus OS."
    exit 1
fi

# Step 1: Ensure git is installed
echo "Step 1: Checking for git..."
if ! command -v git >/dev/null 2>&1; then
    echo "Git not found. Installing git..."
    if ! opkg install git; then
        echo "Error: Failed to install git."
        exit 1
    fi
    echo "Git installed successfully"
else
    echo "Git already installed"
fi
echo ""

# Step 2: Clone or update repository
echo "Step 2: Setting up repository..."
cd /data/apps

NEEDS_RESTART=false

if [ -d "$INSTALL_DIR" ]; then
    echo "Directory exists: $INSTALL_DIR"
    cd "$INSTALL_DIR"

    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true

    if [ -d .git ]; then
        CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
        if [ "$CURRENT_REMOTE" != "$REPO_URL" ]; then
            echo "Updating remote URL to $REPO_URL..."
            git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL"
        fi

        echo "Fetching latest changes..."
        git fetch origin

        LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "none")
        REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "none")

        if [ "$LOCAL" != "$REMOTE" ]; then
            echo "Updates available. Resetting to latest..."
            git checkout main 2>/dev/null || git checkout -b main origin/main
            git reset --hard origin/main
            NEEDS_RESTART=true
            echo "Repository updated to latest"
        else
            echo "Already up to date"
        fi
    else
        echo "Not a git repository. Converting..."
        git init
        git remote add origin "$REPO_URL"
        git fetch origin
        git checkout -b main origin/main 2>/dev/null || git checkout main
        git reset --hard origin/main
        git branch --set-upstream-to=origin/main main 2>/dev/null || true
        NEEDS_RESTART=true
        echo "Converted to git repository and updated to latest"
    fi
else
    echo "Directory does not exist. Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    echo "Repository cloned"
fi
echo ""

# Step 3: Initialize velib_python (the only thing this repo still vendors)
echo "Step 3: Setting up velib_python..."
cd "$INSTALL_DIR"

git submodule update --init ext/velib_python 2>/dev/null || {
    echo "Submodule init failed, cloning velib_python directly..."
    mkdir -p ext
    if [ ! -e "ext/velib_python/.git" ]; then
        rm -rf ext/velib_python 2>/dev/null
        git clone https://github.com/victronenergy/velib_python.git ext/velib_python \
            2>/dev/null || echo "  WARNING: Failed to clone velib_python"
    fi
}

if [ -f "ext/velib_python/vedbus.py" ]; then
    echo "velib_python verified"
else
    echo "WARNING: ext/velib_python/vedbus.py missing; the service will not start."
fi
echo ""

# Step 3b: Converge the shared bleak-connection-manager install.
#
# This repo vendors NO part of the BLE stack -- this is where all of it
# comes from. One checkout under /data/bcm serves bleak, bleak-retry-connector
# and bleak-connection-manager to every BLE service on the box, and
# the service puts that checkout on sys.path itself (ble_stack.py), so nothing
# here can pin the
# fleet to a private version (and the tests cannot drift onto a different
# bleak than production runs). Without it the service has no bleak at all,
# so a failure here is fatal rather than a degradation.
#
# Idempotent, and safe when another consumer already installed it: --ff-only
# means a stale installer can never move the fleet backwards.  --autowire is
# deliberately NOT passed -- planting the sitewide import hook is a per-box
# decision for the operator, not something a consumer installer makes for them.
echo "Step 3b: Converging the shared BLE stack (/data/bcm)..."
BCM_DIR=/data/bcm
BCM_REPO="https://github.com/TechBlueprints/bleak-connection-manager"
BCM_OK=true
BCM_BEFORE=$(git -C "$BCM_DIR" rev-parse HEAD 2>/dev/null || echo "none")

if [ -d "$BCM_DIR/.git" ]; then
    git -C "$BCM_DIR" fetch -q origin && \
        git -C "$BCM_DIR" merge -q --ff-only origin/main || BCM_OK=false
else
    git clone -q "$BCM_REPO" "$BCM_DIR" || BCM_OK=false
fi

if [ "$BCM_OK" = true ]; then
    # Unpiped on purpose: piping through tail/tee eats the exit code, and a
    # failed smoke import must surface as an installer failure -- the library's
    # install.sh does not advance on one, and prints the rollback command.
    "$BCM_DIR/install.sh"
    if [ $? -ne 0 ]; then
        BCM_OK=false
    fi
fi

if [ "$BCM_OK" = true ]; then
    BCM_AFTER=$(git -C "$BCM_DIR" rev-parse HEAD 2>/dev/null || echo "none")
    echo "Shared BLE stack ready ($(git -C "$BCM_DIR" rev-parse --short HEAD))"
    # The service loads the shared stack at import time, so a moved checkout
    # only takes effect on restart -- and the repo itself may be unchanged.
    if [ "$BCM_BEFORE" != "$BCM_AFTER" ]; then
        echo "Shared BLE stack moved ${BCM_BEFORE:-none} -> $BCM_AFTER; service restart required"
        NEEDS_RESTART=true
    fi
else
    echo ""
    echo "ERROR: could not converge the shared BLE stack at $BCM_DIR."
    echo "Nothing else provides bleak: this repo vendors none of it, so the"
    echo "service cannot start until this succeeds. Check connectivity and"
    echo "re-run; the install is idempotent and resumes from partial state."
    exit 1
fi
echo ""

# Step 4: Run enable script
echo "Step 4: Enabling service..."
bash "$INSTALL_DIR/enable.sh"
echo ""

# Step 5: Start or restart service
#
# Pre-restart check (shared-stack consumer contract, rule 7): the launcher
# is whatever /service/<name>/run RESOLVES to on this box -- not the copy in
# the repo -- and a leftover exec of the retired /data/bcm/python3 shim there
# would make the service look migrated (ensure_ble_stack answers "provided")
# while still riding the shim.  Print its exec line; stop on shim residue.
echo "Step 5: Starting service..."
RUN_FILE=$(readlink -f "/service/$SERVICE_NAME/run" 2>/dev/null || echo "")
if [ -z "$RUN_FILE" ] || [ ! -f "$RUN_FILE" ]; then
    echo "ERROR: /service/$SERVICE_NAME/run does not resolve to a file"
    exit 1
fi
echo "Launcher: $RUN_FILE"
grep -E '^[[:space:]]*exec[[:space:]]+[^2]' "$RUN_FILE" | sed 's/^/  /'
# Comments do not exec anything: a launcher that explains it no longer uses
# the shim must not be refused for saying so.
grep -vE '^[[:space:]]*#' "$RUN_FILE" | grep -nE 'bcm[^[:space:]"'"'"']*/python3|BCM_PY' | sed 's/^/  shim residue: /'
# Same pattern BCM's own installer uses to decide whether the shim is still
# needed on this box: a literal /data/bcm/python3, BCM_PY, or a path built
# from ${BCM_ROOT:-/data/bcm}.
if grep -vE '^[[:space:]]*#' "$RUN_FILE" | grep -qE 'bcm[^[:space:]"'"'"']*/python3|BCM_PY|PYTHONPATH'; then
    echo ""
    echo "ERROR: $RUN_FILE still references the retired interpreter shim."
    echo "The service finds the shared BLE stack itself (ble_stack.py); the"
    echo "launcher must exec plain python3.  Not restarting."
    exit 1
fi
if svstat "/service/$SERVICE_NAME" 2>/dev/null | grep -q ": up "; then
    if [ "$NEEDS_RESTART" = true ]; then
        echo "Restarting service to apply updates..."
        svc -t "/service/$SERVICE_NAME"
        sleep 2
        echo "Service restarted"
    else
        echo "Service already running, no updates needed"
    fi
else
    svc -u "/service/$SERVICE_NAME"
    sleep 2
    echo "Service started"
fi

echo ""
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo ""
echo "Service status:"
svstat "/service/$SERVICE_NAME"
echo ""
echo "View logs:"
echo "  tail -f /var/log/$SERVICE_NAME/current"
echo ""
echo "Service management:"
echo "  svc -u /service/$SERVICE_NAME  # Start"
echo "  svc -d /service/$SERVICE_NAME  # Stop"
echo "  svc -t /service/$SERVICE_NAME  # Restart"
echo ""
