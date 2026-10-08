# AGENTS.md — Sietch CRM (new-crm branch)

> **Server-wide reference:** `/home/chapman-server/Projects/CHAPMANSERVER_WIREDOOR.md` — how services on this machine are exposed to the internet (Wiredoor tunnel, public hostnames/ports, credentials, troubleshooting). Read it before touching networking, tunnels, or public ports.

> **Session history:** For the rolling "last session summary" archive (one entry per session, oldest at the end) see [`docs/SESSION_HISTORY.md`](docs/SESSION_HISTORY.md). That file is the source of truth for what changed in recent sessions; AGENTS.md is the onboarding + conventions document.

> **Secrets + env audit:** For the canonical list of env vars + log-leak audit + deploy checklist see [`docs/SECRETS_AUDIT.md`](docs/SECRETS_AUDIT.md).

---

## Non-negotiables (ALWAYS FOLLOW - prevent breaking)
- Preserve ALL current CRM data and layout **as if we are still using the OnlyOffice API**.
  - Same note types (history_categories), opportunities, note history (embedded emails in history events), contacts, tasks, stages, tags, custom/user fields, etc.
  - When in doubt, refer to last known production version details (git show main:...) to see what user is displaying/using in deal tiles and preview modals.
  - Make sure links, data, functionality still work exactly.
- **Do not change anything that does not explicitly need to be changed.**
- Implement/replace **all** functionality/fields/modals that required OO API calls with local DB/v2 equivalents.
- Example: Add contacts support (section/tile/admin) so old contacts data imports and contact-based func (deals, bot modal, etc.) works.
- Focus functional first with new DB/UI. Move full data sync/import to later phase.
- After every change: run git status --short && git diff --stat; update `docs/SESSION_HISTORY.md` (prepend new "Current session summary" block) + CHANGELOG; commit with git.
- For admin: icons in tabs (exact same SVGs next to titles), no separate buttons. Keep current tab order.
- Hover on tiles: exact original production (mouse over lights borders, no re-render/flash).
- Sync: timestamp based (full + delta), bot creds (show fields default hidden), pull all via history etc.
- Header: static "Sietch CRM [version]". Footer/logo/favicon/name static (overwrite old). Watermark/header logo = customizable.
- Git version + docs after changes.

---

## Project Overview & Architecture (high level, reuse these)
- Vanilla JS dashboard (no framework) + Python backend for Sietch CRM.
- **Frontend (public/):**
  - `index.html` (modals, tiles, chrome)
  - `app.js` (main; state, rendering, api wrapper, profile sync, modals for deal-edit/create/quick-note/preview, group kanban, feed, tasks, calendars, notes)
  - `styles.css`
  - Static assets (favicons, ship logos)
- **Backend:** `server.py` (v2 REST API, auth, document storage, Document Server proxy, user profiles, notes, calendars, presence, bot integration).
- **Database:** PostgreSQL 16 (`init.sql` schema, `db.py` connection layer).
- **Document Server:** OnlyOffice Document Server (standalone Docker container for viewing/editing Word, Excel, PowerPoint files).
- **Auth:** `auth.py` (PBKDF2 password hashing, session cookies, password reset via SMTP).
- **Persistence (per CRM user):**
  - `user_profile_store.py` (`data/user-profiles/.../*.json`; versioned; supports groups, calendarTiles, notesTiles, groupTemplates, tileLayout, hiddenFeedKeys, feedKeywordFilter)
  - `notes_store.py` (for notes tiles content)
  - LocalStorage fallbacks + debounce server saves (`scheduleUserProfileSave`).
- **Tiles:** Opportunity groups (kanban with filters/stages/tags/red), fixed panels (feed/notifications, tasks), addable (calendars ICS, notes markdown). See `Toaster_Features` for ideas.

