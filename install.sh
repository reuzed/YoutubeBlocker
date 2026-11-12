#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.user.websiteblocker"
PLIST_DEST="/Library/LaunchDaemons/$LABEL.plist"
LOG_PATH="/var/log/websiteblocker.log"
ERR_PATH="/var/log/websiteblocker.err"

echo "Installing website blocker..."

# ---------------------------------------------------------------------------
# Pick an interpreter the daemon can actually use.
#
# Two constraints that are easy to miss:
#   1. It needs tomllib, i.e. Python 3.11+. The system /usr/bin/python3 is 3.9
#      on this machine, so it does NOT qualify.
#   2. It must live outside /Users. A root daemon has no Full Disk Access, so
#      macOS TCC denies it any read under a user's home -- including this
#      project's .venv. Pointing at .venv/bin/python3 makes every run die with
#      "PermissionError: Operation not permitted: .../pyvenv.cfg".
#
# The first candidate resolves whatever python3 is on PATH back to its *base*
# install, so running this from inside the venv still yields the system Python
# the venv was built from.
# ---------------------------------------------------------------------------
find_python() {
    local base_python="" candidate
    if command -v python3 >/dev/null 2>&1; then
        base_python=$(python3 -c 'import sys, os; print(os.path.join(sys.base_prefix, "bin", "python3"))' 2>/dev/null || true)
    fi

    for candidate in \
        "$base_python" \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
        /usr/bin/python3
    do
        [ -n "$candidate" ] && [ -x "$candidate" ] || continue
        case "$candidate" in /Users/*) continue ;; esac          # TCC-blocked for root daemons
        "$candidate" -c 'import tomllib' >/dev/null 2>&1 || continue
        echo "$candidate"
        return 0
    done
    return 1
}

if ! PYTHON_PATH=$(find_python); then
    echo "ERROR: no usable Python found." >&2
    echo "Need a Python 3.11+ (for tomllib) installed outside /Users -- e.g." >&2
    echo "  brew install python@3.12" >&2
    exit 1
fi
echo "Using interpreter: $PYTHON_PATH"

# Copy the Python script and its config
sudo cp "$SCRIPT_DIR/blocker.py" /usr/local/bin/blocker.py
sudo cp "$SCRIPT_DIR/block_config.toml" /usr/local/bin/block_config.toml
sudo chmod +x /usr/local/bin/blocker.py

# ---------------------------------------------------------------------------
# Render the plist into a temp file rather than into the repo. The old version
# wrote the rendered plist to the *current* directory but then copied it a
# second time from "$SCRIPT_DIR" -- so running the script from anywhere but the
# repo root installed a stale, committed plist over the freshly rendered one.
# ---------------------------------------------------------------------------
TMP_PLIST=$(mktemp -t websiteblocker)
trap 'rm -f "$TMP_PLIST"' EXIT

sed -e "s|__PYTHON_PATH__|$PYTHON_PATH|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    -e "s|__ERR_PATH__|$ERR_PATH|g" \
    "$SCRIPT_DIR/$LABEL.plist.template" > "$TMP_PLIST"

plutil -lint "$TMP_PLIST" >/dev/null || { echo "ERROR: generated plist is malformed" >&2; exit 1; }

sudo cp "$TMP_PLIST" "$PLIST_DEST"
sudo chown root:wheel "$PLIST_DEST"
sudo chmod 644 "$PLIST_DEST"

# Log to /var/log, not /tmp: macOS periodically purges /tmp, which is why there
# was no failure history to look at.
sudo touch "$LOG_PATH" "$ERR_PATH"
sudo chown root:wheel "$LOG_PATH" "$ERR_PATH"

# ---------------------------------------------------------------------------
# Load the service. bootout first so re-running this is idempotent; `enable`
# clears any persistent disable flag, which survives independently of the plist
# and is the classic "it bootstraps fine but never runs" trap.
# ---------------------------------------------------------------------------
sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
sudo launchctl enable "system/$LABEL"
sudo launchctl bootstrap system "$PLIST_DEST"

# Force one run now and confirm it exited cleanly, so a broken install fails
# here instead of silently every 5 minutes for months.
sudo launchctl kickstart -k "system/$LABEL"
sleep 3

EXIT_CODE=$(sudo launchctl print "system/$LABEL" 2>/dev/null | awk '/last exit code/ {print $NF; exit}')
if [ "$EXIT_CODE" != "0" ]; then
    echo
    echo "ERROR: the daemon ran but exited with status ${EXIT_CODE:-unknown}." >&2
    echo "--- last 20 lines of $ERR_PATH ---" >&2
    sudo tail -20 "$ERR_PATH" >&2 || true
    exit 1
fi

echo
echo "Done! The blocker is installed and its first run exited cleanly."
echo "Status:  sudo launchctl print system/$LABEL | head -20"
echo "Logs:    sudo tail -f $LOG_PATH $ERR_PATH"
