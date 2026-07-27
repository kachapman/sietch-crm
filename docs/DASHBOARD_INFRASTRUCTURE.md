# Dashboard Infrastructure & NGINX Reference

**Last updated:** 2026-07-03  
**Applies to:** Production server `ubuntu-webapp1-estimateanalyzer` (`159.89.229.126`)

> **2026-07 reality:** Public traffic for `dashboard.publicadjustermidwest.com` is handled by the host's nginx (systemd), not the Docker `estimate-nginx` container. The active config is `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com`. See the "2026-07 Infrastructure Reality Change" section below.

## Purpose

This document exists so future code agents (and humans) understand how the CRM Kanban Dashboard (`dashboard.publicadjustermidwest.com`) is wired into the shared production server. **Always consult this before modifying nginx, Docker Compose, or the other web apps on this host.** Failure to do so will break the dashboard routing again.

---

## High-level architecture (2026-07)

Public traffic for `dashboard.publicadjustermidwest.com` is handled by the **host's nginx** (systemd service), not a Docker nginx container.

```
Internet
   │
   ▼
ports 80/443 on host (systemd nginx)
   │
   ▼
/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com
   │
   ├──► sietch-crm:8765   (127.0.0.1 only)
   │
   └──► other host services (sherwood-toolbox on 8777, etc.)

systemd: crm-telegram-bot   Telegram customer bot (@vanguardupdates_bot)
                             polls Telegram API, calls dashboard at
                             http://127.0.0.1:8765 (host loopback → container)
```

### Critical facts (2026-07)

1. **Host nginx (systemd) owns ports 80/443 for this domain.** The Docker `estimate-nginx` container is no longer in the path for `dashboard.publicadjustermidwest.com`.
2. **The source-of-truth nginx file for the dashboard domain is now:**
   `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com`
3. **The dashboard container** binds only to `127.0.0.1:8765` and joins `estimate-enhancer_estimate-network` (harmless but not required for routing).
4. **Upload requirements** (`client_max_body_size 100m`, `proxy_request_buffering off`, `proxy_read_timeout 120s`) **must live in the host site file** for this domain.
5. **The old Docker nginx path** (`/opt/estimate-enhancer/nginx.conf` mounted into `estimate-nginx`) is historical for this domain. See the 2026-07 section below.

---

## Compose projects

### 1. Estimate Enhancer (reverse proxy + apps)

Path: `/opt/estimate-enhancer/docker-compose.yml`

```yaml
services:
  app:
    container_name: estimate-enhancer
    networks: [estimate-network]

  iws-calculator:
    container_name: iws-calculator
    networks: [estimate-network]

  nginx:
    container_name: estimate-nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - /var/www/certbot:/var/www/certbot:ro
      # static/upload mounts for estimate-enhancer
    networks: [estimate-network]

networks:
  estimate-network:
    driver: bridge
```

### 2. CRM Kanban Dashboard

Path: `/opt/vanguard/onlyoffice-crm-kanban/docker-compose.yml`

```yaml
services:
  dashboard:
    container_name: sietch-crm
    ports:
      - "127.0.0.1:8765:8765"   # bound to loopback only; external traffic uses nginx proxy
    networks:
      - estimate-network

networks:
  estimate-network:
    external: true
    name: estimate-enhancer_estimate-network
```

### Why this matters (2026-07)

The dashboard still declares the external network so the container can join it if other services on the host need to reach it directly. For public traffic the host nginx now proxies straight to `http://127.0.0.1:8765`.

**Do NOT change the network name** in `docker-compose.yml` without a good reason — it is currently harmless and keeps future options open.

---

## Telegram Bot (crm-telegram-bot)

The Telegram customer bot (`@vanguardupdates_bot`) runs as a **systemd service** on the host, **not** inside Docker. This keeps polling independent of the dashboard container lifecycle. Customer-facing bot text says "Sietch CRM".

| Property | Value |
|----------|-------|
| Service name | `crm-telegram-bot` |
| Unit file | `/etc/systemd/system/crm-telegram-bot.service` (from `docs/crm-telegram-bot.service` in repo) |
| Working dir | `/opt/vanguard/onlyoffice-crm-kanban` |
| Executable | `/tmp/botenv/bin/python3 telegram_bot.py` |
| Python venv | `/tmp/botenv` (created via `python3 -m venv /tmp/botenv`) |
| Dependencies | `python-telegram-bot==22.8`, `httpx` |
| Env vars | Loaded from `.env` in working dir: `TELEGRAM_BOT_TOKEN`, `ONLYOFFICE_PORTAL_URL` |
| Dashboard URL | `http://127.0.0.1:8765` (host loopback → `sietch-crm` container) |
| Auth to dashboard | Bearer token = `TELEGRAM_BOT_TOKEN` (dashboard verifies on bot endpoints) |

