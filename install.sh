#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing website blocker..."

# Copy the Python script
sudo cp "$SCRIPT_DIR/blocker.py" /usr/local/bin/blocker.py
sudo cp "$SCRIPT_DIR/block_config.toml" /usr/local/bin/block_config.toml
sudo chmod +x /usr/local/bin/blocker.py



# Copy and configure the launchd plist
PYTHON_PATH=$(which python3)

sed "s|__PYTHON_PATH__|$PYTHON_PATH|g" com.user.websiteblocker.plist.template > com.user.websiteblocker.plist

sudo cp com.user.websiteblocker.plist /Library/LaunchDaemons/

sudo cp "$SCRIPT_DIR/com.user.websiteblocker.plist" /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.user.websiteblocker.plist
sudo chmod 644 /Library/LaunchDaemons/com.user.websiteblocker.plist

# Load the service
sudo launchctl load /Library/LaunchDaemons/com.user.websiteblocker.plist

echo "Done! The blocker is now running."
echo "Check status with: sudo launchctl list | grep websiteblocker"