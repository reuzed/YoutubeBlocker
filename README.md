# Youtube Blocker

Block access to a set of websites to avoid distraction. You may specify which websites you wish to block, and the timings of the block.

## How to set it up

You will need to point the plist file at your Python path by copying in the result of `which python3`.

```bash
sudo bash install.sh
```

Type in password if prompted.

```bash
sudo launchctl list | grep websiteblocker
```

## How it works

The app writes entries to your /etc/hosts file, which intercepts DNS requests, and points them to somewhere useless. The Python
