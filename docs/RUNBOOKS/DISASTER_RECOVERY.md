# Sietch CRM — Disaster Recovery Runbook

**Audience:** operator restoring the CRM from a fresh droplet (or new
machine) using a previously-exported backup set. Pair this with the
Proton Drive runbook at `deploy/proton-drive/RUNBOOK.md` — together
they cover the full "container is on fire, get me a new one" story.

**Time estimate:** 25–45 min for a full restore on a fresh 4 GB+ droplet,
including DNS and TLS cutover. Faster if you only need a recovery drill
(no DNS change).

**Last verified runbook form:** 2026-10-14. Tested against `148.57.182.164.19`
(noble, fresh droplet). If the Sietch CRM internals change
substantially (new env vars, new volume names), update this file in the
same commit.

---

## 0. Prerequisites (one-time per fresh machine)

Gather the following **before** you start the recovery — you'll want
them on a screen or printed out:

| Item | Where it lives | Example |
|---|---|---|
| Proton Drive email + password + 2FA TOTP | your authenticator app | `ken@chapmanfamily.online` |
| Proton Drive root folder for backups | your decision (created during one-time `proton-drive-cli login`) | `Sietch/Backups` |
| `BACKUP_DIR` on the host (the live dump dir) | derived from `docker volume inspect sietch-crm_dashboard-data --format "{{.Mountpoint}}"` then `/backups` | `/var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups` |
| `BACKUP_DIR_HOST` (already in /etc/proton-drive/sietch-sync.env on host) | same path | `/var/lib/docker/volumes/.../backups` |
| The 3 most recent backup sets in Proton Drive | browse the folder (or `proton-drive-cli ls protondrive:/Sietch/Backups/`) | `2026-10-12/`, `2026-10-11/`, ... |
| DB connection string | from the old `.env` on the prior host (you backed it up) or from Proton Drive's `sietch-backup-*.env` archive | `postgresql://sietch:***@db:5432/sietch_crm` |
| Sietch image tag or `docker-compose.yml` commit hash | the GitHub repo's current `main` branch, or a known tag | `d908f75` (the live image as of this runbook) |
| `COOKIE_SECRET` and any other `SECRET_*` env values | the old `.env` (you backed it up) | `COOKIE_SECRET=…` |
| `TELEGRAM_BOT_TOKEN` (if used) | old `.env` or Proton Drive's `*.env` | `7283…` |

**The most likely place these are lost:** you forgot to put `.env` in
Proton Drive. The env file IS in `deploy/proton-drive/RUNBOOK.md`'s
backup scope if you used the runbook's setup steps. If you didn't,
recover it from the previous host's `/app/.env` or `/etc/sietch/.env`
**before destroying** the prior machine.

---

## 1. Provision a fresh droplet

The current production Sietch CRM was Ubuntu noble (24.04) on a
DigitalOcean droplet. To replicate on a new machine:

```bash
# DO API: create a new droplet (e.g. from terminal)
doctl compute droplet create sietch-crm-recovery \
  --region nyc3 \
  --image ubuntu-24-04-x64 \
  --size s-2vcpu-4gb \
  --ssh-keys <your-key-fingerprint> \
  --enable-monitoring

# or use the web UI; the requirements:
#   - Ubuntu 22.04+ (noble recommended)
#   - 2 vCPU, 4 GB+ RAM
#   - SSH key authorized in your DO account
#   - A `sietch-crm` hostname (or whatever you want — set it later)
```

Record the new public IP (`147.182.164.19` in the example below).

---

## 2. Bootstrap the new host (10 min)

From this machine (not the new droplet — your local host with the DO
API token), seed the droplet with the SSH key and fix DNS (some DO
images come with a broken resolver that prevents `apt update`):

