# YouTube Blocker

YouTube is blocked by default. You get a limited number of unblock sessions, controlled from the terminal with `yt`.

- **Weekdays:** a few windows per day (breakfast, lunch, dinner, evening). Each window allows **one** session of up to 30 minutes. A session ends early if its window closes. Starting a session uses up the window, even if you end it early, so there's no pausing.
- **Weekends:** unblock any time, 1 hour per session, as often as you like.
- **Override:** unblock outside the rules. You must give a reason, and it gets logged.

Every session, refused unblock attempt and tampering event is logged, so you can see how much you actually use.

## Setup (including a new Mac)

```bash
git clone <this repo> && cd YoutubeBlocker
./install.sh
```

Run it as your normal user. It asks for your password once when it needs sudo. It will:
- find a Python 3.11+ outside your home folder, installing `python@3.12` with Homebrew if needed
- install the daemon (launchd, restarted automatically if it dies)
- install the `yt` command and a sudoers rule so `yt` doesn't ask for a password
- remove the old `com.user.websiteblocker` interval blocker if it's there

Re-running `./install.sh` is always safe. Run it again after editing `config.toml`, or to repair a broken install.

## Usage

```bash
yt                # status: blocked/unblocked, time left, today's windows
yt unblock        # start this window's session (or a weekend session)
yt block          # end the session early
yt override "need a tutorial for work" -m 20   # logged; max 120 min
yt log            # today's sessions and events (yt log -d 7 for a week)
yt stats          # minutes per day, last 7 days (yt stats -d 30)
yt doctor         # check the daemon; reloads/restarts it if needed
yt config         # show the installed config
```

## Configuration

Edit `config.toml` in the repo, then re-run `./install.sh`. The installed copy is root-owned on purpose, so changing the rules takes a deliberate step. You can set:
- the windows (name, start, end; they can't cross midnight)
- `session_minutes` for weekday sessions
- `weekend_days` and `weekend_session_minutes`
- the list of blocked domains

## How it works

- `/etc/hosts` gets a marked section pointing YouTube's domains at localhost (IPv4 and IPv6) whenever no session is active.
- A root daemon (`com.user.ytblock`, `KeepAlive`) checks every 20 seconds. It ends sessions on time, and it puts the block back if `/etc/hosts` was edited by hand (logged as `TAMPER`).
- `yt` runs `sudo /usr/local/libexec/ytblock/yt-root`, which the sudoers rule allows without a password. All rules are enforced in that root-owned script.

Files:

| What | Where |
|---|---|
| CLI | `/usr/local/bin/yt` |
| Script + installed config | `/usr/local/libexec/ytblock/` |
| State + usage log (`sessions.jsonl`) | `/var/db/ytblock/` |
| Daemon output | `/var/log/ytblock.log`, `/var/log/ytblock.err` |

**Caveats.** This adds friction; it isn't a lock. You're an admin, so you can always get around it. Also, re-blocking doesn't close a tab that's already open: a video that's already loaded can keep playing after a session ends, so close the tab.

## Uninstall

```bash
./uninstall.sh            # keeps your usage log
./uninstall.sh --purge    # deletes it too
```

If YouTube is somehow still blocked afterwards, check `/etc/hosts` for a `# >>> TIMEBLOCK START <<<` section and delete it.