### Key patterns to ALWAYS reuse
- **Profile:** `buildUserProfilePayload`, `applyUserProfile`, `loadUserProfileFromServer` (prefers server), `saveGroupsToStorage` / `scheduleUserProfileSave` / `saveUserProfileToServer`, `strip*RuntimeFields`.
- **Tiles/layout:** `bindTileChrome`, `applyTileBodyCollapsed` / `applyTileLayoutClasses`, `createLayoutButtons`, `attachTileCollapseButton`, `tileLayout` in `state`.
- **API:** `api(path, opts)` + `parseApiError` (throws on `!ok`); all CRM calls go through v2 API endpoints directly (no proxy).
- **New tile type (if adding):** follow checklist in `Toaster_Features` (add to HTML+JS chooser, persist in profile py + frontend, refresh policy, empty states, update docs).
- **Modals:** reuse `.modal` / `.modal-card` / backdrop / `data-*-dismiss` / escape; `openDealEditModal`, `confirmDialog`.
- **History/feed:** `unwrapHistoryEvents`, `/api/v2/projects/{id}/history`, `applyFeedKeywordFilter`.
- **Groups:** `fetchOpportunitiesForGroup` + `buildFilterQuery`, `renderCard`, `setupGroupToolbar` (templates, remove, filters).
- **Custom fields on create (ISSUE-001/FEAT-002):** Fully implemented and enabled (`CREATE_OPP_USER_FIELDS_ENABLED=true`). `customFieldList` with `{key,value}` camelCase added to create body. See `ISSUES.md` for root cause.
- **Do not:** Duplicate docs (link to `FUTURE_FEATURES.md`, `ISSUES.md`, `Toaster_Features`, `docs/UPDATE_AND_DEPLOY.txt`, `README`). No new abstractions unless the task requires. Prefer minimal changes following existing.

---

## Testing

Three test surfaces, all run via CI (`.github/workflows/smoke.yml`):

```bash
# Python unit tests (stdlib unittest)
python3 tests/test_event_bus.py           # 15 tests — SSE leak fix, publish-drops, system subscribers
python3 tests/test_backup.py              # 4 tests — tar excludes nested backup dir, scanner source
python3 tests/test_onlyoffice_import.py   # 8 tests — type map, closure sentinel, safe filename

# Node unit tests (node:test, no npm deps)
node --test tests/test_pure_helpers.mjs   # 28 tests — userInitials, contactInitials, isAudioUrl,
                                          #            isAudioLinkArgs, collectNumericIdsFromValue
```

`tests/test_pure_helpers.mjs` mirrors pure-JS helpers from `public/app.js`
(`userInitials`, `contactInitials`, `isAudioUrl`, `isAudioLinkArgs`,
`collectNumericIdsFromValue`). If a helper drifts in `app.js`, the test
fails loudly.

For functional / integration testing, see "How to Run / Test / Deploy" below.

---

## Architecture & Deployment Context
- The **dashboard** (this entire project) runs on its own DigitalOcean Ubuntu droplet (production: `https://dashboard.publicadjustermidwest.com`, currently `159.89.229.126`). It serves the vanilla JS UI from `public/` and acts as an API proxy (`server.py`) that forwards CRM calls to the OnlyOffice server while handling user profiles, notes, calendars, and auth.
- The **OnlyOffice CRM** (Community Server / Workspace) runs on a **completely separate** DigitalOcean droplet. The two servers communicate over public HTTPS.
- **Local testing workflow (mandatory before any push):**
  - All development and verification happens on the developer's machine (this laptop) using `./start.sh` with a **real PostgreSQL database**.
  - **`test-server.py` is NEVER for feature development or functional testing** — only chaos/mutation queue simulation (`/api/test/chaos`).
  - If `./start.sh` cannot run (no DB available on the laptop), testing must move to the **LAN server** with a real DB. Do NOT fall back to `test-server.py` for functional testing.
  - After local verification (browser + DevTools), commit and `git push`.
  - On the production dashboard droplet: `git pull`, `docker compose build`, `docker compose up -d` (see `docs/UPDATE_AND_DEPLOY.txt` and `docs/DEPLOY_v1.1_VERIFY_STEPS.md` for the exact safe checklist).
