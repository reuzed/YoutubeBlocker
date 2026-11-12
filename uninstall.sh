#!/bin/bash
set -e

LABEL="com.user.websiteblocker"

echo "Uninstalling website blocker..."

# Unload the service (bootout replaces the deprecated `launchctl unload`)
sudo launchctl bootout "system/$LABEL" 2>/dev/null || true

# Remove files
sudo rm -f "/Library/LaunchDaemons/$LABEL.plist"
sudo rm -f /usr/local/bin/blocker.py
sudo rm -f /usr/local/bin/block_config.toml

# Ensure sites are unblocked
sudo /usr/bin/python3 -c "
import re
hosts = open('/etc/hosts').read()
hosts = re.sub(r'# >>> TIMEBLOCK START <<<.*?# >>> TIMEBLOCK END <<<\n?', '', hosts, flags=re.DOTALL)
open('/etc/hosts', 'w').write(hosts)
" 2>/dev/null || true

sudo dscacheutil -flushcache 2>/dev/null || true
sudo killall -HUP mDNSResponder 2>/dev/null || true

echo "Done! Blocker removed."
echo "Logs left in place: /var/log/websiteblocker.log /var/log/websiteblocker.err"
