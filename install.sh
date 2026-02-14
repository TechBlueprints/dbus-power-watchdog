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

# Step 3: Run enable script
echo "Step 3: Enabling service..."
bash "$INSTALL_DIR/enable.sh"
echo ""

# Step 4: Start or restart service
echo "Step 4: Starting service..."
if svstat "/service/$SERVICE_NAME" 2>/dev/null | grep -q "up"; then
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