### Lifecycle

- **Start/stop:** `systemctl start|stop crm-telegram-bot`
- **Restart after dashboard deploy:** `systemctl restart crm-telegram-bot`
- **Logs:** `journalctl -u crm-telegram-bot -f`
- **Auto-restart:** Enabled (systemd `Restart=always`)
- **Survives reboot:** Yes (systemd `enable`)

### First-time setup

```bash
python3 -m venv /tmp/botenv
/tmp/botenv/bin/pip install python-telegram-bot==22.8 httpx
cp docs/crm-telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crm-telegram-bot
```

### Conflict prevention

Only one bot instance per `TELEGRAM_BOT_TOKEN` can poll Telegram at a time. If you see `409 Conflict` in the logs, kill stale instances and restart:

```bash
pkill -f telegram_bot 2>/dev/null
systemctl restart crm-telegram-bot
```

---

## NGINX config location & reload (2026-07)

| File | Purpose |
|------|---------|
| `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com` | **Source-of-truth** for `dashboard.publicadjustermidwest.com` (host nginx) |
| `/opt/estimate-enhancer/nginx.conf` | Historical (used by `estimate-nginx` for other apps) |

### How to edit (dashboard domain)

1. Edit `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com` on the host.
2. Validate + reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

**Never edit the old Docker-mounted file for this domain** — it is no longer in the request path.

---

## Dashboard nginx server block (host nginx — current)

The following blocks are in the **host** nginx site file for `dashboard.publicadjustermidwest.com` (`/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com`).

```nginx
server {
    listen 80;
    server_name dashboard.publicadjustermidwest.com;

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name dashboard.publicadjustermidwest.com;

    ssl_certificate     /etc/letsencrypt/live/dashboard.publicadjustermidwest.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dashboard.publicadjustermidwest.com/privkey.pem;

    # Required for PDF/image attachments (UploadProgress.ashx) and large notes.
    client_max_body_size 100m;
    proxy_request_buffering off;
    proxy_read_timeout 120s;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Why these settings (2026-07)

- `client_max_body_size 100m` — required for file uploads (attachments on event notes via UploadProgress.ashx). Matches sherwood-toolbox convention on this host.
- `proxy_request_buffering off` — prevents nginx from buffering large uploads before forwarding them to the dashboard proxy (required for multipart).
- `proxy_read_timeout 120s` — prevents upstream timeouts on long-running CRM calls (tags, history, user-profile, large uploads).
- `proxy_pass http://127.0.0.1:8765` — the dashboard container is bound to loopback on the host.

---

## Certificates (2026-07)

Certificates are managed by Certbot on the host at `/etc/letsencrypt` and are used directly by the host nginx (systemd).

| Domain | Certificate path | Managed by |
|--------|------------------|------------|
| `dashboard.publicadjustermidwest.com` | `/etc/letsencrypt/live/dashboard.publicadjustermidwest.com/` | Certbot |
| `dashboard.vanguardadj.com` | `/etc/letsencrypt/live/dashboard.vanguardadj.com/` | Certbot |
| `enhancer.sherwoodestimates.com` | `/etc/letsencrypt/live/enhancer.sherwoodestimates.com/` | Certbot |
| `iwscalc.sherwoodestimates.com` | same as above (SAN) | Certbot |

The old host site `/etc/nginx/sites-enabled/dashboard.vanguardadj.com` (and any `dashboard.vanguardadj.com` references) is legacy. The active site for the current domain is `dashboard.publicadjustermidwest.com`.

---

## Common failure mode (2026-06-25 incident — old world)

**Symptom:** `dashboard.publicadjustermidwest.com` serves the wrong app or shows "not secure."

**Root cause (historical):** The dashboard `server_name` block was missing from `/opt/estimate-enhancer/nginx.conf`. Nginx fell back to the first server block (`enhancer.sherwoodestimates.com`).

**Fix (at the time):** Add the block to `/opt/estimate-enhancer/nginx.conf` and reload `estimate-nginx`.

### Lessons from that era

- Any update that touches the old Docker nginx config must preserve dashboard blocks.
- Always back up before editing.

---

## 2026-07 Infrastructure Reality Change (sherwood-toolbox cutover)

After the sherwood-toolbox deployment, `dashboard.publicadjustermidwest.com` is served directly by the **host's nginx** (systemd service at `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com`). The Docker `estimate-nginx` container and `/opt/estimate-enhancer/nginx.conf` are no longer in the request path for this domain.

