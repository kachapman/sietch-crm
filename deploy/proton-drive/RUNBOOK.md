# Sietch CRM — Proton Drive CLI install + scheduled sync runbook

**Audience:** operator on the droplet (interactive steps are yours; everything else
can be scripted). The single one-time interactive step is the `proton-drive-cli login`
which needs your Proton password + 2FA TOTP in the same terminal session.

## 0. Pre-flight (read-only)

```bash
# verify the OS
. /etc/os-release && echo "$ID $VERSION_CODENAME"
# verify python3 + rsync + tar exist (we use tar for the JSON+history bundles)
command -v python3 rsync tar
# confirm the data volume path we'll sync
ls -d /app/data/backups || ls -d /opt/sietch/data/backups
# confirm disk headroom — 30 GB history + JSON + delta growth
df -h "$(dirname "$(ls -d /app/data/backups || ls -d /opt/sietch/data/backups)")"
```

## 1. Install `proton-drive-cli` (non-interactive)

Proton distributes the CLI via an apt repo. The page at
`https://proton.me/support/drive-cli` shows the same install snippet:

```bash
# 1a. Add the Proton apt repo + key (1-time, root)
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://repo.proton.me/linux/public_key.asc | \
  sudo gpg --dearmor -o /etc/apt/keyrings/proton.gpg
sudo chmod 0644 /etc/apt/keyrings/proton.gpg

echo "deb [signed-by=/etc/apt/keyrings/proton.gpg] https://repo.proton.me/linux/deb $VERSION_CODENAME main" | \
  sudo tee /etc/apt/sources.list.d/proton-drive.list

# 1b. Install (1-time, root)
sudo apt-get update
sudo apt-get install -y proton-drive-cli
proton-drive-cli --version
```

If your distro isn't in Proton's repo (Ubuntu 22.04/24.04, Debian 12/13
are the supported set), fall back to the direct binary:

```bash
# fallback: grab the latest Linux deb from the GitHub release page
# https://github.com/ProtonMail/proton-drive-cli/releases
arch="$(dpkg --print-architecture)"   # amd64 / arm64
ver="$(curl -fsSL https://api.github.com/repos/ProtonMail/proton-drive-cli/releases/latest \
  | grep -oE 'browser_download_url":\s*"[^"]+\.deb"' | tail -1 \
  | sed -E 's/.*"([^"]+)".*/\1/')"
sudo apt-get install -y "$ver"
proton-drive-cli --version
```

## 2. Create the dedicated service user (1-time, root)

The sync process doesn't need a shell, doesn't need to read anything
outside its config + the backup dir, and shouldn't be the Sietch app
user (which has DB credentials). Use a dedicated low-priv account:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
  --user-group --comment "Sietch Proton Drive sync" sietch-backup
# Make the backup dir owned by the Sietch app user (or root, depending
# on your install) but readable by sietch-backup. Adjust the path:
sudo setfacl -R -m u:sietch-backup:rx /app/data/backups
# Optional: if history-attachments dir is large, prefer the Sietch
# service writing to /app/data/backups then this user only needs
# read+execute (no write to the source).
```

## 3. Install the runbook scripts (1-time, root)

The four unit files and one script live in the repo at
`deploy/proton-drive/`. Copy them into the system locations, then
reload systemd:

```bash
# adjust the repo path to where you cloned sietch-crm
cd /path/to/sietch-crm

sudo install -m 0755 deploy/proton-drive/proton-drive-sync.sh /usr/local/bin/
sudo install -m 0755 deploy/proton-drive/proton-drive-failure-notify.sh /usr/local/bin/

sudo install -m 0644 deploy/proton-drive/sietch-backup-proton-drive-sietch-sync.service \
                    /etc/systemd/system/
sudo install -m 0644 deploy/proton-drive/sietch-backup-proton-drive-sietch-sync.timer \
                    /etc/systemd/system/
sudo install -m 0644 deploy/proton-drive/sietch-backup-proton-drive-sietch-sync-notify.service \
                    /etc/systemd/system/

# Adjust the timer schedule in the .timer file if your export runs
# at a non-midnight wall clock. Default is daily 00:10 UTC.
sudo systemctl edit sietch-backup-proton-drive-sietch-sync.timer   # if you want to tweak

sudo systemctl daemon-reload
```

## 4. Write the per-deployment config (1-time, root)

The systemd units read `/etc/proton-drive/sietch-sync.env`. Mode 0600 —
the file does **not** contain the password (the CLI caches the
session token after the one-time login), only paths and flags.

```bash
sudo install -d -m 0700 -o root -g root /etc/proton-drive
sudo tee /etc/proton-drive/sietch-sync.env >/dev/null <<'EOF'
# Source: where the Sietch app writes its scheduled exports. Default
# for the Docker install is /app/data/backups; for a bare-metal install
# it is wherever the Sietch data volume is bind-mounted.
PROTON_DRIVE_SRC=/app/data/backups

