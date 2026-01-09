#!/bin/bash
set -e

echo "Uninstalling website blocker..."

# Unload the service
sudo launchctl unload /Library/LaunchDaemons/com.user.websiteblocker.plist 2>/dev/null || true

# Remove files
sudo rm -f /Library/LaunchDaemons/com.user.websiteblocker.plist
sudo rm -f /usr/local/bin/blocker.py

# Ensure sites are unblocked
sudo python3 -c "
import re
hosts = open('/etc/hosts').read()
hosts = re.sub(r'# >>> TIMEBLOCK START <<<.*?# >>> TIMEBLOCK END <<<\n?', '', hosts, flags=re.DOTALL)
open('/etc/hosts', 'w').write(hosts)
" 2>/dev/null || true

echo "Done! Blocker removed."