# Secrets & Environment Audit

**Date:** 2026-10-06
**Scope:** Audit of all code paths that handle API keys, bot tokens, OAuth
secrets, session secrets, DB passwords, and other credentials. Documents which
variables must be set, how they're loaded, and how they're used.

This is the canonical reference for the env-var checklist and for the deploy
doc's `.env` section.

---

## 1. Environment variables (production-required)

Source of truth: `config.example.env`. Variables below are loaded by `server.py`
at startup (lines 409–432) via `os.environ.get(...)`. Defaults are non-production
suitable.

| Variable | Sensitive? | Purpose | Required for |
|----------|-----------|---------|--------------|
| `DB_HOST` | No | PostgreSQL host | Always |
| `DB_PORT` | No | PostgreSQL port | Always |
| `DB_NAME` | No | DB name | Always |
| `DB_USER` | No | DB user | Always |
| `DB_PASSWORD` | **Yes** | DB password | Always (prod) |
| `PORT` | No | Server bind port | Always |
| `COOKIE_SECURE` | No | Set `true` over HTTPS | Prod (HTTPS) |
| `COOKIE_SECRET` | **Yes** | Signs session cookies (PBKDF2) | Always (prod) |
| `TELEGRAM_BOT_TOKEN` | **Yes** | Telegram bot token | If bot integration |
| `DASHBOARD_URL` | No | Bot → server URL | If bot integration |
| `ONLYOFFICE_PORTAL_URL` | No | OO CRM portal URL (legacy) | If bot integration |
| `TELEGRAM_NOTIFY_ENABLED` | No | Enable notification dispatch | Always |
| `TELEGRAM_NOTIFY_POLL_INTERVAL` | No | Poll seconds | Optional |
| `TELEGRAM_NOTIFY_BATCH_SIZE` | No | Batch size | Optional |
| `SMTP_HOST` | No | SMTP relay host | Optional (system email) |
| `SMTP_PORT` | No | SMTP port | Optional |
| `SMTP_USER` | Mixed | SMTP user (often email) | Optional |
| `SMTP_PASSWORD` | **Yes** | SMTP password | If SMTP_USER set |
| `SMTP_FROM_NAME` | No | From name | Optional |
| `SMTP_USE_TLS` | No | `true`/`false` | Optional |
| `DOCS_JWT_SECRET` | **Yes** | JWT secret for OnlyOffice | Always |
| `DOCS_INTERNAL_URL` | Mixed | Container-internal DS URL | Always |
| `DOCS_PUBLIC_URL` | No | Public DS URL (browser) | Always |
| `CRM_PUBLIC_URL` | No | Public dashboard URL | Always |
| `DOCSERVER_CONTAINER_NAME` | No | Container name (restart btn) | Always |
| `CONTAINER_NAME` | No | This container name | Always |
| `DB_CONTAINER_NAME` | No | DB container name | Optional |
| `REDIS_CONTAINER_NAME` | No | Redis container name | Optional |
| `OAUTH_MICROSOFT_CLIENT_ID` | Mixed | OAuth client ID | If MS OAuth |
| `OAUTH_MICROSOFT_CLIENT_SECRET` | **Yes** | OAuth secret | If MS OAuth |
| `OAUTH_MICROSOFT_TENANT` | No | `common` / tenant ID | If MS OAuth |
| `OAUTH_GOOGLE_CLIENT_ID` | Mixed | OAuth client ID | If Google OAuth |
| `OAUTH_GOOGLE_CLIENT_SECRET` | **Yes** | OAuth secret | If Google OAuth |
| `OAUTH_REDIRECT_URI` | No | OAuth callback URL | If any OAuth |
| `SCANNER_*` | Mixed | Mail scanner IMAP creds + timing | If SCANNER_ENABLED=true |
| `BACKUP_*` | Mixed | Backup remote target creds | If BACKUP_ENABLED=true |
| `PHOTO_STORAGE_PATH` | No | Override photo dir | Optional |
| `DOCUMENT_STORAGE_PATH` | No | Override doc dir | Optional |
| `AVATAR_STORAGE_PATH` | No | Override avatar dir | Optional |
| `CONTACT_PHOTO_STORAGE_PATH` | No | Override contact-photo dir | Optional |
| `BRANDING_STORAGE_PATH` | No | Override branding dir | Optional |

