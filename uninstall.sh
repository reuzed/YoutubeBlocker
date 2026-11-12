#!/bin/bash
# Remove ytblock. Usage logs in /var/db/ytblock are kept unless you pass --purge.
set -e

LABEL="com.user.ytblock"
LEGACY_LABEL="com.user.websiteblocker"

echo "Uninstalling ytblock..."

for label in "$LABEL" "$LEGACY_LABEL"; do
    sudo launchctl bootout "system/$label" 2>/dev/null || true
    sudo rm -f "/Library/LaunchDaemons/$label.plist"
done

sudo rm -f /usr/local/bin/yt /etc/sudoers.d/ytblock
sudo rm -rf /usr/local/libexec/ytblock
sudo rm -f /usr/local/bin/blocker.py /usr/local/bin/block_config.toml  # legacy

# Remove the block from /etc/hosts
sudo /usr/bin/python3 -c "
import re
hosts = open('/etc/hosts').read()
hosts = re.sub(r'# >>> TIMEBLOCK START <<<.*?# >>> TIMEBLOCK END <<<\n?', '', hosts, flags=re.DOTALL)
open('/etc/hosts', 'w').write(hosts)
"
sudo dscacheutil -flushcache 2>/dev/null || true
sudo killall -HUP mDNSResponder 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
    sudo rm -rf /var/db/ytblock /var/log/ytblock.log /var/log/ytblock.err
    echo "Done. Blocker and all logs removed."
else
    echo "Done. Blocker removed. Usage log kept in /var/db/ytblock (use --purge to delete)."
fi
