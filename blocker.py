#!/usr/bin/env python3
"""
Website blocker that modifies /etc/hosts based on time of day.
Must be run as root.
"""

import sys
import os
import shutil
from datetime import datetime
from pathlib import Path
import tomllib 

import tomllib  # instead of toml

CONFIG_PATH = Path("/usr/local/bin/block_config.toml")
# CONFIG_PATH = Path("./block_config.toml")

with open(CONFIG_PATH, "rb") as f:  # note: "rb" not "r"
    config = tomllib.load(f)

BLOCKED_DOMAINS = config["blocked_websites"]
BLOCK_DAYS = config["block_days"]
BLOCK_SCHEDULE = config["block_schedule"]

print(BLOCKED_DOMAINS, BLOCK_DAYS, BLOCK_SCHEDULE)

HOSTS_PATH = Path("/etc/hosts")
BACKUP_PATH = Path("/etc/hosts.backup")

# Block marker so we can identify our entries
BLOCK_START = "# >>> TIMEBLOCK START <<<"
BLOCK_END = "# >>> TIMEBLOCK END <<<"

def get_block_entries() -> str:
    """Generate the hosts entries for blocking."""
    lines = [BLOCK_START]
    for domain in BLOCKED_DOMAINS:
        lines.append(f"127.0.0.1    {domain}")
    lines.append(BLOCK_END)
    return "\n".join(lines)

def read_hosts() -> str:
    """Read current hosts file content."""
    return HOSTS_PATH.read_text()

def write_hosts(content: str) -> None:
    """Write content to hosts file with backup."""
    # Create backup if it doesn't exist
    if not BACKUP_PATH.exists():
        shutil.copy(HOSTS_PATH, BACKUP_PATH)
    
    # Write atomically by writing to temp file first
    temp_path = HOSTS_PATH.with_suffix(".tmp")
    temp_path.write_text(content)
    temp_path.replace(HOSTS_PATH)
    
    # Flush DNS cache
    os.system("dscacheutil -flushcache")
    os.system("killall -HUP mDNSResponder")

def remove_block_entries(content: str) -> str:
    """Remove our block entries from hosts content."""
    lines = content.split("\n")
    result = []
    in_block = False
    
    for line in lines:
        if BLOCK_START in line:
            in_block = True
            continue
        if BLOCK_END in line:
            in_block = False
            continue
        if not in_block:
            result.append(line)
    
    return "\n".join(result)

def is_blocked() -> bool:
    """Check if blocking is currently active."""
    return BLOCK_START in read_hosts()

def block() -> None:
    """Add blocking entries to hosts file."""
    if is_blocked():
        print("Already blocked")
        return
    
    content = read_hosts()
    content = content.rstrip() + "\n\n" + get_block_entries() + "\n"
    write_hosts(content)
    print("Blocking enabled")

def unblock() -> None:
    """Remove blocking entries from hosts file."""
    if not is_blocked():
        print("Already unblocked")
        return
    
    content = read_hosts()
    content = remove_block_entries(content)
    write_hosts(content)
    print("Blocking disabled")

def should_block_now(block_days: list[int], block_schedule: list[dict[str, int]]) -> bool:
    """
    Check if current day and time falls within blocking hours.
    """
    current_hour = datetime.now().hour
    current_day = datetime.now().weekday()
    if current_day not in block_days:
        return False
    
    # If any block_schedule item calls for block, we should block
    for block in block_schedule:
        if block["start"] <= current_hour < block["end"]:
            return True
        
    return False

def auto_update() -> None:
    '''Update blocking state based on current time.'''
    if should_block_now(BLOCK_DAYS, BLOCK_SCHEDULE):
        block()
    else:
        unblock()

def main():
    if os.geteuid() != 0:
        print("This script must be run as root (sudo)")
        sys.exit(1)
    
    if len(sys.argv) < 2:
        print("Usage: sudo python3 blocker.py [block|unblock|auto|status]")
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == "block":
        block()
    elif command == "unblock":
        unblock()
    elif command == "status":
        print("Blocked" if is_blocked() else "Not blocked")
    elif command == "auto":
        auto_update()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()