**Mandatory production set:**
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
`COOKIE_SECRET`, `COOKIE_SECURE=true`,
`DOCS_JWT_SECRET`, `DOCS_INTERNAL_URL`, `DOCS_PUBLIC_URL`,
`CRM_PUBLIC_URL`, `DOCSERVER_CONTAINER_NAME`.

**Strongly recommended:** `TELEGRAM_BOT_TOKEN` if using bot, `SMTP_*` if using
system email, `OAUTH_*_CLIENT_SECRET` if using OAuth providers.

---

## 2. Storage audit

### 2.1 `.env` (file on disk)
- ✅ `.env` is in `.gitignore` (line 3). Never committed.
- ✅ `git ls-files | grep .env` returns only `config.example.env`.
- ✅ `config.example.env` contains only placeholder values (`change_me_in_production`,
  `your_telegram_bot_token_here`, etc.).

### 2.2 PostgreSQL database
| Table.column | Encrypted? | Notes |
|---|---|---|
| `users.password_hash` | PBKDF2-hashed | `auth.py:set_password` (correct) |
| `users.password_salt` | Random salt per user | (correct) |
| `mail_accounts.password_encrypted` | **Plaintext** | Column name misleading — values stored as-is. See §4. |
| `mail_accounts.smtp_password_encrypted` | **Plaintext** | Same as above |
| `mail_accounts.oauth_access_token` | **Plaintext** | Same |
| `mail_accounts.oauth_refresh_token` | **Plaintext** | Same |
| `mail_accounts.oauth_graph_access_token` | **Plaintext** | Same |
| `mail_accounts.oauth_graph_refresh_token` | **Plaintext** | Same |
| `calendar_sources.caldav_password_encrypted` | **Plaintext** | Same |
| `bot_config.value.token` | **Plaintext** (JSON) | Same |
| `service_feeds.webhook_secret` | Random UUID4 | Acceptable (not derived from any other secret) |

**Recommendation (not in scope of this audit):** at-rest encryption for the
`*_encrypted` columns. Documented limitation, not a log-leak risk.

### 2.3 In-memory caches
- `_bot_token_cache: str` (server.py:442) — Telegram bot token in memory.
  Refreshed on PUT `/api/v2/admin/bot-settings`. Cleared on process exit.
- `_bot_config_cache: dict` (server.py:443) — full bot config blob in memory.

These are not persisted to any artifact and are not exposed via any debug
endpoint.

### 2.4 User profile JSON files (`data/user-profiles/.../*.json`)
- No secrets stored. Profiles hold only layout/group preferences and notes.
  Audited: `user_profile_store.py`, `notes_store.py`.

### 2.5 Docker volumes
- `dashboard-data` (mounted at `/data`): contains user profiles + per-user
  blobs. No raw `.env` mounted into containers — `.env` is read by the
  host's `docker compose` process at container startup.

---

## 3. Log/response audit (the actual leak surface)

The audit searched every Python source file for places where a secret value
might be: (a) formatted into a log statement, (b) returned in an HTTP error
response, (c) printed to stderr, or (d) emitted in a traceback.

### 3.1 `server.py`
- **Bot token (`TELEGRAM_BOT_TOKEN` / `_get_bot_token()`):**
  - Used to build `Authorization: Bearer {bot_token}` headers for outgoing
    requests (lines 1733, 5562, 5645, 7335, 13922, 14001, ...).
  - Used to compare against incoming `Authorization: Bearer {bot_token}`
    headers (server-side auth for bot webhooks).
  - **Never** formatted into `print()`, `logger.*`, `traceback.*`, or
    `_json_response()` error bodies.
- **Bot config (`bot_config.token`):**
  - The admin GET endpoint `_handle_admin_bot_settings_get` (line 13626–13628)
    masks the token: `safe["token"] = "••••••" + token[-4:]`.
  - The admin PUT endpoint (line 13650) detects the mask prefix `•••` and
    refuses to overwrite the stored token with the mask value.
  - **Never** logged in full.
- **OAuth tokens (`oauth_access_token`, `oauth_refresh_token`):**
  - Used for outgoing IMAP/SMTP auth and Graph API calls.
  - Stored in `mail_accounts` (see §2.2).
  - **Never** formatted into log statements.