```bash
# If the resolver is broken, fix it first via the recovery console:
#   mkdir -p /etc
#   cat > /etc/resolv.conf <<'EOF'
#   nameserver 1.1.1.1
#   nameserver 8.8.8.8
#   nameserver 9.9.9.9
#   options edns0 trust-ad
#   EOF

ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "apt-get update && apt-get install -y ca-certificates curl gnupg"
```

---

## 3. Install Docker + Docker Compose plugin (5 min)

```bash
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "curl -fsSL https://get.docker.com | sh"
# or use the DO droplet agent's docker option, equivalent.
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "docker --version; docker compose version"
```

If you prefer docker-compose-plugin via apt:

```bash
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<'EOF'
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod 0644 /etc/apt/keyrings/docker.gpg
echo "deb [signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu noble stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
EOF
```

---

## 4. Set up the Proton Drive CLI (for the next backup) and pull the latest bundle (5 min)

You have the `deploy/proton-drive/` files already in the repo. They're
small; copy them over:

```bash
# rsync the runbook + unit files (server's job from this repo)
rsync -av deploy/proton-drive/ root@147.182.164.19:/tmp/proton-drive/

# on the new droplet, install the unit files + scripts
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<'EOF'
install -m 0755 /tmp/proton-drive/proton-drive-sync.sh /usr/local/bin/
install -m 0755 /tmp/proton-drive/proton-drive-failure-notify.sh /usr/local/bin/
install -m 0644 /tmp/proton-drive/sietch-backup-proton-drive-sietch-sync.* /etc/systemd/system/
install -d -m 0700 /etc/proton-drive
install -m 0600 /dev/stdin /etc/proton-drive/sietch-sync.env <<ENV
PROTON_DRIVE_SRC=/var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups
PROTON_DRIVE_DST=protondrive:/Sietch/Backups
PROTON_DRIVE_HOSTNAME=sietch-crm-recovery
ENV
useradd --system --no-create-home --shell /usr/sbin/nologin sietch-backup
mkdir -p /var/lib/sietch-backup
chown -R sietch-backup:sietch-backup /var/lib/sietch-backup
systemctl daemon-reload
# Auth requires interactive login; do that on first backup, not now.
# For the recovery, the CLI is only needed to pull the latest archive
# from Proton Drive. Install only:
EOF
# (Login done at step 9 below. Don't run it yet — wait until step 9.)
```

Now manually pull the latest backup set from Proton Drive to the new
droplet's backup dir (single-shot, no CLI needed for a one-off download
to the local fs). You can do this with rclone using protondrive
backend, **or** with the Python rclone client that ships with the
Proton Drive CLI, **or** you can just download the zips through a web
browser from the Proton web app (smallest, laziest path for recovery):

```bash
# on the new host, set up the dir
mkdir -p /var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups
# pull the latest set from Proton Drive (web UI is fine for this one-off)
# OR rclone config + sync -- if you want a one-liner:
apt-get install -y rclone
mkdir -p ~/.config/rclone
cat > ~/.config/rclone/rclone.conf <<'CONF'
[protondrive]
type = protondrive
user = ken@chapmanfamily.online
pass =  # we'll fill this in by running `rclone config reconnect protondrive`
CONF
# then: rclone config reconnect protondrive (interactive 2FA)
rclone copy protondrive:/Sietch/Backups/<YYYY-MM-DD>/ \
  /var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/

# OR simplest path: use the Proton web UI to download the latest
# backup folder, then scp it up.
# scp -r ~/Downloads/Sietch-Backups-<DATE>/ \
#   root@147.182.164.19:/var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/
```

Record the date of the most recent backup set you have (e.g. `2026-10-12`).

---

## 5. Lay down the Sietch image + compose file (5 min)

```bash
# On the new host — clone the repo and build the image
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "git clone https://github.com/chapmanfamily/sietch-crm.git /opt/sietch-crm"
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "cd /opt/sietch-crm && git checkout d908f75"  # or main, or your tag
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "cd /opt/sietch-crm && docker build -t sietch-crm:recover ."
```

