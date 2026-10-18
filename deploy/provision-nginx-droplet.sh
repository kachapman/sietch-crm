#!/usr/bin/env bash
# Provision nginx + TLS on the Sietch production droplet.
#
# Run AS ROOT ON THE DROPLET (147.182.164.19) after the compose stack is up:
#
#   scp deploy/nginx-sietch-droplet.conf deploy/provision-nginx-droplet.sh \
#       root@147.182.164.19:/root/
#   ssh root@147.182.164.19 'bash /root/provision-nginx-droplet.sh'
#
# Idempotent: safe to re-run. It will not re-request a certificate that is
# still valid, and it will not clobber a live certificate on renewal.
#
# Requires: the compose stack running with the dashboard on 127.0.0.1:8766
# and the docserver on 127.0.0.1:8080 (see docker-compose.yml), and DNS
# A records for both hostnames pointing at this droplet.
set -euo pipefail

CRM_DOMAIN="crm.publicadjustermidwest.com"
DOCS_DOMAIN="docs.publicadjustermidwest.com"
CERT_NAME="$CRM_DOMAIN"          # one SAN cert covers both
CERTBOT_EMAIL="admin@chapmanfamily.online"
CERTBOT_WEBROOT="/var/www/certbot"
SITE_SRC="${1:-/root/nginx-sietch-droplet.conf}"

say() { echo "[provision-nginx] $*"; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
[ -f "$SITE_SRC" ] || { echo "missing $SITE_SRC" >&2; exit 1; }

# ── packages ────────────────────────────────────────────────────────────────
if ! command -v nginx >/dev/null 2>&1; then
  say "installing nginx + certbot"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq nginx certbot python3-certbot-nginx >/dev/null
fi
systemctl enable --now nginx

# ── upstream reachability ───────────────────────────────────────────────────
for hp in "8766 dashboard" "8080 docserver"; do
  set -- $hp
  if ! curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$1/"; then
    say "WARNING: nothing answering on 127.0.0.1:$1 ($2) — is the stack up?"
  fi
done

# ── ACME webroot + the map idiom for $proxy_connection ─────────────────────
install -d -m 0755 "$CERTBOT_WEBROOT/.well-known/acme-challenge"
cat > /etc/nginx/conf.d/proxy-connection.conf <<'CONF'
map $http_upgrade $proxy_connection {
    default upgrade;
    ''      close;
}
CONF

# ── HTTP-only site files first, so the http-01 challenge can be answered ──
# (the TLS vhosts reference a certificate that does not exist yet)
rm -f /etc/nginx/sites-enabled/default
for d in "$CRM_DOMAIN" "$DOCS_DOMAIN"; do
  cat > "/etc/nginx/sites-available/$d" <<CONF
server {
    listen 80;
    listen [::]:80;
    server_name $d;
    location /.well-known/acme-challenge/ { root $CERTBOT_WEBROOT; }
    location / { proxy_pass http://127.0.0.1:8766; }
}
CONF
  ln -sf "/etc/nginx/sites-available/$d" "/etc/nginx/sites-enabled/$d"
done
nginx -t
systemctl reload nginx
say "HTTP vhosts in place; requesting certificate"

# ── certificate ─────────────────────────────────────────────────────────────
if certbot certificates 2>/dev/null | grep -q "$CERT_NAME"; then
  say "certificate for $CERT_NAME already exists; renewing if close to expiry"
  certbot renew --cert-name "$CERT_NAME" || certbot certonly --webroot -w "$CERTBOT_WEBROOT" \
    -d "$CRM_DOMAIN" -d "$DOCS_DOMAIN" --agree-tos -m "$CERTBOT_EMAIL" --non-interactive
else
  certbot certonly --webroot -w "$CERTBOT_WEBROOT" \
    -d "$CRM_DOMAIN" -d "$DOCS_DOMAIN" \
    --agree-tos -m "$CERTBOT_EMAIL" --non-interactive --keep-until-expiring
fi
say "certificate ready"

# ── real TLS site files ────────────────────────────────────────────────────
say "installing TLS vhosts from $SITE_SRC"
# The repo file holds both vhosts; split it on the marker comment.
python3 - "$SITE_SRC" <<'PY'
import re, sys, pathlib
src = pathlib.Path(sys.argv[1]).read_text()
parts = re.split(r"^# ── (\S+) ──+$", src, flags=re.M)
# parts = [preamble, name1, body1, name2, body2, ...]
out = {}
for i in range(1, len(parts) - 1, 2):
    out[parts[i]] = parts[i + 1]
for name, body in out.items():
    p = pathlib.Path(f"/etc/nginx/sites-available/{name}")
    p.write_text(body.lstrip("\n"))
    print(f"[provision-nginx]   wrote {p}")
PY

nginx -t
systemctl reload nginx
say "nginx reloaded"

# ── reload nginx after a successful renewal ────────────────────────────────
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/nginx-reload.sh <<'HOOK'
#!/bin/sh
systemctl reload nginx
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/nginx-reload.sh

systemctl enable --now certbot.timer
say "done. certbot timer: $(systemctl list-timers certbot.timer --no-pager | sed -n 2p)"