- **Critical separation rule:** This project is a **standalone dashboard**. It is deliberately kept completely separate from OnlyOffice so there is zero risk of it affecting or breaking the OnlyOffice Community Server installation. The `onlyoffice-module/` directory (if present) is legacy/separate and not used for the main dashboard. Local test servers exist solely to allow safe iteration on the JS + proxy code before deploying the standalone dashboard.
- **Production shared-hosting note:** The dashboard droplet also runs other web apps. As of 2026-07, public traffic for `dashboard.publicadjustermidwest.com` is handled by the **host's nginx** (systemd service at `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com`), **not** the Docker `estimate-nginx` container. The dashboard container binds to `127.0.0.1:8765`. The dashboard is in a separate Compose project but joins `estimate-enhancer_estimate-network` (harmless). **Required for uploads:** `client_max_body_size 100m; proxy_request_buffering off; proxy_read_timeout 120s;` in the host site file. Always read `docs/DASHBOARD_INFRASTRUCTURE.md` (especially the 2026-07 section) before touching nginx on the host. The old `/opt/estimate-enhancer/nginx.conf` is historical for this domain.

---

## Document Server (OnlyOffice) deployment notes

The dashboard uses an OnlyOffice Document Server for editing Word/Excel/PowerPoint files. In both production and local dev the Document Server runs **on the same droplet as the dashboard** (co-located in Docker), so it is a local service as far as the dashboard container is concerned. The correct `.env` values depend on whether the CRM is running inside Docker or standalone on the host.

### Required `.env` variables

| Variable | Purpose | Production (Docker co-located) | Local dev (standalone on host) |
|----------|---------|--------------------------------|--------------------------------|
| `DOCS_JWT_SECRET` | Shared secret for signing OnlyOffice JWT tokens. Must match `JWT_SECRET` configured on the Document Server. | `change_me_in_production` | `local_docs_secret_not_for_production` |
| `DOCS_PUBLIC_URL` | URL the **user's browser** uses to load the OnlyOffice editor. | `https://docs.publicadjustermidwest.com` | `https://192.168.1.68:9443` |
| `DOCS_INTERNAL_URL` | URL the **CRM server** uses to reach the Document Server internally. | Docker service hostname, e.g. `http://onlyoffice-docserver:80` or `https://onlyoffice-docserver:443` | Leave empty or set to LAN URL; standalone dev falls back to `DOCS_PUBLIC_URL` |
| `CRM_PUBLIC_URL` | URL the Document Server uses to download files and send callbacks. Must be reachable from the Document Server container. | `https://dashboard.publicadjustermidwest.com` | `http://192.168.1.68:8766` |
| `DOCSERVER_CONTAINER_NAME` | Docker container name used by the admin "Restart Document Server" button. | `onlyoffice-docserver` | `onlyoffice-docserver` |

### Production rules

1. **All infrastructure lives on the same droplet.** PostgreSQL, dashboard, and Document Server are all in Docker on the dashboard host. Document Server is reached by the dashboard container via the Docker network (`DOCS_INTERNAL_URL`), and by the user's browser via the public URL (`DOCS_PUBLIC_URL`).
2. **Set `DOCS_INTERNAL_URL` to the Document Server's Docker hostname/port.** Use `http://onlyoffice-docserver:80` for plain HTTP or `https://onlyoffice-docserver:443` if HTTPS is enabled inside the container. Make sure `DOCSERVER_CONTAINER_NAME` matches the actual container name.
3. **Never leave `DOCS_INTERNAL_URL=http://docserver:8080` in production.** That value was a broken placeholder and has been removed from `docker-compose.yml`.
4. **Ensure `CRM_PUBLIC_URL` is reachable from the Document Server container.** OnlyOffice downloads files and sends save callbacks to this URL. The Document Server container must be able to reach the dashboard through the host/nginx. If it cannot, edits will fail silently or show "Document Server connection lost".
5. **OnlyOffice 7.1+ requires `document.key` to be a plain string.** The dashboard now generates `document.key` as `sietch-doc-{doc_id}-{timestamp}` inside the signed editor config. Do not change it back to a JWT.

### Verification

