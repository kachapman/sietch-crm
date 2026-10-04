# AGENTS.md — Sietch CRM (new-crm branch)

> **Server-wide reference:** `/home/chapman-server/Projects/CHAPMANSERVER_WIREDOOR.md` — how services on this machine are exposed to the internet (Wiredoor tunnel, public hostnames/ports, credentials, troubleshooting). Read it before touching networking, tunnels, or public ports.

**Current session summary (for next resume):** 2026-10-04: **Full re-sync + history_attachments uniqueness fix (030) + filename aside fix + email-bodies re-fetch.** Retry of the live migration died on opp 1243: migration 029's GLOBAL unique index on `history_attachments.crm_file_id` rejected file 18133 appearing on a 2nd event (same file routinely referenced by multiple events), and the aborted transaction poisoned the whole run. New migration `030_fix_history_attachment_uniqueness.sql` replaces it with `UNIQUE (event_id, crm_file_id) WHERE NOT NULL` + plain `crm_file_id` lookup index (`init.sql` mirrored; verified same-event dup rowcount=0, cross-event insert OK). `migrate_from_onlyoffice.py` attachment INSERT now `ON CONFLICT (event_id, crm_file_id) DO NOTHING` + SAVEPOINT-per-section (tags/CF/history) with ROLLBACK TO so one bad opp can't kill the run; same per-event dedup applied to `sync_history_attachments.py` (now dict-based + back-fills bytes for `--skip-attachment-download` metadata-only rows via UPDATE), `server.py` pull-deals + pull-email-bodies, `import_json_export.py`. Full migrate re-run completed clean in ~26min: **1229 opps / 48822 history / 17180 attachments (metadata) / 190 tags / 12612 CF / 837 tasks**. 4-shard `sync_history_attachments.py` (added `--shard/--shards` + fail-fast on 401/403/404) downloaded 15,041 files in ~1.5h → **17,166 of 17,180 rows on disk, 33 GB**. `sync_email_bodies.py` (same shard pattern) lifted Mail Message `body_html` coverage from 1,380 → 5,223 of 13,016 events (7,793 bot 403s remaining). `promote_history_attachments.py` produced 15,041 new `project_documents` (15,041+2,125 = 17,166). Earlier fixes from this session: `extractHistoryAttachments` lowercase `filename` (commit `4fddf1d`), embedded-email attachments real names + project_documents editor (commit `18a1975`). **All sync work complete.** Cross-table FK integrity: 0 breaks. Known gaps documented (live API limits, not bugs): 1081 opps w/o tags, 1124 w/o tasks, 7 w/o history, 512 orphan tasks, 535 NULL authors, 1 NULL category. Commits: `3362d97`, `0aa1b41`, `e89ad02`, `e415d3e`, `4fddf1d`, `18a1975`. **Deploy checklist:** `rsync -a --partial` 33 GB `data/history-attachments/` to droplet; verify ~50 GB free; full migrate on droplet will be a no-op delta; 7,793 MM bodies + 14 attachments need admin-token or manual copy. Server PID 1137072, `/api/config` 200. Bot pw `FRi3tz4yWXrMTEZ` preserved.

