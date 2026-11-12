#!/usr/bin/env python3
"""
ytblock -- YouTube is blocked by default; you get limited unblock sessions.

Weekdays: each configured window (breakfast, lunch, ...) allows ONE session of
up to `session_minutes`, ending early if the window closes.
Weekend days: unblock any time, `weekend_session_minutes` per session.
Override: any time, needs a reason, always logged.

The same file is both the CLI (`yt ...`, run as root via a sudoers rule) and
the daemon (`ytblock.py daemon`, run by launchd) that ends sessions on time and
re-applies the block if /etc/hosts is edited behind its back.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import pwd
import subprocess
import sys
import time
import tomllib
import traceback
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

# Dev/test mode: env vars below are honoured only when YTBLOCK_DEV=1. sudo's
# env_reset strips them for the real `yt` command, so they can't be used to
# sidestep the rules.
DEV = os.environ.get("YTBLOCK_DEV") == "1"


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ[name]) if DEV and name in os.environ else Path(default)


INSTALL_DIR = _env_path("YTBLOCK_HOME", "/usr/local/libexec/ytblock")
DATA_DIR = _env_path("YTBLOCK_DATA", "/var/db/ytblock")
HOSTS_PATH = _env_path("YTBLOCK_HOSTS", "/etc/hosts")

CONFIG_PATH = INSTALL_DIR / "config.toml"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "sessions.jsonl"
LOCK_PATH = DATA_DIR / "lock"
HEARTBEAT_PATH = DATA_DIR / "heartbeat"

LABEL = "com.user.ytblock"
PLIST_PATH = Path(f"/Library/LaunchDaemons/{LABEL}.plist")
SUDOERS_PATH = Path("/etc/sudoers.d/ytblock")

BLOCK_START = "# >>> TIMEBLOCK START <<<"
BLOCK_END = "# >>> TIMEBLOCK END <<<"

POLL_SECONDS = 20
OVERRIDE_MAX_MINUTES = 120
OVERRIDE_MIN_REASON = 10
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def now_local() -> datetime:
    if DEV and "YTBLOCK_NOW" in os.environ:
        return datetime.fromisoformat(os.environ["YTBLOCK_NOW"])
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def at_minute(d: date, minute: int) -> datetime:
    return datetime.combine(d, dtime.min) + timedelta(minutes=minute)


def hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass
class Window:
    name: str
    start: int  # minutes since midnight
    end: int    # minutes since midnight, exclusive; 24:00 allowed

    def contains(self, minute: int) -> bool:
        return self.start <= minute < self.end


@dataclass
class Config:
    domains: list[str]
    session_minutes: int
    weekend_days: list[int]
    weekend_session_minutes: int
    windows: list[Window]

    def is_weekend(self, d: date) -> bool:
        return d.weekday() in self.weekend_days


def _parse_hhmm(value: str) -> int:
    h, m = value.split(":")
    minute = int(h) * 60 + int(m)
    if not 0 <= minute <= 24 * 60:
        raise ValueError(f"bad time {value!r}")
    return minute


def load_config() -> Config:
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)
    windows = []
    for w in raw.get("windows", []):
        win = Window(w["name"], _parse_hhmm(w["start"]), _parse_hhmm(w["end"]))
        if win.start >= win.end:
            raise ValueError(f"window {win.name!r}: start must be before end (no crossing midnight)")
        windows.append(win)
    names = [w.name for w in windows]
    if len(set(names)) != len(names):
        raise ValueError("window names must be unique")
    windows.sort(key=lambda w: w.start)
    return Config(
        domains=raw["blocked_websites"],
        session_minutes=int(raw.get("session_minutes", 30)),
        weekend_days=list(raw.get("weekend_days", [5, 6])),
        weekend_session_minutes=int(raw.get("weekend_session_minutes", 60)),
        windows=windows,
    )


# --------------------------------------------------------------------------- #
# State, lock, log
# --------------------------------------------------------------------------- #

@contextmanager
def locked():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE_PATH)


def append_log(record: dict) -> None:
    record = {"time": iso(now_local()), **record}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_log() -> list[dict]:
    try:
        lines = LOG_PATH.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def record_day(rec: dict) -> str:
    return (rec.get("start") or rec["time"])[:10]


# --------------------------------------------------------------------------- #
# /etc/hosts
# --------------------------------------------------------------------------- #

def strip_block(content: str) -> str:
    out, inside = [], False
    for line in content.splitlines():
        if line.strip() == BLOCK_START:
            inside = True
            continue
        if line.strip() == BLOCK_END:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def block_section(domains: list[str]) -> str:
    lines = [BLOCK_START]
    for d in domains:
        lines.append(f"127.0.0.1\t{d}")
        lines.append(f"::1\t\t{d}")  # IPv6 too, or AAAA lookups slip through
    lines.append(BLOCK_END)
    return "\n".join(lines)


def hosts_blocked() -> bool:
    return BLOCK_START in HOSTS_PATH.read_text()


def apply_hosts(cfg: Config, blocked: bool) -> bool:
    """Make /etc/hosts match the desired state. Returns True if it changed."""
    current = HOSTS_PATH.read_text()
    desired = strip_block(current)
    if blocked:
        desired = desired.rstrip() + "\n\n" + block_section(cfg.domains) + "\n"
    if desired == current:
        return False
    tmp = HOSTS_PATH.with_name(".hosts.ytblock.tmp")
    tmp.write_text(desired)
    os.chmod(tmp, 0o644)
    tmp.replace(HOSTS_PATH)
    if not DEV:
        subprocess.run(["dscacheutil", "-flushcache"], check=False)
        subprocess.run(["killall", "-HUP", "mDNSResponder"], check=False)
    return True


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def notify(title: str, message: str) -> None:
    """Show a macOS notification to whoever is logged in at the console.

    The daemon runs as root outside any GUI session, so hop into the console
    user's session with `launchctl asuser` and run osascript as them. The first
    one may need allowing under System Settings > Notifications > Script Editor.
    """
    if DEV:
        print(f"[notify] {title}: {message}")
        return
    try:
        user = subprocess.run(["stat", "-f", "%Su", "/dev/console"],
                              capture_output=True, text=True).stdout.strip()
        if not user or user in ("root", "loginwindow"):
            return  # nobody logged in
        uid = pwd.getpwnam(user).pw_uid
        q = lambda t: json.dumps(t, ensure_ascii=False)  # AppleScript-compatible quoting
        script = f'display notification {q(message)} with title {q(title)} sound name "Glass"'
        subprocess.run(["launchctl", "asuser", str(uid), "sudo", "-u", user, "osascript", "-e", script],
                       capture_output=True, timeout=10, check=False)
    except Exception:  # never let a notification failure break blocking
        traceback.print_exc()


def notify_session_over(cfg: Config, rec: dict, now: datetime) -> None:
    what = rec["window"] if rec.get("window") else rec["kind"]
    msg = f"{what}, {rec['minutes']:.0f} min. YouTube is blocked again."
    if not cfg.is_weekend(now.date()):
        nxt = next_window(cfg, now)
        msg += f" Next: {nxt.name} at {hhmm(nxt.start)}." if nxt else " No more windows today."
    notify("YouTube session over", msg)


# --------------------------------------------------------------------------- #
# Core rules
# --------------------------------------------------------------------------- #

def roll_day(state: dict, now: datetime) -> None:
    today = now.date().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["used_windows"] = []


def finish_session(state: dict, now: datetime, ended_by: str) -> dict | None:
    s = state.get("session")
    if not s:
        return None
    start = datetime.fromisoformat(s["start"])
    planned = datetime.fromisoformat(s["planned_end"])
    ended = min(now, planned)
    rec = {
        "event": "session",
        **s,
        "ended": iso(ended),
        "minutes": round((ended - start).total_seconds() / 60, 1),
        "ended_by": ended_by,
    }
    late = (now - planned).total_seconds()
    if late > 3 * POLL_SECONDS:
        # Daemon wasn't running (or the Mac was asleep) when the session should
        # have ended. Record it so it's visible in the log.
        rec["late_minutes"] = round(late / 60, 1)
    append_log(rec)
    state["session"] = None
    return rec


def start_session(state: dict, now: datetime, end: datetime, kind: str,
                  window: str | None = None, reason: str | None = None) -> dict:
    s = {"kind": kind, "start": iso(now), "planned_end": iso(end)}
    if window:
        s["window"] = window
    if reason:
        s["reason"] = reason
    state["session"] = s
    return s


def tick(cfg: Config, state: dict, now: datetime) -> None:
    """Expire sessions and make /etc/hosts match. Call with the lock held."""
    roll_day(state, now)
    s = state.get("session")
    if s and now >= datetime.fromisoformat(s["planned_end"]):
        rec = finish_session(state, now, "expired")
        notify_session_over(cfg, rec, now)

    want_blocked = state.get("session") is None
    if want_blocked and not hosts_blocked() and state.get("last_applied") == "blocked":
        append_log({"event": "tamper", "detail": "block removed from /etc/hosts outside a session"})
    apply_hosts(cfg, want_blocked)
    state["last_applied"] = "blocked" if want_blocked else "unblocked"


def current_window(cfg: Config, now: datetime) -> Window | None:
    m = minute_of_day(now)
    return next((w for w in cfg.windows if w.contains(m)), None)


def next_window(cfg: Config, now: datetime) -> Window | None:
    m = minute_of_day(now)
    return next((w for w in cfg.windows if w.start > m), None)


def session_minutes_today(now: datetime, state: dict) -> float:
    today = now.date().isoformat()
    total = sum(r.get("minutes", 0) for r in read_log()
                if r.get("event") == "session" and record_day(r) == today)
    s = state.get("session")
    if s and s["start"][:10] == today:
        total += (now - datetime.fromisoformat(s["start"])).total_seconds() / 60
    return total


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def describe_session(s: dict, now: datetime) -> str:
    end = datetime.fromisoformat(s["planned_end"])
    left = max(0, math.ceil((end - now).total_seconds() / 60))
    label = s["kind"] + (f" / {s['window']}" if s.get("window") else "")
    return f"UNBLOCKED: {left} min left (until {end:%H:%M}, {label})"


def cmd_status(cfg: Config, state: dict, now: datetime) -> int:
    s = state.get("session")
    print("YouTube: " + (describe_session(s, now) if s else "BLOCKED"))
    print()
    day = DAY_NAMES[now.weekday()]
    if cfg.is_weekend(now.date()):
        print(f"{day} is a weekend day: unblock any time, {cfg.weekend_session_minutes} min per session.")
    else:
        print(f"{day}: one {cfg.session_minutes}-min session per window")
        m = minute_of_day(now)
        used = state.get("used_windows", [])
        width = max((len(w.name) for w in cfg.windows), default=0)
        for w in cfg.windows:
            if w.name in used:
                label = "used"
            elif w.contains(m):
                label = "OPEN NOW: available"
            elif w.end <= m:
                label = "missed"
            else:
                label = "later"
            print(f"  {w.name:<{width}}  {hhmm(w.start)}-{hhmm(w.end)}  {label}")
    print()
    print(f"Unblocked today: {session_minutes_today(now, state):.0f} min")
    return 0


def cmd_unblock(cfg: Config, state: dict, now: datetime) -> int:
    if state.get("session"):
        print("Already " + describe_session(state["session"], now))
        return 0

    if cfg.is_weekend(now.date()):
        end = now + timedelta(minutes=cfg.weekend_session_minutes)
        tomorrow = now.date() + timedelta(days=1)
        if not cfg.is_weekend(tomorrow):
            end = min(end, at_minute(tomorrow, 0))  # don't spill into a weekday
        s = start_session(state, now, end, "weekend")
    else:
        w = current_window(cfg, now)
        if w is None:
            append_log({"event": "denied", "reason": "outside_window"})
            nxt = next_window(cfg, now)
            hint = f"next: {nxt.name} at {hhmm(nxt.start)}" if nxt else "no more windows today"
            print(f"Not in an unblock window ({hint}).")
            return 1
        if w.name in state.get("used_windows", []):
            append_log({"event": "denied", "reason": "window_used", "window": w.name})
            nxt = next_window(cfg, now)
            hint = f"next: {nxt.name} at {hhmm(nxt.start)}" if nxt else "no more windows today"
            print(f"You've already used the {w.name} session ({hint}).")
            return 1
        end = min(now + timedelta(minutes=cfg.session_minutes), at_minute(now.date(), w.end))
        state.setdefault("used_windows", []).append(w.name)
        s = start_session(state, now, end, "window", window=w.name)

    print(describe_session(s, now))
    return 0


def cmd_block(cfg: Config, state: dict, now: datetime) -> int:
    rec = finish_session(state, now, "manual")
    if rec:
        print(f"Blocked. Session ended after {rec['minutes']:.0f} min.")
        if rec.get("window"):
            print(f"(The {rec['window']} window is still used up for today.)")
    else:
        print("Already blocked.")
    return 0


def cmd_override(cfg: Config, state: dict, now: datetime, reason: str, minutes: int) -> int:
    reason = " ".join(reason.split())
    if len(reason) < OVERRIDE_MIN_REASON:
        print(f'Give a real reason (at least {OVERRIDE_MIN_REASON} characters), e.g. yt override "tutorial for work"')
        return 1
    minutes = max(1, min(minutes, OVERRIDE_MAX_MINUTES))
    finish_session(state, now, "replaced_by_override")
    s = start_session(state, now, now + timedelta(minutes=minutes), "override", reason=reason)
    print(f"OVERRIDE logged: {reason!r}")
    print(describe_session(s, now))
    return 0


def cmd_log(days: int) -> int:
    since = (now_local().date() - timedelta(days=days - 1)).isoformat()
    recs = [r for r in read_log() if record_day(r) >= since]
    if not recs:
        print("Nothing logged.")
        return 0
    for r in recs:
        ev = r["event"]
        if ev == "session":
            start = datetime.fromisoformat(r["start"])
            ended = datetime.fromisoformat(r["ended"])
            what = r["kind"] + (f"/{r['window']}" if r.get("window") else "")
            line = f"{start:%a %d %b %H:%M}-{ended:%H:%M}  {r['minutes']:>5.1f} min  {what:<16} {r['ended_by']}"
            if r.get("reason"):
                line += f"  reason: {r['reason']!r}"
            if r.get("late_minutes"):
                line += f"  (ended {r['late_minutes']} min late)"
        elif ev == "denied":
            t = datetime.fromisoformat(r["time"])
            line = f"{t:%a %d %b %H:%M}        denied     {r['reason']}" + (f" ({r['window']})" if r.get("window") else "")
        elif ev == "tamper":
            t = datetime.fromisoformat(r["time"])
            line = f"{t:%a %d %b %H:%M}        TAMPER     {r.get('detail', '')}"
        else:
            line = json.dumps(r)
        print(line)
    return 0


def cmd_stats(days: int, state: dict) -> int:
    now = now_local()
    per_day: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    for r in read_log():
        d = per_day[record_day(r)]
        if r["event"] == "session":
            d["minutes"] += r.get("minutes", 0)
            d["sessions"] += 1
            if r["kind"] == "override":
                d["overrides"] += 1
        elif r["event"] == "denied":
            d["denied"] += 1
        elif r["event"] == "tamper":
            d["tamper"] += 1
    s = state.get("session")
    if s:
        d = per_day[s["start"][:10]]
        d["minutes"] += (now - datetime.fromisoformat(s["start"])).total_seconds() / 60
        d["sessions"] += 1
        if s["kind"] == "override":
            d["overrides"] += 1

    print(f"{'day':<12}{'minutes':>8}{'sessions':>10}{'overrides':>11}{'denied':>8}{'tamper':>8}")
    total = 0.0
    for i in range(days - 1, -1, -1):
        day = now.date() - timedelta(days=i)
        d = per_day.get(day.isoformat(), {})
        total += d.get("minutes", 0)
        print(f"{day:%a %d %b}  {d.get('minutes', 0):>8.0f}{int(d.get('sessions', 0)):>10}"
              f"{int(d.get('overrides', 0)):>11}{int(d.get('denied', 0)):>8}{int(d.get('tamper', 0)):>8}")
    print(f"{'total':<12}{total:>8.0f}   (avg {total / days:.0f} min/day)")
    return 0


def cmd_daemon() -> int:
    print(f"{iso(now_local())} ytblock daemon started (pid {os.getpid()})", flush=True)
    while True:
        try:
            cfg = load_config()  # re-read each tick so config edits take effect
            with locked():
                state = load_state()
                tick(cfg, state, now_local())
                save_state(state)
            HEARTBEAT_PATH.touch()
        except Exception:
            traceback.print_exc()
            sys.stderr.flush()
        time.sleep(POLL_SECONDS)


def cmd_doctor() -> int:
    problems = 0

    def check(ok: bool, good: str, bad: str) -> bool:
        nonlocal problems
        print(("  ok    " if ok else "  FAIL  ") + (good if ok else bad))
        problems += not ok
        return ok

    def launchd_state() -> str | None:
        r = subprocess.run(["launchctl", "print", f"system/{LABEL}"], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if line.strip().startswith("state ="):
                return line.split("=", 1)[1].strip()
        return "unknown"

    print("ytblock doctor")
    st = launchd_state()
    if st is None and PLIST_PATH.exists():
        print("  ..    daemon not loaded; loading it")
        subprocess.run(["launchctl", "enable", f"system/{LABEL}"], check=False)
        subprocess.run(["launchctl", "bootstrap", "system", str(PLIST_PATH)], check=False)
        time.sleep(2)
        st = launchd_state()
    elif st is not None and st != "running":
        print(f"  ..    daemon loaded but {st}; restarting it")
        subprocess.run(["launchctl", "kickstart", "-k", f"system/{LABEL}"], check=False)
        time.sleep(2)
        st = launchd_state()
    check(PLIST_PATH.exists(), f"plist at {PLIST_PATH}", f"plist missing: re-run install.sh from the repo")
    check(st == "running", "daemon running", f"daemon state: {st or 'not loaded'} (see /var/log/ytblock.err)")

    try:
        age = time.time() - HEARTBEAT_PATH.stat().st_mtime
        check(age < 3 * POLL_SECONDS + 5, f"daemon heartbeat {age:.0f}s ago", f"daemon heartbeat stale ({age:.0f}s ago)")
    except FileNotFoundError:
        check(False, "", "no daemon heartbeat yet")

    try:
        load_config()
        check(True, f"config OK ({CONFIG_PATH})", "")
    except Exception as e:  # noqa: BLE001
        check(False, "", f"config error: {e}")

    check(SUDOERS_PATH.exists(), "sudoers rule installed (yt runs without a password)",
          "sudoers rule missing: yt will ask for your password; re-run install.sh")

    state = load_state()
    want_blocked = state.get("session") is None
    check(hosts_blocked() == want_blocked,
          f"/etc/hosts matches state ({'blocked' if want_blocked else 'unblocked'})",
          "/etc/hosts doesn't match state (the daemon should fix this within 20s)")

    print("\nAll good." if not problems else f"\n{problems} problem(s).")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(prog="yt", description="YouTube blocker with limited unblock sessions.")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sub.add_parser("status", help="show block state and today's windows (default)")
    sub.add_parser("unblock", help="start this window's session (or a weekend session)")
    sub.add_parser("block", help="end the current session early")
    o = sub.add_parser("override", help="unblock outside the rules; reason is logged")
    o.add_argument("reason", nargs="+", help="why you need it")
    o.add_argument("-m", "--minutes", type=int, default=30, help=f"length (max {OVERRIDE_MAX_MINUTES})")
    lg = sub.add_parser("log", help="list sessions and events")
    lg.add_argument("-d", "--days", type=int, default=1, help="how many days back (default: today)")
    stp = sub.add_parser("stats", help="daily usage totals")
    stp.add_argument("-d", "--days", type=int, default=7)
    sub.add_parser("config", help="print the installed config")
    sub.add_parser("doctor", help="check (and repair) the daemon")
    sub.add_parser("daemon")
    sub.add_parser("check-config")
    args = p.parse_args()
    cmd = args.cmd or "status"

    if cmd == "check-config":  # used by install.sh; needs no root
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            print(f"config error in {CONFIG_PATH}: {e}", file=sys.stderr)
            return 1
        print(f"config OK: {len(cfg.windows)} windows, {cfg.session_minutes}-min sessions")
        return 0

    if os.geteuid() != 0 and not DEV:
        print("ytblock must run as root. Use the `yt` command (installed by install.sh).")
        return 1

    if cmd == "daemon":
        return cmd_daemon()
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "config":
        print(f"# {CONFIG_PATH}\n" + CONFIG_PATH.read_text())
        return 0
    if cmd == "log":
        return cmd_log(max(1, args.days))

    cfg = load_config()
    with locked():
        state = load_state()
        now = now_local()
        tick(cfg, state, now)
        if cmd == "stats":
            rc = cmd_stats(max(1, args.days), state)
        elif cmd == "status":
            rc = cmd_status(cfg, state, now)
        elif cmd == "unblock":
            rc = cmd_unblock(cfg, state, now)
        elif cmd == "block":
            rc = cmd_block(cfg, state, now)
        elif cmd == "override":
            rc = cmd_override(cfg, state, now, " ".join(args.reason), args.minutes)
        else:
            p.error(f"unknown command {cmd}")
        tick(cfg, state, now)  # apply any change to /etc/hosts right away
        save_state(state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
