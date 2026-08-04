# AGENTS.md — Sietch CRM (new-crm branch)

**Current version:** 3.0.0 (released 2026-07-18; see CHANGELOG.md)  
**Last session summary (for next resume):** 2026-08-03 session 48: **Microsoft OAuth SMTP domain awareness + sent-mail deal-link fix + IMAP/compose polish.** (A) `oauth_providers.py`: `MicrosoftProvider.smtp_settings()` now accepts `email` and returns `smtp-mail.outlook.com:587` for personal Microsoft domains (`outlook.com`, `hotmail.com`, `live.com`, `msn.com`), keeping `smtp.office365.com:587` for work/tenant accounts; `imap_settings()` signature updated for consistency; `GoogleProvider` signatures updated. (B) `server.py` OAuth callback passes `email` to `smtp_settings()`/`imap_settings()`. (C) `_handle_mail_send` now inserts a sent-message copy into `mail_messages` (folder `Sent`, `is_read=TRUE`, synthetic `imap_uid`, generated `Message-ID`) and uses the returned `mail_messages.id` for `mail_deal_links`, fixing the FK violation caused by using `mail_outgoing.id`. (D) Admin "CRM Mail Accounts" list now filters to only accounts with `is_crm_mail === true`. (E) `server.py` IMAP sync helpers (`_imap_set_seen`, `_imap_set_flagged`, `_imap_move`, `_imap_delete`) now skip synthetic `sent:outgoing:` UIDs. (F) `smtp_client.py` detects `SmtpClientAuthentication is disabled` / `5.7.139` and returns a friendly explanation. (G) `public/app.js` compose deal selection now shows a prominent linked badge, clears the search box after selection, and warns before sending if the user typed a deal but didn't select one. (H) `CHANGELOG.md` and `AGENTS.md` updated. (I) **Verified:** `python3 -m py_compile server.py oauth_providers.py smtp_client.py` passes; `node --check public/app.js` passes; server restarted clean (PID 1718700), `/api/config` returns 200; CRM mail account (`crm@vanguardadj.online`) now syncs successfully (936 INBOX + 107 Sent messages). (J) `server.py` IMAP sync helpers (`_imap_set_seen`, `_imap_set_flagged`, `_imap_move`, `_imap_delete`) now skip synthetic `sent:outgoing:` UIDs. (K) `smtp_client.py` detects `SmtpClientAuthentication is disabled` / `5.7.139` and returns a friendly explanation. (L) `public/app.js` compose deal selection now shows a prominent linked badge, clears the search box after selection, and warns before sending if the user typed a deal but didn't select one. (M) `server.py` `_handle_mail_folders()` now scopes folder list to active account (`?account_id=crm` returns CRM mail folders only; numeric ID returns that account's folders only). (N) `scanner/mail_scanner.py` `_sync_imap_folders()` normalizes folder names (`inbox`→`INBOX`, `sent`→`Sent`, etc.) to prevent case-variant duplicates. (O) DB cleanup: merged duplicate folder rows, removed stale global system folders, normalized existing messages. (P) `switchMailTab()` now awaits `renderMailFolderList()` on every tab switch; `renderMailUnreadBadge()` passes `account_id` to `/api/v2/mail/unread-count` so each tab shows its own unread count. (Q) `#mail-account-settings-btn` toolbar gear now has a click handler that opens account settings for the active tab. (R) `scanner/mail_scanner.py` `_sync_imap_folders()` now skips Exchange diagnostic folders (`Sync Issues/*`, `Conversation History`); existing diagnostic folders removed from DB. (S) `#mail-account-settings-btn` toolbar gear now has a click handler that opens account settings for the active tab. (T) Account settings modal: OAuth accounts show IMAP/SMTP as read-only with "Edit servers" button; password fields show green "Saved" indicator when set; mobile modal fills viewport with footer pinned. (U) **NEXT:** test send from Bluehost CRM account with deal link; Outlook account SMTP remains blocked by Microsoft account-level `SmtpClientAuthentication is disabled` (requires Microsoft Support); return to CRM-mail add-account flag trace. (A) `oauth_providers.py`: `MicrosoftProvider.smtp_settings()` now accepts `email` and returns `smtp-mail.outlook.com:587` for personal Microsoft domains (`outlook.com`, `hotmail.com`, `live.com`, `msn.com`), keeping `smtp.office365.com:587` for work/tenant accounts; `imap_settings()` signature updated for consistency; `GoogleProvider` signatures updated. (B) `server.py` OAuth callback passes `email` to `smtp_settings()`/`imap_settings()`. (C) `_handle_mail_send` now inserts a sent-message copy into `mail_messages` (folder `Sent`, `is_read=TRUE`, synthetic `imap_uid`, generated `Message-ID`) and uses the returned `mail_messages.id` for `mail_deal_links`, fixing the FK violation caused by using `mail_outgoing.id`. (D) Admin "CRM Mail Accounts" list now filters to only accounts with `is_crm_mail === true`. (E) `CHANGELOG.md` and `AGENTS.md` updated. (E) **Verified:** `python3 -m py_compile server.py oauth_providers.py smtp_client.py` passes; server restarted clean (PID 1589456), `/api/config` returns 200. **NEXT:** user reconnects `aplus.estimates@outlook.com` via OAuth to pick up the corrected SMTP host; test send from both Bluehost CRM account and Outlook account. (A) **Mail-list row layout** (styles.css): expand-preview button now `order:8` (always last on desktop); narrow blocks (@container 720px + @media 600px) set `.mail-snippet{display:none}`, `.mail-subject{order:10; flex:1 1 100%}` (line 2), `.mail-row-tags{order:3}`, `.mail-date{order:4; margin-left:auto}`, `.mail-expand-btn{order:5; margin-left:0}` (last on line 1). Base `.mail-date` changed to `width:auto; min-width:105px; white-space:nowrap` so date+time never clips. (B) **Scanner fixes** (scanner/mail_scanner.py): `_find_opportunity_by_claim_code` now uses `field_id` (not `custom_field_id`) with dynamic lookup via `_claim_field_id()` (field_key `'field_11'` → id 72 from `custom_field_definitions`), wrapped in try/except → None. `_fetch_messages` line 378: `att.part` → `att.content_id` (fixed JSON serialization crash: `store_failed` for every attachment message). Per-account processed UIDs: `_mark_processed`/`_is_processed` keyed `f"{account_id}:{uid}"`; `_store_message` dedup adds `AND account_id=%s`. (C) **UI** (app.js + index.html): toolbar gear `#mail-account-settings-btn` added after refresh button, shown only when `acct.canManage`; `updateMailToolbarSettingsBtn()` called after renderMailTabs/switchMailTab/save. Tab-cog markup + delegated handler removed. OAuth status → `'Connected as ' + acct.email`. `account-settings-from-name` moved from manual SMTP section to top-level after Email Address (visible all auth methods). 30s auto-refresh `setInterval` in `openMailInboxModal`, cleared in `closeMailModal` + `minimizeEmailModal`. (D) **Verified:** node --check + py_compile pass; server PID 1420113 started clean; zero `store_failed`/`custom_field_id` errors; 146 stored messages (was 104), newest now Aug 2 07:30:23. Two transient `AUTHENTICATE failed` at startup (expired OAuth token, pre-existing timezone bug in refresh logic). (E) **Docs:** CHANGELOG updated, AGENTS updated. UNCOMMITTED: 8 modified + 4 untracked files. **NEXT:** browser-check (narrow layout, expand button last, subject line 2, date+time, toolbar gear, account settings, auto-refresh), then commit. **Previous session (45):** IMAP `A()` search fix, `message-id` from headers, `server.ehlo()` after starttls for SMTP XOAUTH2, `notification_dispatcher.py` crash (query_dicts), OAuth email fully working for `vanguardadjusting@outlook.com`.

## Localtonet tunnel (dev)

To test the CRM remotely, two localtonet tunnels are configured on this workstation:

| Tunnel | Public URL | Forwards to | Type |
|--------|-----------|-------------|------|
| Sietch CRM | `https://g2vpdgb498.localto.net` | `127.0.0.1:8766` | HTTPS/HTTP |
| Sietch Docs | `https://m6cbapao4w.localto.net:6777` | `127.0.0.1:6777` → (tcp-forwarder.py) → `127.0.0.1:9443` | TCP (raw TLS passthrough) |

**TCP forwarder:** The docserver tunnel was set up as a TCP tunnel pointing at port 6777, but the OnlyOffice Document Server container listens on port 9443. A small Python script (`tcp-forwarder.py` in the project root) bridges `127.0.0.1:6777` → `127.0.0.1:9443`. Kill with `pkill -f tcp-forwarder.py` or `kill $(lsof -ti :6777)`. It is started per-session and will not survive a reboot.

**Important for production deployment:** These values MUST be reversed before deploying to the production droplet. The production `.env` should use the actual production URLs:

| Variable | Dev (localtonet) | Production |
|----------|------------------|------------|
| `CRM_PUBLIC_URL` | `https://g2vpdgb498.localto.net` | `https://dashboard.publicadjustermidwest.com` |
| `DOCS_PUBLIC_URL` | `https://m6cbapao4w.localto.net:6777` | `https://docs.publicadjustermidwest.com` |
| `DOCS_INTERNAL_URL` | `https://127.0.0.1:9443` | `https://onlyoffice-docserver:443` (Docker) or `https://docs.publicadjustermidwest.com` (separate droplet) |

**Two code changes in `server.py` that are safe to keep in production but should be reviewed:**

1. **`_effective_docs_internal_url()` (line 250):** Simplified to always respect `DOCS_INTERNAL_URL` when it's set to a non-placeholder value, regardless of Docker/host mode. In Docker mode the function previously only used `DOCS_INTERNAL_URL` if inside a container; now it works in both modes. The old Docker-only guard was removed. This is safe for production as long as `DOCS_INTERNAL_URL` is set to a valid address.

2. **`_proxy_document_server()` (line 275):** Added SSL certificate verification bypass for HTTPS connections to the internal Document Server. This was needed because the local OnlyOffice Document Server uses a self-signed certificate. The same pattern is already used in `_download_from_docserver()`. **To remove this bypass in production:** delete the `ctx = None` / `if ds_url.startswith("https"):` block (lines ~285-290) and pass `context=ctx` from the `urlopen` calls, or change `CERT_NONE` to `CERT_REQUIRED` if the production Document Server has a proper CA-signed certificate.

**To revert all changes for production:**
```bash
# 1. Restore .env to production values
git checkout -- .env

# 2. In server.py, revert the two functions if desired:
#    - _effective_docs_internal_url() → old Docker-guard version
#    - _proxy_document_server() → remove SSL bypass block
#    (Both are optional; the new code works fine in production too.)

# 3. Rebuild and restart the Docker compose stack
git pull
docker compose build
docker compose up -d
```

- Known issue: server process dies when backgrounded from opencode shell (use `setsid` to detach — see ISSUE-012).
- Known issue: server must be killed and restarted after opencode session exits (use `setsid` to detach — see ISSUE-012).
- Known issue: OnlyOffice self-signed cert requires SSL bypass in `_download_from_docserver()`.
- Known issue: Tile pin button hidden — causes erratic behavior from full DOM rebuild (`mountDashboardTiles()` uses `innerHTML=""` to clear all containers). Needs redesign to use DOM insertion/reordering instead of full teardown. See `bindTilePinButton` and `mountDashboardTiles`.
- **FIXED (pending user verification): Dashboard tiles jumped erratically during resize and drag.** Fixed in Phase 2D-3 / ISSUE-013 (see ISSUES.md): dense flow removed, ghost-preview resize with commit-on-pointerup, SortableJS native drag, in-place reorder, dead draggable attrs removed. DO NOT PUSH until user verifies in browser.
- **KNOWN (follow-up, deferred):** `renderBoardGroups()` removes/recreates every group tile `<section>` on each data refresh (app.js:8198) — loses scroll/filter DOM state. Should become idempotent (update in place). Out of scope for Phase 2D-3.
- **KNOWN: Odysseus freeform architecture evaluated and REJECTED.** User chose the CSS Grid stability fix (Phase 2D-3) over a fixed-position snap-zone rewrite. If tiles ever need freeform placement, that's a full layout-engine rewrite (new x/y/w/h persistence schema, mobile fallback).
- **KNOWN: Remaining dead code** — `PINNED_TILE_IDS` (empty array), `PANEL_TILE_IDS` (empty Set), `isTilePinnedToTop` (always false), `setTilePinnedToTop` (empty), `normalizeOrderForPinned` (never called), `bindTilePinButton` (empty), `applyTilePinState` (empty), `syncPanelRowLayout` (referenced but undefined — would throw ReferenceError if called).
- Pending: sidebar toggle arrow direction may be reversed (expanded `◀` vs collapsed `▶`) — needs verification.
- Documents icons: Tabler SVG file-type icons with muted per-type colors, stroke-width 2 for legibility.
- Documents: drag-and-drop file move into folders, inline rename, move-to-personal/company, auto-refresh after save-as.
- Documents: sidebar folder tree (My Documents + Company scopes), XLS icon fix (substring "document" in mime type matched word check first), larger muted folder icons in file list.
- Sidebar folder tree uses `GET /documents/folders/tree` endpoint, repositions under active scope button, chevron expand/collapse separated from navigation.

- Added server health indicator (tabler-server icon, amber when unreachable, hidden when healthy; 60s poller + api() hooks; skips when tab hidden).
- Added Admin Infrastructure tab (server status, Docker health, infra log, restart controls).
- Documents toolbar: Delete/Move/Copy consolidated into dropdown.
- Docker: healthcheck + `restart: on-failure:5`.
- In-memory 200-event infra ring buffer in server.py.
- Added `do_PATCH` handler — stage updates from preview modal now work (`PATCH /api/v2/projects/{id}`).
- Documents endpoints wrapped in try/except — no more server crashes on DB errors (returns 500 JSON + logs to infra ring buffer).
- Minimize-to-sidebar for Email, Documents, and Search modals — universal minimize button (— icon) in modal header, icon-only sidebar triggers on right edge. Email saves scroll+selection; Documents saves scope+query; Search saves preview tabs. All coordinate with bookmark sidebar.
- Tabler icons webfont added for sidebar trigger icons.
- Search modal header row added with minimize button. Fixed tiny scrollbar in tab bar.
- Fixed desktop header positions (admin-console-btn restored to right: 9rem; health indicator at right: 11.5rem).
- Fixed mobile header positions (sign-out rightmost, admin gear left, health indicator left of that).

This file is auto-loaded by Grok into the system prompt for every session in this directory tree. It provides persistent project context so you do **not** need a full "pick up where we left off" explanation or complete re-exploration on every new session. (See also user-guide 12-project-rules.md and 17-sessions.md.)

**Always:** 
- Resume prior work via TUI welcome screen (recent sessions for this cwd), `/load`, `grok --resume <id>`, or `-c` (continue most recent). Chat history + prior state is preserved in `~/.grok/sessions/...`.
- Still use tools (list_dir / read_file / grep / run_terminal_command for git etc.) to inspect *current* code state — files + docs are the source of truth.
- For new sessions: `cd <project-root>` (sets workspace/cwd for rules + session grouping).

## Non-negotiables (ALWAYS FOLLOW - prevent breaking)
- Preserve ALL current CRM data and layout **as if we are still using the OnlyOffice API**.
  - Same note types (history_categories), opportunities, note history (embedded emails in history events), contacts, tasks, stages, tags, custom/user fields, etc.
  - When in doubt, refer to last known production version details (git show main:...) to see what user is displaying/using in deal tiles and preview modals.
  - Make sure links, data, functionality still work exactly.
- **Do not change anything that does not explicitly need to be changed.**
- Implement/replace **all** functionality/fields/modals that required OO API calls with local DB/v2 equivalents.
- Example: Add contacts support (section/tile/admin) so old contacts data imports and contact-based func (deals, bot modal, etc.) works.
- Focus functional first with new DB/UI. Move full data sync/import to later phase.
- After every change: run git status --short && git diff --stat; update AGENTS.md last summary + CHANGELOG; commit with git.
- For admin: icons in tabs (exact same SVGs next to titles), no separate buttons. Keep current tab order.
- Hover on tiles: exact original production (mouse over lights borders, no re-render/flash).
- Sync: timestamp based (full + delta), bot creds (show fields default hidden), pull all via history etc.
- Header: static "Sietch CRM [version]". Footer/logo/favicon/name static (overwrite old). Watermark/header logo = customizable.
- Git version + docs after changes.

## Project Overview & Architecture (high level, reuse these)
- Vanilla JS dashboard (no framework) + Python backend for Sietch CRM.
- **Frontend (public/):** 
  - index.html (modals, tiles, chrome)
  - app.js (main; state, rendering, api wrapper, profile sync, modals for deal-edit/create/quick-note/preview, group kanban, feed, tasks, calendars, notes)
  - styles.css
  - Static assets (favicons, ship logos)
- **Backend:** server.py (v2 REST API, auth, document storage, Document Server proxy, user profiles, notes, calendars, presence, bot integration).
- **Database:** PostgreSQL 16 (init.sql schema, db.py connection layer).
- **Document Server:** OnlyOffice Document Server (standalone Docker container for viewing/editing Word, Excel, PowerPoint files).
- **Auth:** auth.py (PBKDF2 password hashing, session cookies, password reset via SMTP).
- **Persistence (per CRM user):** 
  - user_profile_store.py (data/user-profiles/.../*.json; versioned; supports groups, calendarTiles, notesTiles, groupTemplates, tileLayout, hiddenFeedKeys, feedKeywordFilter)
  - notes_store.py (for notes tiles content)
  - LocalStorage fallbacks + debounce server saves (scheduleUserProfileSave).
- **Tiles:** Opportunity groups (kanban with filters/stages/tags/red), fixed panels (feed/notifications, tasks), addable (calendars ICS, notes markdown). See Toaster_Features for ideas.
- **Key patterns to ALWAYS reuse:**
  - Profile: buildUserProfilePayload, applyUserProfile, loadUserProfileFromServer (prefers server), saveGroupsToStorage/scheduleUserProfileSave/saveUserProfileToServer, strip*RuntimeFields.
  - Tiles/layout: bindTileChrome, applyTileBodyCollapsed/applyTileLayoutClasses, createLayoutButtons, attachTileCollapseButton, tileLayout in state.
  - API: `api(path, opts)` + parseApiError (throws on !ok); all CRM calls go through v2 API endpoints directly (no proxy).
  - New tile type (if adding): follow checklist in Toaster_Features (add to HTML+JS chooser, persist in profile py + frontend, refresh policy, empty states, update docs).
  - Modals: reuse .modal / .modal-card / backdrop / data-*-dismiss / escape; openDealEditModal, confirmDialog.
  - History/feed: unwrapHistoryEvents, /api/v2/projects/{id}/history, applyFeedKeywordFilter.
  - Groups: fetchOpportunitiesForGroup + buildFilterQuery, renderCard, setupGroupToolbar (templates, remove, filters).
- **Custom fields on create (ISSUE-001/FEAT-002):** Fully implemented and enabled (CREATE_OPP_USER_FIELDS_ENABLED=true). customFieldList with {key,value} camelCase added to create body. See ISSUES.md for root cause.
- **Do not:** Duplicate docs (link to FUTURE_FEATURES.md, ISSUES.md, Toaster_Features, docs/UPDATE_AND_DEPLOY.txt, README). No new abstractions unless the task requires. Prefer minimal changes following existing.

## Post-v1.2 shipped items (for reference)
All items from the explicit post-1.1 testing list + live feedback were completed for v1.2.0:
- Tile collapse/minimize (notes, calendar, groups with half/quarter width preserved while collapsed) + calendar double-height scaling.
- Immediate persistence on group remove.
- Tasks rows show description.
- Keyword filter: comma-separated = AND (every token required).
- Preview stays open on edit-from-preview.
- White favicon/logo.
- Friendly message for linked-email "not found".
- Full tasks-list modal (cabinet icon button, open+completed, show-completed toggle, check/uncheck via /close+/reopen, New Task, deep links, light readability styling).
- Template management: delete-only modal with × per template (no edit/rename).
- CRM crash/5xx banner (exact wording, 30s guidance, throttle, auto-clear, api() trigger).
- AGENTS.md added.
- FUTURE_FEATURES cleanup (crash item removed because shipped).

**v1.4.5 additions (most recent session work — read this first on next resume):**
- Side-by-side "quick edit" / note popup from opp preview: opens left of preview (desktop) or fixed top (mobile); both fully interactive; auto-refresh of preview history on submit; new manual ⟳ refresh button left of ✎; × delete on note history items in preview (confirm + DELETE /history/{id}).
- Presence AFD: "Away from dashboard" (subtle gray dot + section) for tab-away but active session vs true "offline" only on sign-out (server clears hb on /logout); 3h auto-logout; "Last CRM (proxy)" confirmed only from real proxied calls.
- Feed: today's notifications get subtle white left border line (`.feed-item-today`).
- Crash resilience: on 502/5xx (api + presence), persistent right-side amber banner ("CRM temporarily unreachable... refresh in 30s or contact admin"); no raw toasts; tiles *all* render (CRM ones empty/no-content, local features work); banner hides only on successful CRM response.
- Quick note side submit now reliably refreshes preview.
- **Mobile fixes (3):** bot/event-log buttons stacked below sign-out (Option A, bot swapped to rightmost on mobile); opp preview modal fills screen width; presence stale autoStatus leak fixed (modal + tile) + poll failure no longer wipes team roster. See CHANGELOG.md.
- All changes in v1.4.5 release notes + full deploy checklist followed (local close, git tag/push, prod docker + VERIFY blocks). Update AGENTS on every release.

See CHANGELOG.md and docs/RELEASE_v1.2.md. AccuLynx research stays in FUTURE_FEATURES under Other ideas (not implemented). 

For the previous post-v1.1 list and implementation notes, consult the git history / session artifacts around the v1.2 commits.

Legacy open items (lower priority unless asked): FEAT-003 attachments, new toasters (stale deals, closing this week, etc.), FEAT-022 in-modal document editor tab.

## Architecture & Deployment Context
- The **dashboard** (this entire project) runs on its own DigitalOcean Ubuntu droplet (production: https://dashboard.publicadjustermidwest.com, currently 159.89.229.126). It serves the vanilla JS UI from `public/` and acts as an API proxy (`server.py`) that forwards CRM calls to the OnlyOffice server while handling user profiles, notes, calendars, and auth.
- The **OnlyOffice CRM** (Community Server / Workspace) runs on a **completely separate** DigitalOcean droplet. The two servers communicate over public HTTPS.
- **Local testing workflow (mandatory before any push):**
  - All development and verification happens on the developer's machine (this laptop) using `./start.sh` with a **real PostgreSQL database**.
  - **`test-server.py` is NEVER for feature development or functional testing** — only chaos/mutation queue simulation (`/api/test/chaos`).
  - If `./start.sh` cannot run (no DB available on the laptop), testing must move to the **LAN server** with a real DB. Do NOT fall back to `test-server.py` for functional testing.
  - After local verification (browser + DevTools), commit and `git push`.
  - On the production dashboard droplet: `git pull`, `docker compose build`, `docker compose up -d` (see docs/UPDATE_AND_DEPLOY.txt and docs/DEPLOY_v1.1_VERIFY_STEPS.md for the exact safe checklist).
- **Critical separation rule:** This project is a **standalone dashboard**. It is deliberately kept completely separate from OnlyOffice so there is zero risk of it affecting or breaking the OnlyOffice Community Server installation. The `onlyoffice-module/` directory (if present) is legacy/separate and not used for the main dashboard. Local test servers exist solely to allow safe iteration on the JS + proxy code before deploying the standalone dashboard.
- **Production shared-hosting note:** The dashboard droplet also runs other web apps. As of 2026-07, public traffic for `dashboard.publicadjustermidwest.com` is handled by the **host's nginx** (systemd service at `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com`), **not** the Docker `estimate-nginx` container. The dashboard container binds to `127.0.0.1:8765`. The dashboard is in a separate Compose project but joins `estimate-enhancer_estimate-network` (harmless). **Required for uploads:** `client_max_body_size 100m; proxy_request_buffering off; proxy_read_timeout 120s;` in the host site file. Always read `docs/DASHBOARD_INFRASTRUCTURE.md` (especially the 2026-07 section) before touching nginx on the host. The old `/opt/estimate-enhancer/nginx.conf` is historical for this domain.

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

## How to Run / Test / Deploy
- **Dev (normal):** `cd ~/new-crm && cp -n config.example.env .env && ./start.sh` (or `DB_HOST=127.0.0.1 ./.venv/bin/python3 server.py` if needed). Server prints LAN URLs on start (binds 0.0.0.0). Login with your credentials (session-cookie auth). **Requires a real PostgreSQL database.**
- **Chaos testing only:** `python test-server.py` — this is **not** a development server. Only for simulating failures via `/api/test/chaos`. All functional development requires the real server with a real DB (or LAN server).
- **Test changes:** Browser + DevTools (Network tab for API calls, Application → Local Storage for profile). Always test both happy path and failure scenarios.
- **No tests:** No automated suite; manual + visual testing.
- **Agent memory rule (critical):** After every feature or fix, explicitly confirm the exact files changed, run `git status --short && git diff --stat`, write a one-line summary in AGENTS.md under "Last session summary", and ensure the CHANGELOG entry exists before ending the session.
- **Deploy:** See docs/UPDATE_AND_DEPLOY.txt (stop local server, edit, test locally, git commit + push). Then on the production droplet: `git pull`, `docker compose build`, `docker compose up -d`.
- **Debug:** server.py logs, browser console, `grep` in the codebase.
- **Profile data:** `data/user-profiles/...` (gitignored); survives restarts.

## Coding Conventions (follow existing)
- Vanilla JS + CSS; no new libs.
- Reuse helpers (formatMoney, unwrap, escapeHtml, crmOpportunityUrl, historyEventDate, customField* etc.).
- State in `state = { groups, ... }`; render functions are idempotent (find or create tile).
- After state change that should persist: the *ToStorage() + scheduleUserProfileSave().
- Comments for non-obvious (esp. CRM API quirks).
- Update docs (FUTURE/ISSUES/CHANGELOG) when adding/fixing.
- For new features: add to Add Tile if applicable; document in Toaster_Features/FUTURE.
- Keep changes minimal and isolated.
- On edit: prefer search_replace for precision; read first.

## Other
- Acculynx research findings live in FUTURE_FEATURES.md (do not lose them).
- When in doubt on priority: user's explicit list > FUTURE suggested order.
- For sessions: prefer resume over new. If new session, AGENTS.md + tools get you oriented fast.
- Questions during work: use ask_user_question for narrow choices.

Update this file when conventions or priorities change.

## Known agent failure mode
- Repeatedly forgets completed work (backdate HTML, changelog entries, etc.).
- Either overwrites or never commits verified changes.
- Going forward: every feature/fix must be explicitly confirmed on disk, `git status --short && git diff --stat` run, a one-line summary written here, and the CHANGELOG entry added before the session ends. No arguing; the record is the source of truth.

(Generated as part of post-1.1 work to solve repeated context gathering.)