**Previous summary (2026-10-02):** **OnlyOffice import/sync v2 audit + rewrite + first live API run.** Pre-cutover audit of the migration pipeline from the older production OnlyOffice CRM (`office.publicadjustermidwest.com`) found 22 bugs (most severe: every field became `text` because numeric `fieldType` (0/1/3/5) was matched against string `type_map`; history attachments never downloaded; `actualCloseDate` dropped; `created_by` always NULL; `pull-deal-tags` destructive on failure; `pull-tasks` used wrong endpoint; no per-deal tags because JSON export didn't include them; live API uses different URLs than the static export implies). **Schema:** new migrations `024_onlyoffice_ids_and_actual_close.sql`, `025_contact_multi_values.sql`, `026_user_avatar_icon.sql`, `027_unique_contact_onlyoffice_id.sql` (promote onlyoffice_id to unique for ON CONFLICT dedup), `028_contact_multi_value_unique.sql`. All init.sql mirrors for fresh DBs. **Standalone scripts** (`import_json_export.py`, `migrate_from_onlyoffice.py`) full rewrite with: numeric CF type map (0→text, 1→textarea, 3→url, 5→date), contact multi-value tables + person→company 2nd pass + avatar download, opportunity dedup by `onlyoffice_id` then title, `actual_close_date` with sentinel handling, history event author mapping (OO user UUID → sietch uuid via /people/), `crm_event_id`/`crm_entity_id` on history, history attachment download with `crm_file_id`-prefixed unique filenames to `data/history-attachments/<opp>/<event>/<id>_<safe_name>`, JSON `email_snapshot` Mail Message handling, dedup'd contacts, task_labels export + import. **server.py live handlers** — same fixes + always-sync-tags-and-CFs (was new-only), non-destructive `pull-deal-tags` (scoped to imported deals), upsert `pull-contacts` with avatar download, switched `pull-tasks`/`full-reconcile` to `/api/2.0/crm/task/filter?isClosed=true/false`, `pull-email-bodies` now downloads email attachments into history_attachments (Bug #6), fixed orphan-cleanup count log, added filter params (`since`/`stage_id`/`responsible_id`) to `pull-deals`, `full-reconcile` now actually runs users/stages/tags chain. **Endpoint corrections during live run** — `/api/2.0/people/` → `/api/2.0/people`, `/api/2.0/crm/contact` → `/api/2.0/crm/contact/filter?count=500`, `/api/2.0/projects/task/` (404) → `/api/2.0/crm/task/filter?isClosed=…`, `/api/2.0/files/{id}` (403 for bot) → `/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={id}`. **User avatar picker** — new `users.avatar_icon TEXT` column + 100-icon **TABLER_ICON_OPTIONS** constant in `public/app.js` (people/animals/nature/buildings/transport/media/food/tech/office/security/misc, matching reference screenshot) + new "Pick Icon" button in profile modal + new `openTablerIconPicker()` modal with 10×10 grid + new server endpoints `GET/PUT /api/v2/my/avatar-style`, `GET /api/v2/users/avatar-styles`, `GET /api/v2/contact-avatars/<name>` (path-traversal-safe) + `renderUserAvatar()` / `renderContactAvatar()` helpers (priority: photo > icon > initials, first+last initials) + mail-account tab appearance picker now uses the same 100-icon pool. New unit-test file `tests/test_onlyoffice_import.py` (8 tests, all pass). New `sync_opp_details.py` focused live-API per-opp tags+CF+history sync (used after bulk import to fill in JSON export gaps). **First live API run succeeded** (against `office.publicadjustermidwest.com` with `bot@vanguardadj.com` admin credentials): 11 users (bot password preserved) / 19 stages / 23 tag defs / 30 CF defs (22 text / 4 url / 3 textarea / 1 date — correct numeric type mapping) / 18 contacts (with 14 emails, 14 phones, 21 addresses + 18 avatar files on disk) / 1229 opportunities (was 1191 in export — **38 new deals created since July**) / **190 opportunity_tags** (was 0 — live per-deal tag endpoint pulled real ones) / 12612 opportunity CF values / **41745 history events** (was 38898 — 2847 new events) / **836 tasks** (was 0 — live endpoint) / 10 history categories. Mail Message events have `created_by` mapped to real users (verified `claudc@vanguardadj.com`). Scott Miller deal (`onlyoffice_id=1235`): 1 tag, 14 CF values, 22 history events. **Files:** `migrations/024…028_*.sql` (NEW), `init.sql` (~120 lines), `import_json_export.py` (full rewrite), `migrate_from_onlyoffice.py` (full rewrite), `server.py` (~150 lines added: helpers + 6 handler refactors + 4 new API endpoints), `public/app.js` (~150 new lines), `public/index.html` (tabler icon picker modal + Pick Icon button), `public/styles.css` (~80 lines), `sync_opp_details.py` (NEW), `tests/test_onlyoffice_import.py` (NEW), `CHANGELOG.md`. **Server PID 190502**, `/api/config` returns 200. **NEXT:** browser-test the profile modal Pick Icon button (open profile → Pick Icon → grid appears → click `cat` → circle now shows cat icon on white background); browser-test the contact list (open sidebar contacts → avatars should NOT be broken-image — should render from `/api/v2/contact-avatars/<id>.jpg`; fallback to initials if 404); commit all changes via `git add -A && git commit -m "OnlyOffice import v2 audit + rewrite + live sync"`. **Known limitation:** file downloads via `filehandler.ashx?action=download&fileid=N` return 403 for the bot user on most files (folder permissions — admin token required, or manual file copy); 0 history_attachments in current DB. **Migration command for future use:** `migrate_from_onlyoffice.py --portal-url https://office.publicadjustermidwest.com --email bot@vanguardadj.com` (uses getpass prompt — does NOT expose password on shell).

**Previous session (this same 2026-09-30 session):** Service Feeds (third-party status / webhook → notifications).

## Add `display: flex; flex-direction: column;` to bookmark and search-popup preview bodies (2026-09-22) New `service_feeds` table (migration 022) + RSS/Atom poller daemon (`service_feed_poller.py`, stdlib-only — no `feedparser` dependency) + webhook receiver endpoint at `POST /api/v2/service-feeds/webhook/{secret}` (no session auth, secret in URL). Per-feed target_mode='all' (notifies every active user including admins) or target_mode='specific' (only user IDs in `target_user_ids`). Per-feed `accent_color` applies as left-border stripe + icon-circle background tint on the notification card. Admin UI lives at the bottom of **Admin → General** tab: feed list with cloud icon + kind badge + target description + last-polled-at, + Add Feed editor (RSS/Webhook kinds, color picker, target-mode radios, searchable user picker with chips for specific mode, copyable webhook URL after save), Refresh / Edit / Delete buttons. New notification `type='service_feed'` with Tabler cloud icon. Click opens `payload.url` in a new tab. Webhook accepts Proton-style `{event:{title,body,shortlink}}` and generic `{title,message,url}` shapes; returns 200 for unknown secrets to avoid enumeration. **Verified end-to-end:** test webhook POSTed → 5 notifications created (one per active user). **Files:** `migrations/022_add_service_feeds.sql` (NEW), `init.sql`, `service_feed_poller.py` (NEW, ~180 lines), `server.py` (~250 lines: routes + handlers + helpers + poller boot), `public/index.html` (+~80 lines), `public/app.js` (+~280 lines), `public/styles.css` (+~50 lines), `CHANGELOG.md`. **Cache buster `?v=3.6.1` → `?v=3.6.2`; VERSION still 2.3.0.** **NEXT:** browser-test (Admin → General → Service Feeds → add a Proton status RSS feed, verify polling produces notifications with cloud icon + accent stripe, click opens incident URL in new tab; add a webhook feed, copy URL, POST a test event, verify notification appears).

**Previous session (this same 2026-09-30 session):** Changelog modal full-screen on narrow / mobile + × close button.

## Add `display: flex; flex-direction: column;` to bookmark and search-popup preview bodies (2026-09-22) On phones and narrow viewports the changelog modal was rendering far wider than the viewport (overflowing left of the screen) so the close button wasn't reachable. Added `@media (max-width: 720px)` rule for `.modal-card-changelog` that makes the modal fill the viewport (100vw × 100dvh, no border-radius, no padding around the card) — matches the same full-screen mobile pattern used by photo-report / invoice-generator / coc-generator modals. Added `word-break: break-word; overflow-wrap: anywhere;` to `.changelog-body code` so long inline code spans don't force horizontal overflow. Added a `×` close button to `.changelog-modal-head` (matches the header pattern used by every other modal) — the existing `[data-changelog-dismiss]` binding already handles the close action, no JS change. **Verified:** `py_compile` + `node --check` pass. **Cache buster `?v=3.5.9` → `?v=3.6.0`; VERSION still 2.3.0.** **Files:** `public/styles.css` (~20 lines), `public/index.html` (1 button added), `CHANGELOG.md`. **NEXT:** browser-test on phone-width viewport → confirm changelog modal fills the screen and both X (top-right) + Close (bottom-right) buttons are visible without scrolling.

**Previous session (this same 2026-09-28 session):** Project doc refresh + auto-open + editor diagnostics + upload-button cleanup.

## Add `display: flex; flex-direction: column;` to bookmark and search-popup preview bodies (2026-09-22) (1) `createBlankDocument(type, oppId)` project path now dispatches a `document-created` CustomEvent that the project Documents tab's existing photo-report-saved/invoice-generated/coc-generated listener pattern picks up and calls `refreshDocs()`. Standalone path still calls `loadDocumentsList()` + still triggers `startRenameDoc(data.id)` per user request. (2) On BOTH paths, the new doc's editor now opens in a new tab (`window.open(data.editUrl, '_blank')`). (3) **Diagnostic logs** for the "Download failed" / slow editor problem: added `log_infra_event` in `_handle_document_create_blank` (records doc id, type, **absolute path** on disk, file-exists check, byte count), in `_handle_document_editor_config` (records the download URL handed to OnlyOffice), and in `_handle_document_download` (404 paths for missing DB row + missing file, with the absolute path that was missing). Also added `Cache-Control: no-store` to download responses. **When the user reproduces the failure next**, they should check the infra ring buffer / `/tmp/crm-server.log` for `documents` and `docserver` events — these will pinpoint whether the create wrote the file, what URL OnlyOffice was told to fetch, and whether the download handler actually saw the request. (4) **Upload button** in both the project Documents tab and Photos tab is now icon-only (removed the literal "Upload" text), matching the standalone modal's `#documents-upload-btn`. **Cache buster `?v=3.5.7` → `?v=3.5.8`.** **Verified:** `py_compile` + `node --check` pass, server restarted clean (PID 2634947). **NEXT:** user reproduces the .xlsx creation from project modal → confirm (a) the doc appears in the list immediately, (b) the editor opens in a new tab automatically, (c) the editor loads without "Download failed", (d) the new infra-log entries for that doc id appear in `/tmp/crm-server.log`. If "Download failed" still recurs, the logs will tell us whether the issue is in create / editor-config / download-handler / network and we can iterate.

**Previous session (this same 2026-09-28 session):** Three follow-up fixes to v2.3.0 invoice/COC generators — signature image upload (`POST /api/v2/admin/document-templates/{id}/signature`), "Generated by Sietch CRM" footer toggle (`show_branding_footer` column), em dash / Unicode crash fix (replaced `_doc_pdf_safe` with Unicode→ASCII mapping version; wrapped 6 unsanitized `pdf.cell()` / `pdf.get_string_width()` calls).

## Add `display: flex; flex-direction: column;` to bookmark and search-popup preview bodies (2026-09-22) **Three follow-up fixes to v2.3.0 invoice/COC generators.** (1) **Signature image upload** in template editor — new `POST /api/v2/admin/document-templates/{id}/signature` endpoint mirroring the logo upload (stores at `data/logos/docsig_{id}_{safename}`, replaces existing, unlinks old file). Added Signature Image file input + preview to the editor form; `saveAdminDocTpl` now uploads signature sequentially with logo if both are selected. (2) **"Generated by Sietch CRM" footer toggle on COC** — new `document_templates.show_branding_footer BOOLEAN NOT NULL DEFAULT TRUE` column. Added to migration `021_add_document_templates.sql`, `init.sql`, and the runtime `CREATE TABLE IF NOT EXISTS` block. Idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` covers existing DBs. New "Show 'Generated by Sietch CRM' footer" checkbox in the editor. `_create_coc_pdf` reads the toggle and only draws the text when True. (3) **Em dash / Unicode crash fix** — root cause: fpdf2's built-in Helvetica only covers Latin-1, so any em dash (U+2014) / smart quote (U+2018–U+201D) / ellipsis (U+2026) crashed with `Character "—" at index N in text is outside the range of characters supported by the font used: "helvetica"`. Replaced `_doc_pdf_safe` with a Unicode-mapping version (em/en/figure dash → `-`, smart quotes → ASCII, ellipsis → `...`, bullets → `*`, arrows → `->`/`<-`/`^`/`v`, NBSP → space, line separators → newline), then strips anything still above U+00FF. Wrapped 6 unsanitized `pdf.cell()` / `pdf.get_string_width()` calls (the width check was the original crash site). **Verified:** em dashes everywhere now produce valid PDFs (was crashing on `get_string_width(desc)` for line items), `show_branding_footer` column visible in `information_schema`, all 4 admin endpoints respond correctly, server restarted clean. **Cache buster `?v=3.5.6` → `?v=3.5.7`; VERSION still 2.3.0.** **Files:** `migrations/021_add_document_templates.sql` (+column), `init.sql` (+column), `server.py` (route + handler + sanitization + column + PUT allowlist + footer gate), `public/index.html`, `public/app.js`, `CHANGELOG.md`. **NEXT:** browser-test (upload signature image, toggle footer off, regenerate with em-dash text in fields); commit all v2.3.0 work + push.

**Previous session (this same 2026-09-28 session):** Initial v2.3.0 release: invoice + COC fpdf2 generators, `document_templates` table + admin sub-tab inside Reports tab, "Create Document" dropdown added to every Documents tab (main / bookmark / search) and standalone Documents modal, system action note inserted on every generated PDF (invoice, COC, **and the previously silent photo report — now also fixed**).

## Add `display: flex; flex-direction: column;` to bookmark and search-popup preview bodies (2026-09-22) Added two new fpdf2-based PDF generators (Invoice, COC) modeled on the photo-report generator pattern from Sherwood toolbox. New `document_templates` table (migration 021) carries company branding (logo, name, address, phone, email, primary color), invoice/COC titles, default terms, default sales rep, default line items (JSONB), and signature config — single template reused by both document types. New admin CRUD endpoints + admin UI under a new **Document Templates (Invoice / COC)** sub-tab inside the existing Reports admin tab (Photo Reports / Document Templates sub-tab bar). New modals: **Invoice Generator** and **COC Generator**, opened from (a) a new "Create Document" button + dropdown (Word / Excel / Invoice / COC — no folders) added to the Documents tab toolbar inside every opp preview (main modal, bookmark sidebar, search popup — all share `renderOpportunityPreviewContent`); and (b) **Invoice / Certificate of Completion** entries added to the standalone Documents modal's "Create document or folder" dropdown. **Project-scoped flow** auto-fills customer / claim # (via `field_11` lookup, same as photo report) / job # / sales rep / address from project; user can override any field. **Standalone flow** opens in fully manual-entry mode (no project context); saves to user's personal scope with `opportunity_id=NULL`. **System action notes:** every generator (invoice, COC, **plus the previously silent photo report — now also fixed**) inserts a `history_events` row using the system "Action" category (id resolved at runtime via `_resolve_action_category_id()` since id is data-driven across envs); note format `<doc type> generated: <filename> (<size>, Claim #<n>)`. **Verified:** server compiles (`py_compile`), app.js compiles (`node --check`), `document_templates` table auto-created at startup, GET endpoint returns 401 (route wired), direct PDF generation test produced valid PDFs (`file` confirms), server restarted clean (PID 2551738). Test template cleaned up. **Cache buster `?v=3.5.5` → `?v=3.5.6`; VERSION 2.2.5 → 2.3.0.** **Files:** `migrations/021_add_document_templates.sql` (NEW), `init.sql`, `server.py` (~1100 lines: 8 routes + 7 handlers + 2 PDFs + helpers + photo-report fix), `public/index.html`, `public/app.js` (~700 lines: admin sub-tab + 2 modals + dropdown wiring + createBlankDocument oppId arg + auto-refresh listeners), `public/styles.css` (~140 lines), `CHANGELOG.md`, `VERSION`. **NEXT:** browser-test (admin templates → project invoice/COC generation → standalone invoice/COC → history tab action notes → bookmark/search preview dropdown); then commit + push.

## Add `display: flex; flex-direction: column;` to bookmark and search-popup preview bodies (2026-09-22) **Add display:flex to bookmark and search-popup preview bodies — the missing 2 of 4 preview body variants.** The user was in the bookmark sidebar (right-side "Bookmarks" panel) showing the opp preview. The preview pane still wasn't filling vertically. I had fixed `.opp-preview-body` (the regular full-screen modal) earlier this session, and `.email-preview-body` already had the fix (set when email preview was originally built), but I missed the OTHER TWO variants: `.bookmark-preview-body` and `.search-popup-preview-body`. Both have the same bug: `flex: 1; min-height: 0; overflow-y: auto;` but no `display: flex` — so they're flex items in a flex column parent (filling the height) but the content inside is regular block layout, not distributed. **Fix:** added `display: flex; flex-direction: column;` to BOTH classes in `public/styles.css` (2 properties each). All FOUR preview body classes are now fixed: `.opp-preview-body` (full-screen modal), `.bookmark-preview-body` (sidebar), `.search-popup-preview-body` (search popup), `.email-preview-body` (email). Cache buster is already at `?v=3.5.5`. **Verified:** served CSS shows the fix on both classes. **UNCOMMITTED** as of session end. **NEXT:** user hard-refreshes → opens bookmark sidebar → clicks a deal → preview pane should now stretch to fill the panel. If there's a FIFTH preview body class I haven't seen yet, the pattern to check is `flex: 1; min-height: 0; overflow-y: auto;` without `display: flex`. **Files:** `public/styles.css` (2 properties × 2 rules). Previous: Bump cache busters to v=3.5.5.

### Remaining TODO (Future Sessions)

**Scanner Custom Behavior Execution:** ✅ COMPLETED
- Create Task → inserts into tasks table with assignee
- Notify Users → inserts into notifications table
- Create Deal → inserts into opportunities, optionally creates follow-up task and notifies user
- Add Email Tags → inserts into mail_tag_assignments
- Add Project Tags → inserts into opportunity_tags (creates tags if needed)
- Change Project Stage → updates opportunities.stage_id with optional condition
- Reply to Email → creates draft or sends reply via SMTP

**Template Variable Engine:** ✅ COMPLETED
- String interpolation for `{subject}`, `{from}`, `{from_name}`, `{project}`, `{project_id}`, `{claim_number}`, `{date}`, `{body_preview}`

**Config Data Loading:** ✅ COMPLETED
- Users fetched from /api/v2/users
- Stages fetched from /api/v2/stages
- Email tags fetched from mail_tags with checkboxes

**Model Training (Deferred):**
- Waiting for OnlyOffice user field migration for more labeled data
- Auto-retrain after 10 corrections, auto-reload immediately

**CRM Mail Add-Account Flag:** ✅ CLOSED — Code trace verified correct, not a bug. (A) `oauth_providers.py`: `MicrosoftProvider.smtp_settings()` now accepts `email` and returns `smtp-mail.outlook.com:587` for personal Microsoft domains (`outlook.com`, `hotmail.com`, `live.com`, `msn.com`), keeping `smtp.office365.com:587` for work/tenant accounts; `imap_settings()` signature updated for consistency; `GoogleProvider` signatures updated. (B) `server.py` OAuth callback passes `email` to `smtp_settings()`/`imap_settings()`. (C) `_handle_mail_send` now inserts a sent-message copy into `mail_messages` (folder `Sent`, `is_read=TRUE`, synthetic `imap_uid`, generated `Message-ID`) and uses the returned `mail_messages.id` for `mail_deal_links`, fixing the FK violation caused by using `mail_outgoing.id`. (D) Admin "CRM Mail Accounts" list now filters to only accounts with `is_crm_mail === true`. (E) `CHANGELOG.md` and `AGENTS.md` updated. (E) **Verified:** `python3 -m py_compile server.py oauth_providers.py smtp_client.py` passes; server restarted clean (PID 1589456), `/api/config` returns 200. **NEXT:** user reconnects `aplus.estimates@outlook.com` via OAuth to pick up the corrected SMTP host; test send from both Bluehost CRM account and Outlook account. (A) **Mail-list row layout** (styles.css): expand-preview button now `order:8` (always last on desktop); narrow blocks (@container 720px + @media 600px) set `.mail-snippet{display:none}`, `.mail-subject{order:10; flex:1 1 100%}` (line 2), `.mail-row-tags{order:3}`, `.mail-date{order:4; margin-left:auto}`, `.mail-expand-btn{order:5; margin-left:0}` (last on line 1). Base `.mail-date` changed to `width:auto; min-width:105px; white-space:nowrap` so date+time never clips. (B) **Scanner fixes** (scanner/mail_scanner.py): `_find_opportunity_by_claim_code` now uses `field_id` (not `custom_field_id`) with dynamic lookup via `_claim_field_id()` (field_key `'field_11'` → id 72 from `custom_field_definitions`), wrapped in try/except → None. `_fetch_messages` line 378: `att.part` → `att.content_id` (fixed JSON serialization crash: `store_failed` for every attachment message). Per-account processed UIDs: `_mark_processed`/`_is_processed` keyed `f"{account_id}:{uid}"`; `_store_message` dedup adds `AND account_id=%s`. (C) **UI** (app.js + index.html): toolbar gear `#mail-account-settings-btn` added after refresh button, shown only when `acct.canManage`; `updateMailToolbarSettingsBtn()` called after renderMailTabs/switchMailTab/save. Tab-cog markup + delegated handler removed. OAuth status → `'Connected as ' + acct.email`. `account-settings-from-name` moved from manual SMTP section to top-level after Email Address (visible all auth methods). 30s auto-refresh `setInterval` in `openMailInboxModal`, cleared in `closeMailModal` + `minimizeEmailModal`. (D) **Verified:** node --check + py_compile pass; server PID 1420113 started clean; zero `store_failed`/`custom_field_id` errors; 146 stored messages (was 104), newest now Aug 2 07:30:23. Two transient `AUTHENTICATE failed` at startup (expired OAuth token, pre-existing timezone bug in refresh logic). (E) **Docs:** CHANGELOG updated, AGENTS updated. UNCOMMITTED: 8 modified + 4 untracked files. **NEXT:** browser-check (narrow layout, expand button last, subject line 2, date+time, toolbar gear, account settings, auto-refresh), then commit. **Previous session (45):** IMAP `A()` search fix, `message-id` from headers, `server.ehlo()` after starttls for SMTP XOAUTH2, `notification_dispatcher.py` crash (query_dicts), OAuth email fully working for `vanguardadjusting@outlook.com`.

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
- **⚠️ CRITICAL: Starting server from opencode shell.** The server process dies when backgrounded normally from the opencode shell. You **MUST** use `setsid bash -c '...'` to create a fully detached process group:
  ```bash
  setsid bash -c 'cd /home/chapman-server/Projects/new-crm && exec ./.venv/bin/python3 server.py >> /tmp/crm-server.log 2>&1' &
  ```
  Do NOT use plain `python3 server.py &` — it will appear to start but die silently when the opencode session exits or times out. The localtonet tunnel will then show "ConnectionRefused".
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