# Youtube Blocker

Block access to a set of websites to avoid distraction. You may specify which websites you wish to block, and the timings of the block.

## Cofiguration

By altering `block_config.toml` you can change the list of blocked websites, the days of the week duringg which they are blocked, and the times of day during which they are blocked.

My defaults only block YouTube, and block Youtube during weekday working hours, and sleeping hours.

## How to set it up

There are install and uninstall scripts provided. These will need to be run with root access, since they must modify system files and set up launch items.

```bash
sudo bash install.sh
```

Type in password if prompted. To check if the script is functioning correctly, run:

```bash
sudo launchctl list | grep websiteblocker
```

You should see a line with the launch item, and this line should contain a zero, indicating there have been nno errors.

## Uninstall

```bash
sudo bash uninstall.sh
```

If things are still blocked, you can inspect the file by running

```bash
cat /etc/hosts
```

If there is a section with `# >>> TIMEBLOCK START <<<`, manually delete this or run `uv run blocker.py unblock`.

## How it works

The app writes entries to your /etc/hosts file, which intercepts DNS requests, and points them to somewhere useless. The Python script is run by Mac's cron job style launchd daemon. This script checks the current day of the week and time against your provided config, and blocks and unblocks accordingly. The list of blocked websites can also be altered in config.