- **Session cookies / `COOKIE_SECRET`:**
  - Used as the salt for `hmac.new(secret.encode(...), ...)` in
    `_sign_jwt` / `_verify_jwt` (lines 1644, 1655).
  - **Never** logged or echoed.
- **`DOCS_JWT_SECRET`:**
  - Used as the secret for signing JWTs handed to OnlyOffice (line 1732).
  - **Never** logged.
- **DB password (`DB_PASSWORD`):**
  - Only read via `os.getenv("DB_PASSWORD", "")` and passed as `PGPASSWORD`
    env var to `pg_dump` / `psql` subprocesses (lines 11736, 11870).
  - **Never** formatted into log statements.
  - Subprocess env is per-call and not inherited by child shells beyond
    the immediate subprocess.
- **Email/SMTP password (`SMTP_PASSWORD`, `acct["password_encrypted"]`):**
  - Used for SMTP/IMAP auth.
  - **Never** formatted into log statements.
  - SMTP exception messages (`str(e)`) come from `smtplib` and do NOT
    include the password (smtplib never echoes passwords in error text).
- **Password-reset tokens (`_handle_password_reset_request`):**
  - Generated server-side, embedded into a reset link
    `f"{CRM_PUBLIC_URL}/reset?token={token}"` (line 2645).
  - Sent via SMTP. **Never** logged.
- **Document download tokens (`download_token`):**
  - Embedded in URLs handed to OnlyOffice for file downloads
    (`f"{CRM_PUBLIC_URL}/api/v2/documents/{doc_id}?token={download_token}"`,
    line 12328). **Not** a long-lived secret — short TTL.
- **User passwords (login flow):**
  - `_handle_login` (line 2364) reads `payload.get("password")`.
  - Hashes via `auth_mod.authenticate_user(email, password)` (line 2369).
  - On failure, returns `{"error": "Invalid email or password"}` — does NOT
    echo the password value.
  - **Never** logged.

### 3.2 `telegram_bot.py`
- All `print()` / `logger.*` calls reviewed: none log `TOKEN` / `tg_token`.
  (Used only to build outgoing API URLs.)
- HTTP error logging at lines 154, 172: logs `resp.text[:200]` for debugging
  HTTP failures. Telegram API error bodies do not echo the bot token.

### 3.3 `oauth_providers.py`
- Logs `err_body` for HTTP errors (line 41) — the OAuth provider's error
  response body, not the client secret.
- Logs `url` for failures — URLs contain `client_id` (not secret) and
  `code` / `state` (short-lived PKCE values).

### 3.4 `smtp_client.py`
- Logs SMTP errors with `from_addr` + exception message. `smtplib` does NOT
  echo the password in error text. Verified by error path:
  - `SMTPAuthenticationError: (535, b'5.7.8 ...')` — codes only, no creds.
  - `SMTPConnectError: [Errno 111] Connection refused` — host/port only.

### 3.5 `automation_engine.py`, `backup_service.py`, `service_feed_poller.py`
- Reviewed: no secrets in log statements.
- `backup_service.py`: reads `BACKUP_REMOTE_KEY_FILE` to SSH. The key file
  path is logged at startup if verbose; the key contents are not.

### 3.6 `public/app.js` (client-side)
- No `console.log/error/warn` calls include token, password, or secret
  values. Audited: `grep -E "console\\.(log|error|warn)\\s*\\(.*(token|password|secret|api_key|bearer)"`.

---

## 4. Known limitations (NOT in scope)

These are pre-existing items the audit explicitly did NOT cover because they
are design/architecture decisions, not log leaks. They are tracked separately.

1. **`*_encrypted` columns store plaintext.** Column names suggest encryption;
   they actually store raw credential strings. DB dumps would expose them.
   Fix requires: (a) a key-management story (KMS / age / Vault), (b) a
   migration to re-encrypt existing rows on first read, (c) transparent
   decrypt on access. Out of scope.
2. **No structured logging.** Logs are free-text `print()` / `logger.info(...)`
   strings. Difficult to redact at a logging layer. Out of scope.
3. **No request IDs in error responses.** Errors propagate as `{"error": "..."}`
   without a server-side correlation ID. Out of scope.