### Required upload settings (must live in the host site file)

```nginx
client_max_body_size 100m;
proxy_request_buffering off;
proxy_read_timeout 120s;
```

These are inside the `listen 443` server block for `dashboard.publicadjustermidwest.com`, before the `location /` block.

Without them:
- Attachments (PDFs, images) via `UploadProgress.ashx` fail with 413 "client intended to send too large body".
- Long CRM calls (tags, history, user-profile) can hit upstream timeouts.

### Current container binding

The dashboard container binds only to `127.0.0.1:8765`. Host nginx proxies to that address. The external Docker network declaration is kept for compatibility but is not required for public routing.

### Verification commands (2026-07)

```bash
# Check the active host config for the dashboard domain
sudo nginx -T 2>/dev/null | sed -n '/dashboard.publicadjustermidwest.com/,/^\}/p'

# Confirm the three critical lines are present
sudo nginx -T 2>/dev/null | grep -E 'client_max_body_size|proxy_request_buffering|proxy_read_timeout' | cat

# Basic health
curl -sI https://dashboard.publicadjustermidwest.com/ | head -3
curl -s https://dashboard.publicadjustermidwest.com/api/config | head -c 200

# Container
docker ps --filter name=sietch-crm
curl -I http://127.0.0.1:8765/ 2>&1 | head -5
```

### Update checklist for agents (2026-07)

Before making any nginx/compose change on this server:

1. [ ] Read this document (especially the 2026-07 section).
2. [ ] Identify whether you are editing the **host** site file for `dashboard.publicadjustermidwest.com` or a different app's config.
3. [ ] Back up the target file (`/etc/nginx/sites-enabled/...` or the old `/opt/estimate-enhancer/nginx.conf`).
4. [ ] For dashboard uploads: ensure the three lines (`100m`, `off`, `120s`) are present in the host site file.
5. [ ] `sudo nginx -t && sudo systemctl reload nginx` (or the Docker equivalent for other apps).
6. [ ] Verify `https://dashboard.publicadjustermidwest.com/` still loads.
7. [ ] Test a real attachment upload in the edit modal if you touched upload-related settings.
8. [ ] Verify other host apps (`enhancer.sherwoodestimates.com`, `tools.sherwoodestimates.com`, etc.) still work if you touched shared nginx state.

---



## Document Server (OnlyOffice)

The dashboard embeds OnlyOffice for editing Word/Excel/PowerPoint files. In production, the Document Server runs **co-located in Docker on the same droplet** as the dashboard and PostgreSQL. The dashboard container reaches it via the Docker network (`DOCS_INTERNAL_URL`), while end users reach it via the public URL (`DOCS_PUBLIC_URL`).

See `AGENTS.md` → "Document Server (OnlyOffice) deployment notes" for the full variable table and production rules.

### Document Server verification

```bash
# Healthcheck from inside the dashboard container
docker exec sietch-crm /bin/sh -c 'python3 -c "import urllib.request; print(urllib.request.urlopen(\"http://onlyoffice-docserver:80/healthcheck\", timeout=5).status)"'

# Healthcheck from the host (via public URL / mapped port)
curl -s -k -o /dev/null -w "%{http_code}\n" https://docs.publicadjustermidwest.com/healthcheck

# Editor config contains a plain-string document.key (requires authenticated session)
# curl -s -b "sietch_session=..." https://dashboard.publicadjustermidwest.com/api/v2/documents/1/editor-config | python3 -m json.tool

# CRM must be reachable from Document Server container for downloads/callbacks
docker exec onlyoffice-docserver /bin/sh -c 'curl -s -o /dev/null -w "%{http_code}\n" https://dashboard.publicadjustermidwest.com/api/config'
```

## Verification commands (see also the 2026-07 section above)

```bash
# Host nginx view for the dashboard domain (current)
sudo nginx -T 2>/dev/null | sed -n '/dashboard.publicadjustermidwest.com/,/^\}/p'

# Test HTTPS cert
openssl s_client -connect dashboard.publicadjustermidwest.com:443 -servername dashboard.publicadjustermidwest.com </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer

# Test HTTP → HTTPS redirect
curl -I http://dashboard.publicadjustermidwest.com/

# Test dashboard response
curl -I https://dashboard.publicadjustermidwest.com/
curl -s https://dashboard.publicadjustermidwest.com/api/config | head -c 200

# Container health (localhost)
curl -I http://127.0.0.1:8765/ 2>&1 | head -5
docker ps --filter name=sietch-crm
```