# Destination: a Proton Drive folder. rclone's "protondrive:" remote
# name. The hostname gets appended automatically so multi-host
# backups don't clobber each other. Pick a sensible folder name; the
# # for spaces is required by the shell parser.
PROTON_DRIVE_DST=protondrive:/Sietch/Backups
# (e.g. for a custom folder, change the path after the colon)

# Per-host sub-folder name (so two Sietch instances don't collide on
# the same Proton account). The systemd unit will append this to the
# destination as a sub-folder, e.g. .../Sietch/Backups/<hostname>/.
PROTON_DRIVE_HOSTNAME=$(hostname --long 2>/dev/null || hostname)
PROTON_DRIVE_HOSTNAME=$(echo "$PROTON_DRIVE_HOSTNAME" | tr 'A-Z' 'a-z' | tr '.' '-' | tr -dc 'a-z0-9-')
EOF
sudo chmod 0600 /etc/proton-drive/sietch-sync.env
sudo chown root:root /etc/proton-drive/sietch-sync.env
```

## 5. ONE-TIME interactive login (you must run this)

This is the only step that needs your Proton password + 2FA. Run it
in a tmux/screen session so a dropped connection doesn't abort the
2FA prompt.

```bash
sudo -u sietch-backup \
  HOME=/var/lib/sietch-backup \
  /usr/bin/proton-drive-cli login
#   ↳ follow the interactive prompts:
#      - email
#      - password
#      - 2FA TOTP (from your authenticator app)
#   ↳ on success the CLI writes the session to
#     /var/lib/sietch-backup/.config/proton-drive-cli/session.json
#   ↳ verify with:
proton-drive-cli ls protondrive:/Sietch/Backups/$(hostname)
#   (should list existing folder contents or be empty without error)
```

The session file is readable only by the user that ran login. Because
the .service unit runs as `User=sietch-backup`, the login **must** be
performed as that user, not root. The `sudo -u sietch-backup HOME=…`
prefix is the only way to make the session file land at
`/var/lib/sietch-backup/…` where the .service expects it.

## 6. Enable the timer (1-time, root)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sietch-backup-proton-drive-sietch-sync.timer
sudo systemctl list-timers sietch-backup-proton-drive-sietch-sync.timer

# First sync runs at 00:10 UTC + the systemd-randomized
# AccuracySec=5min delay. To test now:
sudo systemctl start sietch-backup-proton-drive-sietch-sync.service
sudo journalctl -u sietch-backup-proton-drive-sietch-sync.service -n 50
```

## 7. Verify

```bash
# one-shot run logs to the journal
sudo journalctl -u sietch-backup-proton-drive-sietch-sync.service --since "5 min ago"

# expected pattern: INFO "Transferred X.X MB / Y.Y MB" then a final
# "sync complete" line. If you see "ERROR ... sync failed: ..."
# check the link (Proton account, 2FA session, source path perms).

# the destination should now contain the new sub-folder
sudo -u sietch-backup HOME=/var/lib/sietch-backup \
  proton-drive-cli ls "protondrive:/Sietch/Backups/$(cat /etc/proton-drive/sietch-sync.env | awk -F= '/HOSTNAME/ {print $2}')"
```

## 8. Failure notification

The .service unit has `OnFailure=sietch-backup-proton-drive-sietch-sync-notify.service`,
which means: if the .service ever exits non-zero, systemd fires the
matching `notify@<unitname>.service`. That notify unit runs
`/usr/local/bin/proton-drive-failure-notify.sh`, which reads journal
lines for the failing unit, queries the Sietch admin list, and emails
them via the local SMTP relay. To test:

```bash
# force a failure (rename the source to a non-existent path)
sudo sed -i 's,^PROTON_DRIVE_SRC=.*,PROTON_DRIVE_SRC=/nonexistent,' /etc/proton-drive/sietch-sync.env
sudo systemctl start sietch-backup-proton-drive-sietch-sync.service
# 60-90 s later: check the admin inbox
sudo journalctl -u sietch-backup-proton-drive-sietch-sync-notify*.service -n 50
# restore
sudo sed -i 's,^PROTON_DRIVE_SRC=.*,PROTON_DRIVE_SRC=/app/data/backups,' /etc/proton-drive/sietch-sync.env
```

## 9. When you rotate the Proton password or 2FA secret

Re-run step 5 (interactive login). The CLI re-binds the session.
No systemd changes needed. To verify the new session is live:

```bash
sudo systemctl start sietch-backup-proton-drive-sietch-sync.service
sudo journalctl -u sietch-backup-proton-drive-sietch-sync.service -n 20
```
