# Deploying to DigitalOcean (dashboard.publicadjustermidwest.com)

This guide covers updating the server after a commit to GitHub.

**Server path:** `/opt/vanguard/onlyoffice-crm-kanban`

For initial server setup (Docker, Nginx, HTTPS certs), see the one-time setup steps archived in docs/. This document assumes the droplet is already provisioned.

---

## DNS at Bluehost (for dashboard.publicadjustermidwest.com)

1. In **Bluehost → Domains → DNS / Zone Editor** for `publicadjustermidwest.com`.
2. Add an A record: **dashboard** → your DigitalOcean droplet public IPv4.

Verify:

```bash
dig +short dashboard.publicadjustermidwest.com
```

---

## Pulling updates from GitHub

On the droplet:

```bash
cd /opt/vanguard/onlyoffice-crm-kanban
chmod +x deploy/update.sh
./deploy/update.sh
```

Or manually:

```bash
cd /opt/vanguard/onlyoffice-crm-kanban
git pull
docker compose build
docker compose up -d
```

---

## Backups

### Pre-deploy snapshot (required before a deploy)

Run **before** `git pull` / `docker compose build`:

```bash
cd /opt/sietch-crm
deploy/pre-deploy-backup.sh            # writes to /mnt/sietch_backup/pre-deploy
deploy/pre-deploy-backup.sh --help     # options and env overrides
DRY_RUN=1 deploy/pre-deploy-backup.sh  # show the plan, write nothing
```

The destination defaults to the **attached 100 GB volume**
(`/mnt/sietch_backup/pre-deploy`), not `/root/Backups` on the root disk. A
full-data tarball is ~27 GB and the root volume is only 77 GB — writing it
there is what took the droplet to 92% on 2026-11-09, and a full disk takes the
CRM down. The script refuses to run if the destination resolves to the root
filesystem or has less than `PRE_DEPLOY_MIN_FREE_GB` (default 35) free.

Layout matches the older ad-hoc backups: `PRE_STATE.txt`, `sietch_crm.dump`
(`pg_dump -Fc`), `code-<sha>.tar.gz`, `dashboard-data.tar.gz`,
`dashboard-uploads.tar.gz`, plus copies of `env`, `docker-compose.yml` and
`Dockerfile` (not tracked in git).

### Ad-hoc volume backup

```bash
docker run --rm -v sietch-crm_dashboard-data:/data -v /mnt/sietch_backup:/backup alpine \
  tar czf /backup/dashboard-data-$(date +%F).tar.gz -C /data .
```

Never write this to `/root` or any path on `/`.

---

## Troubleshooting

| Issue | Check |
|-------|-------|
| 502 / connection refused | `docker compose ps` and `docker compose logs -f` |
| Login works locally but not on subdomain | `COOKIE_SECURE=true` only works over HTTPS; use certbot |
| Settings not saving | `docker compose logs`; ensure `/app/data` volume is writable |
| CRM API errors | `ONLYOFFICE_PORTAL_URL` correct; droplet can reach office.publicadjustermidwest.com |

---

## Security notes

- The app binds to `127.0.0.1:8765` on the host; only Nginx is public.
- Session cookies are HttpOnly; `COOKIE_SECURE=true` in production.
- Do not commit `.env` or `data/` to GitHub.