(Use the actual commit hash / tag you noted in step 0. If the image
builds with no local Docker cache, it pulls the base image from the
registry, so the step takes 2-3 minutes on a fresh machine.)

---

## 6. Recreate the .env (CRITICAL)

This is the single most failure-prone step — you need every secret the
previous instance had. The Proton Drive bundle should have a snapshot
of `/etc/sietch/.env` or `/app/.env`. If you have it:

```bash
# pull the env file from the most recent backup set
ls /var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/*/files-*/etc/sietch/ 2>/dev/null
# or /app/.env directly
# in any case, copy it onto the new host's .env
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "install -d -m 0755 /opt/sietch-crm/.env-dir"
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "cp /tmp/recovered.env /opt/sietch-crm/.env"
chmod 600 /opt/sietch-crm/.env
```

**Verify every secret the Sietch app expects is present** — the app
crashes on startup if `COOKIE_SECRET`, `DOCS_JWT_SECRET`, `TELEGRAM_BOT_TOKEN`,
or `DB_PASSWORD` is missing:

```bash
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<'EOF'
set -a; . /opt/sietch-crm/.env; set +a
for v in COOKIE_SECRET DOCS_JWT_SECRET DB_PASSWORD TELEGRAM_BOT_TOKEN; do
  if [ -z "${!v}" ]; then echo "MISSING: $v"; else echo "OK:     $v"; fi
done
EOF
```

If any are missing, source them from the previous droplet's
`/etc/sietch/` (if still accessible) or your password manager.

---

## 7. Bring up the database and apply the SQL dump (5 min)

```bash
# Start the database container
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "cd /opt/sietch-crm && docker compose up -d db"
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "cd /opt/sietch-crm && docker compose logs -f db"  # wait for "ready"

# Find the SQL dump from the most recent backup set
# The bundle name is sietch-data-<DATE> (per the per-bundle archive scheme).
# Inside it, the SQL lives alongside the JSON exports.
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<EOF
ls -la /var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/ | tail
EOF
# Get the latest tar:
# (manually find the file, e.g. via the most recent timestamp)
LATEST_TAR=\$(ssh root@147.182.164.19 'ls -t /var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/sietch-data-*.tar.gz | head -1')
echo "Latest archive: \$LATEST_TAR"
# In this bundle scheme the SQL lives at the top of the bundle.
# Look inside the tarball:
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "tar -tzvf \$LATEST_TAR | grep -E 'sietch-backup|psql' | head"
# (We don't have a sql export in the per-bundle layout; the restore path
# is "use the per-bundle JSON exports which include the DB dump via the
# built-in JSON+SQL backup format". The Proton Drive bundle is a *file*
# tar, not a SQL backup. SQL restore is only useful if you have the
# manual pg_dump from step 7a below.)
```

**7a. If you have a `sietch-backup-<DATE>.sql.gz` from the legacy path** (the
   manual one-off pg_dump that used to exist), apply it:

```bash
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<EOF
cd /opt/sietch-crm
docker compose up -d db
docker compose exec -T db psql -U sietch -d sietch_crm -c 'SELECT 1' || \
  docker compose run --rm db psql -U sietch -d sietch_crm < /tmp/legacy-dump.sql.gz
# If dump is gzipped:
zcat /path/to/sietch-backup-DATE.sql.gz | \
  docker compose exec -T db psql -U sietch -d sietch_crm
EOF
```

**7b. If you only have the per-bundle JSON exports** (the standard path
    from 2026-10), use the in-app import:

```bash
# Start the dashboard so the import endpoint is reachable
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 "cd /opt/sietch-crm && docker compose up -d dashboard"
# Wait for the dashboard health check
ssh -i ~/.ssh/id_ed15519 root@147.182.164.19 \
  "until curl -fsS http://localhost:8766/api/health | grep -q ok; do sleep 1; done"

# Now upload the JSON parts via the admin import endpoint.
# You need to be admin. To bootstrap, create a user via the dashboard
# admin (if you have the previous admin's email + a password reset
# method) or via direct SQL:
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<'EOF'
cd /opt/sietch-crm
docker compose exec -T db psql -U sietch -d sietch_crm <<SQL
INSERT INTO users (email, password_hash, password_salt, is_admin, is_active)
VALUES (
  'admin-recover@chapmanfamily.online',
  -- bcrypt hash of a temp password
  '$2b$12$...<paste-bcrypt-hash-of-known-password>...',
  'recover',
  TRUE, TRUE
)
ON CONFLICT (email) DO UPDATE SET is_admin = TRUE, is_active = TRUE;
SQL
EOF
# Now log in via the web UI with that admin + temp password, change the
# password, and run the per-bundle imports through the admin console.
# Each part is one POST /api/v2/admin/import/json request.
for part in db-core files-documents files-state; do
  curl -X POST -H "Cookie: $(cat /tmp/sietch-cookie.txt)" \
    -F "file=@/tmp/sietch-export-$part-DATE.zip" \
    http://localhost:8766/api/v2/admin/import/json
done
```

Sequence resets in the import reset the per-bundle table's serial PKs
so new rows won't collide with restored ones. The Sietch app logs a
"backup_set" payload that lists missing parts; the banner clears as
each part lands.

---

## 8. Bring up the Sietch app, validate (5 min)

```bash
ssh -i ~/.ssh/id_ed15519 root@147.182.164.19 "cd /opt/sietch-crm && docker compose up -d"
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<'EOF'
# Health
docker ps --format 'table {{.Names}}\t{{.Status}}'
curl -sS http://localhost:8766/api/config

# Quick functional check
curl -sS -c /tmp/cookie.txt -X POST -d 'email=admin-recover@chapmanfamily.online&password=<temp>' \
  -H "Content-Type: application/x-www-form-urlencoded" \
  http://localhost:8766/api/v2/auth/login
# Should return a session cookie
EOF
```

If login works, validate the data:

- Open the web UI, navigate to a sample customer / opp, confirm the
  full record renders (names, addresses, custom fields, history)
- Run a small backup (Run Export Now in the admin) and confirm
  `data/backups/` populates with the JSON parts
- Check the backup log: the latest export should show "✅" and
  include the per-part sizes

---

## 9. Wire up Proton Drive backup (3 min)

(You skipped the interactive `proton-drive-cli login` at step 4 — do it
now. The CLI prompts for your 2FA TOTP. Once you have a session
cached, the systemd timer takes over.)

```bash
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 \
  "sudo -u sietch-backup HOME=/var/lib/sietch-backup \
   /usr/bin/proton-drive-cli login"
# ↳ follow the prompts (email, password, 2FA TOTP)
# ↳ on success, the CLI caches the session at
#   /var/lib/sietch-backup/.config/proton-drive-cli/session.json

# Enable the timer (runs daily at 00:10 UTC by default)
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 bash <<'EOF'
systemctl daemon-reload
systemctl enable --now sietch-backup-proton-drive-sietch-sync.timer
systemctl list-timers sietch-backup-proton-drive-sietch-sync.timer
# Trigger one sync now to verify the round-trip
systemctl start sietch-backup-proton-drive-sietch-sync.service
journalctl -u sietch-backup-proton-drive-sietch-sync.service -n 30
EOF
```

Verify the sync ran (look for `Transferred N MB` + `sync complete`):

```bash
ssh -i ~/.ssh/id_ed25519 root@147.182.164.19 \
  "sudo -u sietch-backup HOME=/var/lib/sietch-backup \
   proton-drive-cli ls protondrive:/Sietch/Backups/\$(hostname)/"
```

---

## 10. DNS cutover (only if the old machine is gone)

If the new IP replaces the old one, update DNS:

- DO dashboard → Networking → Domains → select `crm.publicadjustermidwest.com` (or your A record)
- Update the A record to the new IP `147.182.164.19`
- Wait for TTL (5 min default) to flush
- Verify from your local box: `dig +short crm.publicadjustermidwest.com`

If you kept the old machine running and want it to stay up, point the
DNS to the new IP only after you've verified the new machine is fully
operational — keep the old one as a fallback until you trust the new one.

---

## 11. (Optional) Backfill from the running Sietch

Once the new Sietch is up, trigger a fresh export from the admin
console (Run Export Now), wait ~10 min for the history tar (~30 GB
on a real install), and confirm the artifacts landed in
`/var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/`.
The next scheduled Proton sync at 00:10 UTC will pick them up and
push to Proton Drive.

If you want to be aggressive: re-run this on a daily basis for a
week, then retire the prior machine once you're confident in the new
one.

---

## 12. Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| App exits on startup with `KeyError: COOKIE_SECRET` | `.env` missing a key | Restore from backup, re-encrypt in your password manager |
| `docker compose up -d` fails with "port already in use" | Old Sietch still running on a port | `docker ps -a`, kill orphans |
| `psql: connection refused` during import | DB not ready | `docker compose logs -f db`; wait for `ready` |
| `Import failed: missing manifest.json` | Wrong file or corrupted zip | Re-download from Proton Drive; verify zip integrity |
| `Permission denied (publickey)` on `ssh root@...` | Key not authorized on new host | Repeat step 2 — add the SSH key |
| `proton-drive-cli login` says "incorrect password" but creds are correct | Old session still cached; password was rotated | `rm -rf /var/lib/sietch-backup/.config/proton-drive-cli/session.json` and re-login |
| `proton-drive-cli sync` fails silently | Missing dir, env file, or `sietch-backup` user lacks read perms | `chmod -R a+rX /var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups`; verify the env file's `PROTON_DRIVE_SRC` |
| 403 from the Sietch API after restore | Wrong `COOKIE_SECRET` re-derived all the existing cookies | Users re-login; the `COOKIE_SECRET` env value should be the **same** as before the migration |
| Backup log says `Backup already running` | A previous run died with the lock held | Stale-PID bypass in `run_backup` (commit `1be53d3`) handles this — wait 5 min or `pkill -f server.py` on the prior box if still up |
| 33 GB history-attachments tar missing | The data dir didn't get synced (Docker volume not bind-mounted) | Verify `docker inspect sietch-crm_dashboard-data` mounts the right host path; see step 4 |

---

## 13. Post-recovery verification checklist

- [ ] Sietch web UI loads at the new IP / domain
- [ ] `proton-drive-cli ls protondrive:/Sietch/Backups/$(hostname)/` shows the most recent bundle
- [ ] Backup log in admin console shows a recent successful export (the "✅" line)
- [ ] `journalctl -u sietch-backup-proton-drive-sietch-sync.service` shows sync completed
- [ ] `docker compose ps` shows all 3 services (`db`, `dashboard`, optionally `scanner`) in `healthy` state
- [ ] Sietch's per-bundle retention is pruning old archives: `ls /var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/ | wc -l` should be a small number
- [ ] DNS resolves the new IP
- [ ] TLS cert is current (Let's Encrypt may have expired if the old IP is different)

---

## 14. Final note on the JSON export path

The Sietch CRM's `admin console → run export now` button is the
modern, full-coverage restore source — it streams the per-part JSON
exports (db-core, files-documents, files-state) plus the 30 GB
history-attachments tar to `/var/lib/docker/volumes/sietch-crm_dashboard-data/_data/backups/`.
The Proton Drive timer then pulls those into your Drive. The
`/api/v2/admin/import/json` endpoint reads the same parts back.

This runbook assumes the new droplet is set up the same way. If the
internal volume paths or env var names change, update this file
in the same commit.