```bash
# From the dashboard container
# Editor config should have a plain-string document.key
# docker exec -it sietch-crm /bin/sh -c 'python3 -c "import urllib.request; print(urllib.request.urlopen("http://localhost:8766/api/v2/documents/1/editor-config").read().decode()[:500])"'

# Document Server healthcheck from inside the dashboard container
docker exec sietch-crm /bin/sh -c 'python3 -c "import urllib.request; print(urllib.request.urlopen("http://onlyoffice-docserver:80/healthcheck", timeout=5).status)"'

# Document Server healthcheck from the host (via public URL / mapped port)
curl -s -k -o /dev/null -w "%{http_code}\n" https://docs.publicadjustermidwest.com/healthcheck

# CRM reachable from Document Server container
docker exec onlyoffice-docserver /bin/sh -c 'curl -s -o /dev/null -w "%{http_code}\n" https://dashboard.publicadjustermidwest.com/api/config'
```

---

## How to Run / Test / Deploy

- **⚠️ CRITICAL: Starting server from opencode shell.** The server process dies when backgrounded normally from the opencode shell. You **MUST** use `setsid bash -c '...'` to create a fully detached process group:
  ```bash
  setsid bash -c 'cd /home/chapman-server/Projects/new-crm && exec ./.venv/bin/python3 server.py >> /tmp/crm-server.log 2>&1' &
  ```
  Do NOT use plain `python3 server.py &` — it will appear to start but die silently when the opencode session exits or times out. The localtonet tunnel will then show "ConnectionRefused".
- **Dev (normal):** `cd ~/new-crm && cp -n config.example.env .env && ./start.sh` (or `DB_HOST=127.0.0.1 ./.venv/bin/python3 server.py` if needed). Server prints LAN URLs on start (binds 0.0.0.0). Login with your credentials (session-cookie auth). **Requires a real PostgreSQL database.**
- **Chaos testing only:** `python test-server.py` — this is **not** a development server. Only for simulating failures via `/api/test/chaos`. All functional development requires the real server with a real DB (or LAN server).
- **Test changes:** Browser + DevTools (Network tab for API calls, Application → Local Storage for profile). Always test both happy path and failure scenarios.
- **Unit tests:** See the **Testing** section above. Run them locally before pushing; the CI workflow runs the same suite on every push / PR to `main` + `develop`.
- **Agent memory rule (critical):** After every feature or fix, explicitly confirm the exact files changed, run `git status --short && git diff --stat`, prepend a new "Current session summary" entry to `docs/SESSION_HISTORY.md`, and ensure the CHANGELOG entry exists before ending the session.
- **Deploy:** See `docs/UPDATE_AND_DEPLOY.txt` (stop local server, edit, test locally, git commit + push). Then on the production droplet: `git pull`, `docker compose build`, `docker compose up -d`.
- **Debug:** `server.py` logs, browser console, `grep` in the codebase.
- **Profile data:** `data/user-profiles/...` (gitignored); survives restarts.

---

## Coding Conventions (follow existing)
- Vanilla JS + CSS; no new libs.
- Reuse helpers (`formatMoney`, `unwrap`, `escapeHtml`, `crmOpportunityUrl`, `historyEventDate`, `customField*` etc.).
- State in `state = { groups, ... }`; render functions are idempotent (find or create tile).
- After state change that should persist: the `*ToStorage()` + `scheduleUserProfileSave()`.
- Comments for non-obvious (esp. CRM API quirks).
- Update docs (`FUTURE_FEATURES.md` / `ISSUES.md` / `CHANGELOG.md`) when adding/fixing.
- For new features: add to Add Tile if applicable; document in `Toaster_Features` / `FUTURE_FEATURES.md`.
- Keep changes minimal and isolated.
- On edit: prefer `edit` for precision; read first.

---

## Other
- Acculynx research findings live in `FUTURE_FEATURES.md` (do not lose them).
- When in doubt on priority: user's explicit list > FUTURE suggested order.
- For sessions: prefer resume over new. If new session, AGENTS.md + tools get you oriented fast.
- Questions during work: use `question` tool for narrow choices.

Update this file when conventions or priorities change.

---

## Known agent failure mode
- Repeatedly forgets completed work (backdate HTML, changelog entries, etc.).
- Either overwrites or never commits verified changes.
- Going forward: every feature/fix must be explicitly confirmed on disk, `git status --short && git diff --stat` run, a one-line summary prepended to `docs/SESSION_HISTORY.md`, and the CHANGELOG entry added before the session ends. No arguing; the record is the source of truth.

(Generated as part of post-1.1 work to solve repeated context gathering.)