#!/bin/bash
# Install (or repair) ytblock. Safe to re-run any time -- e.g. on a new Mac,
# after editing config.toml, or if the daemon has gone missing.
#
#   ./install.sh        (it will sudo when it needs to)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.user.ytblock"
LEGACY_LABEL="com.user.websiteblocker"
LIB_DIR="/usr/local/libexec/ytblock"
DATA_DIR="/var/db/ytblock"
BIN="/usr/local/bin/yt"
PLIST_DEST="/Library/LaunchDaemons/$LABEL.plist"
SUDOERS_DEST="/etc/sudoers.d/ytblock"
LOG_PATH="/var/log/ytblock.log"
ERR_PATH="/var/log/ytblock.err"

# The user who gets password-less `yt`. Works whether run plainly or via sudo.
TARGET_USER="${SUDO_USER:-$USER}"
if [ "$TARGET_USER" = "root" ]; then
    echo "ERROR: run this as your normal user (./install.sh), not from a root shell." >&2
    exit 1
fi

echo "Installing ytblock for user '$TARGET_USER'..."

# ---------------------------------------------------------------------------
# Pick an interpreter the daemon can actually use.
#   1. Needs tomllib, i.e. Python 3.11+ (macOS's /usr/bin/python3 is 3.9).
#   2. Must live outside /Users: a root daemon has no Full Disk Access, so TCC
#      denies reads under a home dir -- including this repo's .venv.
# The first candidate resolves whatever python3 is on PATH back to its *base*
# install, so running this from inside the venv still finds a usable Python.
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
        case "$candidate" in /Users/*) continue ;; esac
        "$candidate" -c 'import tomllib' >/dev/null 2>&1 || continue
        echo "$candidate"
        return 0
    done
    return 1
}

if ! PYTHON_PATH=$(find_python); then
    BREW=$(command -v brew || ls /opt/homebrew/bin/brew /usr/local/bin/brew 2>/dev/null | head -1 || true)
    if [ -n "$BREW" ]; then
        echo "No Python 3.11+ found; installing python@3.12 with Homebrew..."
        if [ "$(id -u)" -eq 0 ]; then
            sudo -u "$TARGET_USER" "$BREW" install python@3.12
        else
            "$BREW" install python@3.12
        fi
    fi
    if ! PYTHON_PATH=$(find_python); then
        echo "ERROR: need Python 3.11+ installed outside /Users." >&2
        echo "Install Homebrew (https://brew.sh), then: brew install python@3.12" >&2
        exit 1
    fi
fi
echo "Using interpreter: $PYTHON_PATH"

# Validate the config before touching anything.
YTBLOCK_DEV=1 YTBLOCK_HOME="$SCRIPT_DIR" "$PYTHON_PATH" -I "$SCRIPT_DIR/ytblock.py" check-config || { echo "ERROR: config.toml is invalid" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Remove the old interval-based blocker, if present.
# ---------------------------------------------------------------------------
if sudo launchctl print "system/$LEGACY_LABEL" >/dev/null 2>&1 || [ -e "/Library/LaunchDaemons/$LEGACY_LABEL.plist" ]; then
    echo "Removing legacy $LEGACY_LABEL..."
    sudo launchctl bootout "system/$LEGACY_LABEL" 2>/dev/null || true
    sudo rm -f "/Library/LaunchDaemons/$LEGACY_LABEL.plist" /usr/local/bin/blocker.py /usr/local/bin/block_config.toml
fi

# ---------------------------------------------------------------------------
# Files. Everything is root-owned and not user-writable: the sudoers rule
# grants root to yt-root, so nothing it runs may be editable without sudo.
# ---------------------------------------------------------------------------
TMP_DIR=$(mktemp -d -t ytblock)
trap 'rm -rf "$TMP_DIR"' EXIT

sudo mkdir -p "$LIB_DIR" "$DATA_DIR" /usr/local/bin
sudo chown root:wheel "$LIB_DIR" "$DATA_DIR"
sudo chmod 755 "$LIB_DIR" "$DATA_DIR"

sudo install -o root -g wheel -m 755 "$SCRIPT_DIR/ytblock.py" "$LIB_DIR/ytblock.py"
sudo install -o root -g wheel -m 644 "$SCRIPT_DIR/config.toml" "$LIB_DIR/config.toml"

cat > "$TMP_DIR/yt-root" <<EOF
#!/bin/sh
exec "$PYTHON_PATH" -I "$LIB_DIR/ytblock.py" "\$@"
EOF
sudo install -o root -g wheel -m 755 "$TMP_DIR/yt-root" "$LIB_DIR/yt-root"

cat > "$TMP_DIR/yt" <<EOF
#!/bin/sh
# ytblock CLI -- see: yt --help
exec sudo "$LIB_DIR/yt-root" "\$@"
EOF
sudo install -o root -g wheel -m 755 "$TMP_DIR/yt" "$BIN"

# Password-less sudo for exactly yt-root. Validate before installing: a broken
# sudoers file can lock you out of sudo entirely.
echo "$TARGET_USER ALL=(root) NOPASSWD: $LIB_DIR/yt-root" > "$TMP_DIR/sudoers"
sudo visudo -cf "$TMP_DIR/sudoers" >/dev/null || { echo "ERROR: generated sudoers rule is invalid" >&2; exit 1; }
sudo install -o root -g wheel -m 440 "$TMP_DIR/sudoers" "$SUDOERS_DEST"

# ---------------------------------------------------------------------------
# launchd daemon (KeepAlive: launchd restarts it if it dies or is killed).
# ---------------------------------------------------------------------------
sed -e "s|__PYTHON_PATH__|$PYTHON_PATH|g" \
    -e "s|__SCRIPT_PATH__|$LIB_DIR/ytblock.py|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    -e "s|__ERR_PATH__|$ERR_PATH|g" \
    "$SCRIPT_DIR/$LABEL.plist.template" > "$TMP_DIR/$LABEL.plist"
plutil -lint "$TMP_DIR/$LABEL.plist" >/dev/null || { echo "ERROR: generated plist is malformed" >&2; exit 1; }

sudo install -o root -g wheel -m 644 "$TMP_DIR/$LABEL.plist" "$PLIST_DEST"
sudo touch "$LOG_PATH" "$ERR_PATH"
sudo chown root:wheel "$LOG_PATH" "$ERR_PATH"

# bootout first so re-runs pick up a new plist; `enable` clears any persistent
# disable flag (the classic "bootstraps fine but never runs" trap).
sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
sudo launchctl enable "system/$LABEL"
sudo launchctl bootstrap system "$PLIST_DEST"

sleep 3
if ! sudo launchctl print "system/$LABEL" 2>/dev/null | grep -q "state = running"; then
    echo
    echo "ERROR: the daemon isn't running." >&2
    echo "--- last 20 lines of $ERR_PATH ---" >&2
    sudo tail -20 "$ERR_PATH" >&2 || true
    exit 1
fi

echo
echo "Done. Daemon is running and YouTube is blocked outside sessions."
echo
"$BIN" status
echo
echo "Try: yt unblock | yt block | yt status | yt stats | yt doctor | yt --help"