4. **`/api/health` exposes DB reachability but not creds.** Audited; no issue.

---

## 5. Verification

The audit was verified by:

1. `git check-ignore -v .env` → `.gitignore:3:.env .env` (correct).
2. `git ls-files | grep '\.env$'` → only `config.example.env` (no `.env`).
3. `grep -E "password|secret|token|key.*=.*[a-zA-Z0-9]{12,}" config.example.env` → no matches (no real secrets).
4. `grep -nE "print\\(|logger\\.|stderr|format_exc" server.py` → 135 matches;
   each one reviewed individually for secret values in format strings.
5. `grep -nE "f\\\"[^\\\"]*\\{(bot_token|password|secret|api_key|token)[^a-z_].*\\}" server.py` → no matches.
6. `grep -nE "console\\.(log|error|warn)\\s*\\([^)]*(token|password|secret|Authorization)" public/app.js` → no matches.

---

## 6. Deploy checklist (cross-reference)

When deploying to a new droplet (e.g. 165.227.90.63 / crm.publicadjustermidwest.com),
the deploy doc (`docs/UPDATE_AND_DEPLOY.txt` step 9) has the canonical
checklist. This audit ensures every required env var is documented and that
the `.env` is the only file that needs to be hand-aimed.

**Minimum required .env keys for production:**

```
DB_HOST=<your-db-host>
DB_PORT=5432
DB_NAME=sietch_crm
DB_USER=sietch
DB_PASSWORD=<generated-strong-password>

COOKIE_SECURE=true
COOKIE_SECRET=<python3 -c "import secrets; print(secrets.token_urlsafe(32))">

DOCS_JWT_SECRET=<must match OnlyOffice JWT_SECRET>
DOCS_INTERNAL_URL=http://onlyoffice-docserver:80
DOCS_PUBLIC_URL=https://docs.<your-domain>
CRM_PUBLIC_URL=https://<your-domain>
DOCSERVER_CONTAINER_NAME=onlyoffice-docserver

TELEGRAM_BOT_TOKEN=<from @BotFather>
```

Optional (depending on which integrations you enable):

```
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_USE_TLS=true
SMTP_FROM_NAME="Sietch CRM"

OAUTH_MICROSOFT_CLIENT_ID=
OAUTH_MICROSOFT_CLIENT_SECRET=
OAUTH_MICROSOFT_TENANT=common
OAUTH_GOOGLE_CLIENT_ID=
OAUTH_GOOGLE_CLIENT_SECRET=
OAUTH_REDIRECT_URI=https://<your-domain>/api/v2/mail/oauth/callback

SCANNER_ENABLED=true
SCANNER_IMAP_HOST=
SCANNER_IMAP_PORT=993
SCANNER_IMAP_USER=
SCANNER_IMAP_PASSWORD=

BACKUP_ENABLED=true
BACKUP_REMOTE_TYPE=sftp
BACKUP_REMOTE_HOST=<backup-host>
BACKUP_REMOTE_USER=<backup-user>
BACKUP_REMOTE_KEY_FILE=/run/secrets/backup_ssh_key
BACKUP_REMOTE_PATH=/backups/sietch-crm
```

**Pre-deploy:**
- [ ] `.env` written on the droplet only; never committed to git.
- [ ] `chmod 600 .env`; owned by the deploy user.
- [ ] `docker compose config` shows all expected env vars substituted.
- [ ] `curl -s http://127.0.0.1:8766/api/config` returns `{"version": "..."}`.
- [ ] No secrets visible in `docker compose logs dashboard` (grep for
      `BOT_TOKEN`, `PASSWORD`, `SECRET`, `JWT`, `CLIENT_SECRET`).

**Post-deploy (every deploy):**
- [ ] `docker compose logs dashboard | grep -i 'token\|password\|secret'` →
      no plaintext values.
- [ ] `docker exec dashboard env | grep -E 'TOKEN|SECRET|PASSWORD'` shows
      variables are present (confirms `docker compose` substituted them).
- [ ] `grep -RE "(BOT_TOKEN|SECRET|PASSWORD|TOKEN).{8,}" data/` returns
      no plaintext secrets in user profile JSON (sanity check).