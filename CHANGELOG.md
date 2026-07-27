# Changelog

All notable changes to the Sietch CRM dashboard are documented here.

## Phase 3 — Email Scanner (2026-07-26)

- **Backend rename: `vanguard` → `sietch`.** All backend references updated: `SESSION_COOKIE` → `sietch_session`, portal name → `"sietch"` (30+ occurrences in `server.py`), Docker network `vanguard-internal` → `sietch-internal`, `notification_dispatcher.py` PORTAL → `"sietch"`, `db.py` fallback defaults → `sietch_crm`/`sietch`. Cold migration strategy — all existing sessions invalidated on deploy.
- **New DB tables.** Added `mail_tags`, `mail_tag_assignments`, `mail_templates` to `init.sql` (sections 7.14–7.15). Existing sections renumbered (7.13 Sync → 7.16, 7.14 Email Classifier → 7.17, 7.15 User Profiles → 7.18, 7.16 Branding → 7.19).
- **Scanner module (`scanner/`).** IMAP polling daemon using `imap_tools`, adapted from `email_scanner` branch classifier pipeline. Stores full emails in DB, classifies via rule-based pipeline with optional ML head (`sentence-transformers` + logistic/kNN). Files: `mail_scanner.py`, `train_ml_head.py`, `Dockerfile`, `docker-compose.scanner.yml`, `scanner_service.py`, `.env.example`, `requirements.txt`, `requirements-ml.txt`.
- **New API endpoints.** 15 `/api/v2/mail/*` routes added to `server.py`: inbox/messages CRUD, mark read/unread, link-to-deal, tag management, templates, accounts, scanner status/log/config/reprocess. All inside `KanbanHandler` class.
- **Frontend mail modal connected.** All stub functions in `app.js` replaced with real API calls: `loadMailMessagesForModal`, `loadMailAccountsForModal`, `fetchMailMessage`, `markMailMessageRead`, delete, mark-unread, link-to-deal. The existing modal scaffold (minimize-to-sidebar, search, select-all, expand/collapse) now functional.
- **Scanner admin panel (3B).** New "Email Scanner" tab in admin console: scanner status indicator, IMAP account list + add form, behavior toggles (create deals/tasks/notes/notify), scanner log viewer, reprocess endpoint. All connected to `/api/v2/mail/*` endpoints.
- **Import script.** `import_email.py` — Phase 4 stub for migrating old OnlyOffice CRM email data. Schema-compatible, no changes needed when migration time comes.
- **Docs.** `docs/MAIL_SCANNER_PLAN.md` (detailed spec), `migrations/001_add_mail_tables.sql`.
- **Files:** `server.py`, `db.py`, `notification_dispatcher.py`, `docker-compose.yml`, `config.example.env`, `init.sql`, `public/app.js`, `scanner/*`, `docs/MAIL_SCANNER_PLAN.md`, `import_email.py`, `migrations/001_add_mail_tables.sql`, `AGENTS.md`.

## Phase 3 — Email Module (2026-07-26)

- **Full email module build.** Modal renamed to "Email". Tabler icons throughout. Tabs in modal title bar (CRM Mail + user accounts + Admin). Compose button in toolbar with mail-plus SVG. Compact compose layout (fields < 1/3, body fills rest).
- **Backend rename: `vanguard` → `sietch`.** All backend references updated: `SESSION_COOKIE` → `sietch_session`, portal name → `"sietch"`, Docker network `vanguard-internal` → `sietch-internal`.
- **New DB tables.** `mail_tags`, `mail_tag_assignments`, `mail_templates`, `mail_account_access` (account sharing), `mail_outgoing` (sent tracking), `mail_contacts` (per-user). Extended `mail_accounts` with SMTP + OAuth + signature columns. Added `starred` to `mail_messages`, `attachments_json` to `mail_outgoing`.
- **30+ `/api/v2/mail/*` endpoints.** inbox, messages CRUD, mark read/unread, star, archive, move, link-to-deal, tags, templates, accounts CRUD, sharing, unread-count, trash-count, outgoing, send, undo-send, drafts, signature, contacts CRUD, contacts import/export, threads, status, log, config, reprocess.
- **SMTP sending with attachments.** `send_email_from_account()` in `smtp_client.py` reads per-account SMTP config from DB, supports file attachments (MIME multipart). `POST /api/v2/mail/send` sends email, stores in `mail_outgoing`, links to deal if `deal_id` provided. 5-second undo send with `POST /api/v2/mail/send/undo`.
- **Reply / Reply All / Forward.** Buttons in email detail view. Pre-fills compose modal with recipients, `Re:`/`Fwd:` subject prefix, quoted original body.
- **Per-account signatures.** Signature field in account settings modal (rich text). Auto-inserted in compose body on new messages.
- **Star/Flag.** Clickable star per message in list. Star column in header. `PUT /api/v2/mail/messages/{id}/star` toggle.
- **Archive.** Button in toolbar. `PUT /api/v2/mail/messages/{id}/archive`. Archive folder in sidebar.
- **Move to folder.** Dropdown in toolbar. `PUT /api/v2/mail/messages/{id}/move` with target folder.
- **Attachments.** File upload in compose (10MB per file, 25MB total). Base64 conversion and MIME attachment in SMTP send. Attachment metadata stored as JSONB.
- **Drafts.** Save Draft button in compose. Drafts folder shows draft messages from `mail_outgoing WHERE status='draft'`.
- **Templates.** Template picker dropdown in compose. Templates CRUD in Scanner Admin panel.
- **Contact database.** Per-user `mail_contacts` table. Autocomplete in To/Cc/Bcc fields. Import from CSV, export to CSV.
- **Conversation threading.** Group messages by `conversation_id`. Thread count badge. Toggle between flat and threaded views.
- **Scanner module (`scanner/`).** IMAP polling via `imap_tools`, classifier pipeline from `email_scanner` branch, ML head support, Dockerfile, compose, scanner_service.
- **Keyboard shortcuts.** `j`/`k` navigate, `Escape` deselect (only when email modal focused).
- **Dark mode contrast.** Lower opacity on folder buttons, tag items, tabs, toolbar ghost buttons, dropdown elements.
- **Files:** `server.py`, `db.py`, `notification_dispatcher.py`, `docker-compose.yml`, `config.example.env`, `init.sql`, `public/app.js`, `public/index.html`, `public/styles.css`, `scanner/*`, `smtp_client.py`, `docs/MAIL_SCANNER_PLAN.md`, `import_email.py`, `migrations/001_add_mail_tables.sql`, `AGENTS.md`.

## Phase 2G — project.html + Open Project button (2026-07-26)

- **Wired "Open in CRM" buttons to project detail page.** `crmOpportunityUrl(id)` now returns `/project/{id}` (was dead `#` stub). `crmTaskUrl(task)` extracts oppId from task and returns `/project/{oppId}`. All 5 call sites now open the standalone project page in a new tab.
- **Renamed "Open in CRM" → "Open Project".** Updated `createCrmOpenLink()` default title, plus 3 HTML buttons (task preview modal, deal-edit modal, quick-note modal).
- **Rewrote `public/project.html`** (390 lines). Dark theme via CSS variables from `styles.css`. Full project fields: stage, value, contact, due date, responsible, created, description, tags (High Priority highlight), custom fields (2-column grid). Add Note editor in "History & notes" section header — same location as preview modal. Contenteditable with format toolbar (B/I/U/S/lists/clear), submit to `POST /api/v2/projects/{id}/history` with `categoryId: 10`, re-fetches events after submit (no full reload). Mobile responsive (720px max-width, grid collapses).
- **Fixed `/project/{id}` URL rewrite.** Changed from silent internal rewrite to 302 redirect so the browser URL gets `?id=` in the query string (JS reads `window.location.search`).
- **Fixed stage showing `[object Object]`.** `stage` field from API is `{ title, id }` — now extracts `.title` before display.
- **Added @-mention autocomplete to project page note editor.** Fetches portal users, dropdown on `@`, keyboard nav (Enter/Tab/Escape), colored mention chips, extracts user IDs on submit and passes `notifyUserList` to API.
- **Added user fields to project page.** Fetches field definitions from `/api/v2/custom-fields` and saved values from `/api/v2/projects/{id}/custom-fields`, cross-references by fieldId to get proper labels. Handles checkbox (Yes/No), heading skip, and orphan values without definitions.
- **Fixed user field save from deal-edit and preview modals.** `PUT /api/v2/projects/{id}` now processes `customFieldList` array — writes each field to `opportunity_custom_field_values` with upsert. Previously the server ignored `customFieldList` in the PUT body, so user field changes were silently dropped.
- **Fixed FK constraint violation on project update.** `contactId: 0` and `responsibleUserId: 0` from the frontend are now converted to `NULL` before the DB UPDATE, preventing `opportunities_contact_id_fkey` violations when no contact/responsible user is set.
- **Fixed checkbox user field display.** `customFieldTypeCode()` now also checks `def.type` (server returns `{ type: 3 }` not `{ fieldType: 3 }`). Previously checkboxes always returned type 0 (text), so `formatCustomFieldValueForDisplay` returned `"true"` instead of `"Yes"`, and the checklist renderer showed ✗ instead of ✓.
- **Fixed checkbox uncheck not persisting.** Both `collectPreviewCustomFieldValues` and `collectDealEditCustomFieldValues` now send `"false"` for unchecked checkboxes instead of skipping them entirely. Previously, unchecking a checkbox sent nothing, so the old `"true"` value remained in the DB.
- **Files:** `public/project.html`, `server.py`.

- **Phase 2G — project.html + Open Project button (2026-07-26)**

- **Fixed checklist checkbox display in preview modal.** Root cause: `field_type='text'` in DB for Measurement Report, Insurance Documents, Inspection Photos (should be `'checkbox'`). Updated DB via SQL. CSS specificity bug: `.opp-preview-field-chk` (0,1,0) was overridden by `.opp-preview-field` (0,1,0) due to source order, making compact styles permanently invisible. Fixed both issues: changed selector to `.opp-preview-field.opp-preview-field-chk` (0,2,0) and `.opp-preview-field.opp-preview-field-chk .opp-preview-chk-yes/no` for full override. Also added safety net in checklist renderer to render `"true"` as checked. Commits `213298a`, `302e610`.

## Phase 2G — User Profile + Notifications ✅ COMPLETE

Target release on `new-crm` branch.

### Phase 2G (2026-07-24 session 23)
- **Unified note editor toolbar across all modals.** Deal-edit and quick-note modals now have the same formatting toolbar as the preview modal: bold, italic, underline, strikethrough, bullet/numbered lists, highlighter color picker, emoji, link, clear formatting, and backdate calendar button. All three editors share identical UI and behavior.
- **Backdate button moved into formatting toolbar.** Calendar icon button is now the last button in the toolbar row for all three editors (preview, deal-edit, quick-note). Removed separate `.note-backdate-wrap` divs from deal-edit and quick-note modals.
- **Global delegated handlers for highlighter/emoji/link buttons.** Added document-level click handlers for `.note-format-hilite-btn`, `.note-format-emoji-btn`, `.note-format-link-btn` so they work in all note editors (preview, deal-edit, quick-note).
- **Fixed preview modal note editor layout.** Editor section now renders after History & notes title and Quick Context button (was rendering above them). Fixed "strange semi-colon" text node artifact caused by leading newline in template literal — switched to string concatenation.
- **Fixed group tile count + filter summary wrapping.** Deal count and filter summary text now share one flex row via `.group-tile-count-summary` wrapper instead of being separate children that wrap to different rows.
- **Add Tile button icon updated.** Header "Add tile" button now uses Tabler `layout-grid-add` icon (3 squares + plus sign).
- **Files:** `public/app.js` (unified toolbar, global delegated handlers, count+summary wrapper), `public/styles.css` (`.group-tile-count-summary` flex rules, removed stale `.note-backdate-wrap`/`.note-backdate-label`), `public/index.html` (deal-edit + quick-note toolbar HTML, Add Tile icon).
- **Preview modal "Add Note" editor.** Added inline expandable rich text note editor inside the opportunity preview modal. "Add Note" button in the History & notes section title opens a formatted editor with enhanced toolbar: bold, italic, underline, strikethrough, bullet/numbered lists, highlighter color dropdown (6 dark-theme-safe colors), emoji picker, link insertion, and clear formatting. Note submission reuses `createOpportunityHistoryEvent` with category 10, @-mention support, and backdate. Preview auto-refreshes after submission.
- **Highlighter color dropdown.** New floating palette (`.preview-highlighter-dropdown`) with 6 swatch buttons (green, yellow, blue, pink, orange, purple at 20% opacity) for highlighter formatting in the preview note editor.
- **Emoji picker for contenteditable.** Adapted `showNotesEmojiPicker` pattern for contenteditable divs — inserts via `document.execCommand("insertText")` instead of textarea splice.
- **Link insertion.** New inline URL input with Apply/Cancel buttons, auto-prefixes `https://` if missing, wraps selected text or inserts full URL as `<a>` link.
- **Preview modal header buttons.** Removed refresh button (⟳) from header; added close (X) button matching search modal style (`class="btn btn-icon-only modal-header-control"` with `ti ti-x`).
- **Backdate icon button in deal-edit and quick-note modals.** Replaced visible `<input type="date">` with calendar SVG icon button + hidden native date input + label tooltip. Delegated click/change handlers at document level. Label shows formatted short date (e.g., "Jul 24") on change. Preview note editor also includes backdate icon.
- **Fixed preview modal field layout.** `.opp-preview-field` changed to `flex-direction: column` so labels and values stack. Checklist fields remain row direction.
- **Fixed task/team tile collapse.** Added `min-height: 0 !important` for `.tasks-panel.tile-body-collapsed` and `.presence-panel.tile-body-collapsed .presence-tile-body` so collapsed panels don't consume grid space.
- **Fixed user profile avatar save.** Added `&_t=${Date.now()}` cache-buster to img `src` to bypass 24h HTTP cache after photo changes.
- **Delete photo button icon-only.** Removed "Delete Photo" text from button, kept `title` attribute.
- **Files:** `public/app.js` (addNoteBtn handler, _toggleHighlighterDropdown, _showPreviewEmojiPicker, _showPreviewLinkInput, backdate delegated handlers, removed refresh handler, added close handler, tile collapse min-height fix, avatar cache-bust), `public/styles.css` (addNoteBtn, preview-note-editor-wrap, highlighter dropdown, link input, backdate btn/label/wrap, mention-dropdown positioning, format-sep), `public/index.html` (cache-bust v2.2.0, removed refresh button, added close button).
- Cache-bust bumped to `app.js?v=2.2.0`, `styles.css?v=2.2.0`.

### Phase 2G (2026-07-24)
- **Notification creation logging.** Added error logging to notification creation loop in `_handle_project_history_create()` — `except Exception: pass` replaced with `print()` + `traceback.print_exc()` to stderr. This will reveal the actual DB error that has been silently swallowing all notification INSERT failures.
- **Fixed preview modal field layout.** Changed `.opp-preview-field` from row to column flex direction with `gap: 0.25rem`. Removed `flex-shrink: 0` and `white-space: nowrap` from `dt` so labels and values stack vertically on narrow viewports. Checklist fields (`.opp-preview-field-chk`) remain in row direction.
- **Fixed notification creation category filter.** Added category ID 10 (Note in new DB) to the filter that gates notification creation — was `(1, 8)` (old OnlyOffice IDs), now `(1, 8, 10)`. This was the root cause of zero notifications being created despite successful history inserts.
- **Notification drawer improvements.** (a) Badge syncs immediately on drawer open. (b) Notification items now show full message as primary text ("X tagged you in a note on Y") with a clean text snippet of the note below. (c) Expand button for full note now renders HTML (mentions show as colored @Name) instead of raw tags. (d) Replaced unrecognized tag-edit icon with @ icon for note_tagged notifications. (e) Server stores stripped-HTML snippet in notification payload (first 120 chars of note content).
- **Removed "minimized tile(s) skipped" from dashboard status text.** Only open opportunities and open tasks are shown now.
- **Fixed @-mention notifications trigger timing race.** The PostgreSQL `AFTER INSERT` trigger on `history_events` fired during `db.insert_returning()` before `history_notify_users` was populated — zero notifications were ever created. Replaced the trigger with explicit Python notification creation in `_handle_project_history_create()` after both tables are populated. Removed `create_tag_notifications` function and trigger from `init.sql`.
- **Fixed @-mention notifications frontend filter.** `isGuid()` filter on `notifyUserList` silently rejected integer user IDs (old OnlyOffice UUID format). Now accepts both GUIDs and numeric IDs.
- **Fixed dashboard tile grid snapping.** Panel tiles (feed/tasks/presence) had `min-height: 0` which let them shrink below the 400px grid row. Restored `min-height: var(--tile-row-height)` so they fill a full row like notes/calendar tiles. Group tiles capped at `max-height: var(--tile-row-height)` with scrollable filter header and board — entire tile scrolls internally instead of overflowing the grid.
- **Multi-row tile resize.** Tiles can now be resized to 3, 4, or up to 8 rows tall (was binary: normal/double). Resize drag uses continuous row count based on CSS `--tile-row-height` variable. Ghost preview and layout classes (`tile-triple`, `tile-quad`) support arbitrary row counts. Tall button toggles between 1 and 2 rows.
- **Three-column width.** Tiles can now be 3 columns wide (was: 1/2/4 only). Removed the explicit span-3 ban in resize logic. `tile-three` CSS class now functional.
- **Group tile toolbar layout.** Filter toggle and template buttons moved from nested `.tile-toolbar-tools` wrapper to direct toolbar children, positioned right of the title text. On narrow tiles (<550px), deal count and filter summary wrap to a second row while buttons stay with the title. Close/minimize buttons always pinned far right.
- **Cleaned up @-mention dead code.** Removed `_mentionSyncHidden()` (no-op), `.mention-chips` containers and hidden inputs from HTML, `.mention-chips` CSS. Mentions are now inline `<span class="mention-inline">` in the editor content.
- **Fixed bot user display names.** `bot@vanguardadj.com` → "CRM Bot", `test@example.com` → "Test User" (were empty/null, causing @-mention dropdown to show numeric IDs).
- **Files:** `server.py` (notification creation in Python), `init.sql` (removed trigger), `public/app.js` (isGuid fix, tileHeight/setTileHeight numeric, tileWidth/setTileWidth "three", applyTileLayoutClasses multi-row, resize continuous rows, mention cleanup, group toolbar layout), `public/styles.css` (panel min-height, group tile max-height, tile-triple/tile-quad, multi-row overrides, container query update), `public/index.html` (removed mention-chips containers and hidden inputs).
- Cache-bust bumped to `app.js?v=2.1.0`, `styles.css?v=2.1.0`.

### Phase 2G (2026-07-23)
- **User Profile Modal.** New profile button (user-square-rounded icon) in header opens modal with: avatar upload (Pillow 200×200 thumbnail), display name edit, email (read-only), password change (uses existing `/api/v2/auth/change-password` endpoint).
- **Avatar storage.** Profile pictures stored in `data/avatars/{user_id}/avatar.jpg` with 64×64 thumbnail. Initials circle fallback with name-hash color. Endpoints: `GET/POST/DELETE /api/v2/my/avatar`.
- **@-mention autocomplete.** Replaced old `<select multiple>` notify selects with modern @-mention autocomplete in note editors (deal-edit + quick-note). Type `@` → dropdown filters portal users → select inserts styled chip → on submit, `extractMentionsFromContent()` parses chips → user IDs sent as `notifyUserList`.
- **Notification preferences.** New `GET/PUT /api/v2/me/notification-prefs` endpoints. Frontend toggles for in-dashboard and Telegram notifications. Telegram checkbox is dormant until new system is fully active.
- **Feed tile updated.** Now tries `/api/v2/notifications` first (only @-mentions + task assignments), falls back to old history scan if endpoint unavailable.
- **Notification dispatcher updated.** Checks `notification_preferences.telegram` before sending, skips customers, adds project link (`{DASHBOARD_URL}/project/{id}?event={event_id}`) in Telegram messages.
- **Lightweight project detail page.** New `public/project.html` — requires session cookie auth, shows full project details + event history. `?event={event_id}` param highlights specific event. URL rewrite: `/project/{id}` → `project.html?id={id}`.
- **Files:** `init.sql` (avatar_url column), `server.py` (avatar/preferences endpoints, project rewrite), `notification_dispatcher.py` (preference checks, project links), `public/app.js` (profile modal, @-mention, feed rewrite), `public/index.html` (profile button + modal, hidden inputs), `public/styles.css` (profile modal, mention chips), `public/project.html` (new), `AGENTS.md`, `CHANGELOG.md`.
- Cache-bust bumped to `app.js?v=2.0.0`, `styles.css?v=2.0.0`.

## Phase 2E — Photo gallery ✅ COMPLETE

Target release v2.2.5 on `new-crm` branch.

### v1.95.17
- **Fixed OnlyOffice Document Server editor config for 7.1+.** `document.key` is now a plain unique string (`sietch-doc-{id}-{ts}`) instead of a JWT. OnlyOffice 7.1+ requires `document.key` as a plain string inside the signed editor-config token; the previous JWT value caused "auth missing required parameter document.key" errors and prevented documents from opening.
- **Made Document Server internal URL production-safe.** `_effective_docs_internal_url()` uses `DOCS_INTERNAL_URL` when it looks intentionally configured (not the placeholder `http://docserver:8080`), and falls back to `DOCS_PUBLIC_URL` in standalone dev or when the internal URL is empty/placeholder. This supports both co-located Docker setups and production deployments where Document Server lives on a separate droplet.
- **Removed hardcoded `DOCS_INTERNAL_URL` from `docker-compose.yml`.** The value now comes from `.env`, so production can set it to the public URL (or a real internal URL) instead of the broken local-only `http://docserver:8080` placeholder.
- **Added Document Server restart control to admin Infrastructure tab.** New `POST /api/v2/admin/restart-docserver` endpoint and "Restart Document Server" button use the configurable `DOCSERVER_CONTAINER_NAME` env var (default `onlyoffice-docserver`). Renamed the existing button to "Restart dashboard container" for clarity. On a production droplet where Document Server runs elsewhere, the restart will simply report that the container is not found locally.
- **Files:** `server.py`, `public/index.html`, `public/app.js`, `docker-compose.yml`, `config.example.env`.
- Cache-bust bumped to `app.js?v=1.95.17`, `styles.css?v=1.87.30`.

### v2.2.5
- **Rebrand active codebase and docs from "Vanguard" to "Sietch CRM".** Updated user-facing strings in `smtp_client.py`, `telegram_bot.py`, `auth.py`, `db.py`, `init.sql`, `migrate_from_onlyoffice.py`, `README.md`, `Toaster_Features`, `big_CRM_plan.md`, `FUTURE_FEATURES.md`, `server.py`, and `public/index.html`. Active infrastructure docs (`AGENTS.md`, `docs/DASHBOARD_INFRASTRUCTURE.md`, `docs/UPDATE_AND_DEPLOY.txt`, `docs/PRODUCTION_SERVER_NOTES.txt`, `DEPLOY.md`, `docs/crm-telegram-bot.service`, `docs/TELEGRAM_BOT_UPDATES.md`) now use the `sietch-crm` container name and `dashboard.publicadjustermidwest.com` production domain.
- **Removed hardcoded admin credential references.** Eliminated `kenc@vanguardadj.com`, `bot@vanguardadj.com`, and `FRi3tz4yWXrMTEZ` from code comments and active docs. `BOT_CRM_EMAIL` / `BOT_CRM_PASSWORD` are now documented as obsolete; Telegram bot only requires `TELEGRAM_BOT_TOKEN`.
- **Historical release notes and migration runbooks left untouched** (`docs/RELEASE_*.md`, `docs/MIGRATE_DOMAINS.md`, `docs/CUTOVER_RUNBOOK.md`, `CHANGELOG.md` older entries, `onlyoffice-module/`).
- **Note:** Phase 2E photo gallery and Document Server fixes in v1.95.16/v1.95.17 are implemented and server-tested but not yet verified in browser.

### v2.2.5-1
- **Fixed tile resize regression.** Restored `pointer-events: auto` on `.tile-resize-handle` after commit `8bcd106` accidentally set it to `none`, making resize handles visible but undraggable. No JS changes needed.
- **Files:** `public/styles.css`.

### v2.2.5-2
- **Added photo batch actions (download, move, copy, delete).** Photos tab now uses a single **Actions…** dropdown (matching the Documents modal) with Download, Move to…, Copy to…, and Delete options. Selecting multiple images via checkboxes enables the menu.
  - Backend: added `GET /api/v2/photos/{id}?download=1` attachment disposition, `POST /api/v2/photos/batch-download` (ZIP of originals), `POST /api/v2/photos/batch-move`, `POST /api/v2/photos/batch-copy`, and `POST /api/v2/photos/batch-delete`.
  - Frontend: Actions dropdown + select-all checkbox + inline destination project search panel for move/copy.
  - Batch copy duplicates files and assigns the current user as `uploaded_by`; move updates `opportunity_id` and clears `folder_id`; delete soft-deletes selected rows.
- **Improved Document Server self-signed certificate UX.** `public/doc-editor.html` now shows a clickable healthcheck link and a reload button when the OnlyOffice `api.js` script fails to load, guiding users to accept the certificate before retrying.
- **Unified Documents tab in preview modal with Documents-modal list UI.** Replaced the separate toolbar buttons with a single **Actions…** dropdown (Download, Move to…, Copy to…, Delete) plus a select-all checkbox. Document rows now match the full Documents modal style: checkbox, file-type icon, title, size, date, and hover download button. No directories/folders in the preview tab.
- **Admin console sidebar toggle redesign.** Replaced thick text arrows (`▶`/`◀`) with a clean inline SVG chevron. Toggle is now absolutely positioned at the `.admin-body` boundary, replacing the vertical border divider. Works on all screen sizes (not just mobile). Collapses/expands with smooth left-slide animation. Soft `--text-muted` color for dark mode.
- **Files:** `server.py`, `public/app.js`, `public/styles.css`, `public/doc-editor.html`.

### v1.95.16
- **Added Photos tab to opportunity preview modal.** New "Photos" tab alongside Details/Documents with an image grid, upload button, per-project quota display, thumbnail generation, and delete-on-hover.
- **Photo upload endpoint implemented.** `POST /api/v2/projects/{id}/photos` accepts multipart/form-data image uploads, persists to `data/photos/{oppId}/`, generates a 400px JPEG thumbnail with Pillow, extracts EXIF metadata, and enforces the 150MB per-project quota trigger.
- **Photo serving endpoints.** `GET /api/v2/photos/{id}` returns the original image; `GET /api/v2/photos/{id}?thumbnail=1` returns the thumbnail.
- **Lightbox viewer.** Click any thumbnail to open an overlay with full-size image, caption (filename + size), prev/next arrows, keyboard navigation (Escape/←/→), and backdrop click to close.
- **Backend:** added Pillow import, `_extract_exif` helper, `_handle_project_photo_upload`, `_handle_photo_download`, image extensions to `_EXT_TO_MIME`.
- **Files:** `server.py`, `public/index.html`, `public/app.js`, `public/styles.css`.
- Cache-bust bumped to `app.js?v=1.95.16`, `styles.css?v=1.87.29`.

## Phase 2A — Search modal expansion (FEAT-007 Phase D) ✅ COMPLETE

Target release v2.2.4 on `new-crm` branch.

### v1.95.15
- **Inline project-fields editor in opportunity preview modal.** Added an edit button next to the "Project fields" title. Clicking it switches the section into an editable form with stage, contact, responsible, value, follow-up due date, private, and all custom/user fields. Uses the same input types as the create-project modal (e.g., date picker for Date of Loss). Check / X buttons save or cancel.
- **Backend:** extended `updateOpportunityBulk` and `buildOpportunityPutBody` to support contact/responsible/bidValue/isPrivate.
- **Files:** `public/app.js`, `public/styles.css`, `public/index.html`, `server.py`.
- Cache-bust bumped to `app.js?v=1.95.15`, `styles.css?v=1.87.28`.

### v1.95.14
- **Search modal now opens as a filterable project directory.** Loads the first 50 open projects on open with server-side pagination (Prev / Next / page info / total count).
- **Full-text search across deal and user fields.** `filterValue` now searches `title`, `description`, `contact` (first/last/company), and all custom-field values via `ILIKE '%q%'` on both the header search bar and the search modal. Header search automatically upgraded to use the new behavior.
- **Server-side filters.** Stage, owner, tag, and custom-field filters are all sent to the backend; the previous client-side stage/owner filtering was removed.
- **Custom/user field filters.** Dynamic filter rows in the search modal: select a custom field, then a type-specific value control (text contains, select exact, checkbox Yes/No, date exact). Multiple filters are combined with AND logic.
- **Tag filter merged into Projects tab.** The standalone Tags tab was removed; tag filtering now lives in the main filter bar alongside stage/owner/sort.
- **Sort options added.** Newest, oldest, title A–Z / Z–A, bid high–low / low–high, stage A–Z.
- **"+ Tab" button adds preview in background.** Clicking a project row still opens and switches to the preview tab; the "+ Tab" button adds a preview tab without leaving the search list.
- **Removed "Open in CRM" links from search results and header search dropdown.** The dashboard is independent of OnlyOffice, so deep-linking to the old CRM is no longer needed.
- **Reduced contrast on search result title and checkbox.** Title now uses `font-weight: 500` and a softer `#b8c0d0` color; checkbox is scaled down and muted.
- **Backend:** extended `GET /api/v2/projects` with `customFieldFilters`, `tagId`, `sort_by=stage`, and full-text `filterValue`; added `GET /api/v2/projects/count`; added trigram (GIN) indexes for fast `ILIKE` on text fields.
- **Files:** `server.py`, `init.sql`, `public/index.html`, `public/app.js`, `public/styles.css`.
- Cache-bust bumped to `app.js?v=1.95.14`, `styles.css?v=1.87.27`.

### v1.95.13
- **Moved Tasks tile user filter into the title bar.** The `<select id="tasks-user-filter">` was previously in a separate `.panel-header` row below the toolbar with a "User" label. Removed the label and the panel-header row; the select now lives in `.tile-toolbar-tools` via a new `ensureTasksUserFilter()` helper. Added compact styling (`.tasks-user-filter-select`) so it matches toolbar height and does not thicken the title bar.
- **Fixed Team/Tasks tile toolbars wrapping on narrow/mobile widths.** The `@container (max-width: 550px)` rule forced `.tile-toolbar-tools` to a second row for all tiles, which pushed the Team status dropdown and Tasks user selector below the title bar on mobile. Added overrides to keep those tools on the title row, set `flex-wrap: nowrap` on the Team/Tasks toolbars, and reduced select max-width/padding in the mobile breakpoint.
- Cache-bust bumped to `app.js?v=1.95.11`, `styles.css?v=1.87.23`.

### v1.95.12
- **Fixed Team tile Messages tab showing "No messages yet" when messages exist.** Two-part root cause: (1) the `/api/presence` endpoint did not include `myRecentDms` or `lastReadDms` in its response, so both the tile and popup Messages tabs had no inbox data; (2) `renderPresenceTileCompact` only re-rendered the Team tab body when fresh snapshots arrived, so the Messages tab never updated even after the server started returning data. Updated `server.py` `_handle_presence_get` to return `myRecentDms` and `lastReadDms`, and updated `renderPresenceTileCompact` to refresh `#presence-tile-messages-body` when it is visible.
- **Fixed Team tile Messages tab shrinking.** Removed `max-height: 280px` from `.dashboard-tile.presence-panel .presence-tile-body` and set `min-height: 240px` so the body fills the tile height instead of collapsing when the inbox is empty.
- Cache-bust bumped to `app.js?v=1.95.10`, `styles.css?v=1.87.21`.

### v1.95.11
- **Fixed Team popup Messages tab shrinking.** Added `min-height: 320px` to `.presence-list` so the modal content area keeps a stable height even when the inbox is empty.
- **Fixed Messages tab empty state.** When there are no recent DMs, the Messages tab now shows the team roster with a "Select a team member to start messaging" header so users can open a DM thread directly from the Messages tab instead of switching to Team and back.
- Cache-bust bumped to `app.js?v=1.95.8`, `styles.css?v=1.87.20`.

### v1.95.10
- **Fixed preview tag dropdown cut off when no tags.** The tag add dropdown used `.template-dropdown`'s default `right: 0` positioning, so when no tag chips were present the + button sat at the left edge of the preview modal and the menu opened to the left, extending past the modal border. Added `.opp-preview-tag-add-wrap .template-dropdown { left: 0; right: auto; }` so it always opens to the right and stays visible.
- Cache-bust bumped to `styles.css?v=1.87.19`.

### v1.95.9
- **Fixed spikey tile corner artifacts (root cause).** Commit `fe4ccf8` removed `.dashboard-tile.board-group-tile { overflow: hidden; }` to avoid clipping the kanban horizontal scrollbar, but that exposed square-corner artifacts at tile corners. Reverted that removal: `.board-group-tile` once again has `overflow: hidden` so children clip to the tile's `border-radius`. To keep the horizontal scrollbar visible, restored `padding-bottom: 1rem` on `.dashboard-tile.board-group-tile .board` (and `.local-kanban-tile .board`) so the scrollbar sits above the rounded bottom corners instead of being clipped.
- Cache-bust bumped to `styles.css?v=1.87.18`.

### v1.95.8
- **Fixed squared-off artifacts on tile corners (follow-up).** Shrunk resize handles from 22x22px to 12x12px and moved them 6px inward from corners so they sit entirely within the tile's border-radius zone. Added `pointer-events: none` so handles are truly non-interactive at all times. Mobile handles now 18x18px (was 28x28px).
- **Minimized tiles now uniform height (follow-up).** Team/Presence tile: KEEP status dropdown visible when collapsed, hide tabs (`presence-tile-tabs-inline`). Override `.panel-header` padding from `0.65rem 1rem 0` + `margin-bottom: 0.5rem` to `0.45rem 0.65rem` + `margin-bottom: 0` to match standard `.tile-toolbar`. Open Pipeline: hide filter summary (`.group-filter-summary-compact`) when collapsed. Notes tiles: explicit `padding: 0.45rem 0.65rem` on `.notes-tile-bar` when collapsed to ensure uniform toolbar height matching Tasks tile reference.
- Cache-bust bumped to `styles.css?v=1.87.17`.

### v1.95.7
- **Fixed grid gaps and erratic resize on all tile types.** Root cause of gaps: grid had `align-items:start` which prevented shorter tiles from stretching to fill their row height. When one tile in a row was tall (e.g. Team), shorter tiles (Tasks, Notifications) left visible empty gaps below them. Removed `align-items:start` so tiles use the default `stretch`, filling the entire grid row. Collapsed tiles already override with `align-self:start`.
- **Fixed erratic resize on all tile types.** Root cause: `bindTileResize` used the CSS variable `--tile-row-height` (400px) as the reference for the height decision boundary and ghost preview. With `grid-auto-rows: auto`, actual row heights vary. When a tile sat in a row taller than 400px, the boundary was wrong — the ghost showed incorrect dimensions and the normal/double toggle triggered at wrong positions. Fix: compute `singleRowH` from the actual rendered tile height at pointerdown. For normal tiles, `singleRowH = baseHeight` (tile stretches to row height). For double tiles, back-compute `(baseHeight - gapWidth) / 2`. Ghost preview and height boundary now use `singleRowH` and `baseHeight` instead of the CSS variable.
- **Calendar rendering chunked into async batches.** `renderCalendarMonthBody` now renders 1 week per `requestIdleCallback` (with `setTimeout` fallback) so the main thread stays responsive during calendar paint. The grid header + weekday row render immediately; each of the 6 week rows is appended via `insertAdjacentHTML` in its own idle callback. Month label, event count, and click handlers are bound after the final week renders. Also deferred the `loadCalendarForTile` render call via `requestAnimationFrame` so ICS fetch completion doesn't block the initial dashboard paint.
- **Group tile height cap and vertical scroll.** `.board` inside board-group-tiles now has `max-height: var(--tile-row-height)` (normal) or `calc(var(--tile-row-height) * 2 + 1.25rem)` (double) with `overflow-y: auto`. Tall columns now scroll vertically instead of pushing the tile to unbounded height. Tile gets `overflow: hidden` to clip board overflow. This fixes the "won't shrink" bug — the tile can now shrink back to normal height because content is capped.
- **Local kanban horizontal scroll restored.** Reverted `overflow-x: hidden` — columns can now be scrolled horizontally. Board height is capped by the tile's grid row (same as group tiles).
- **Panel tile content fills tile height.** Feed (notifications) and Tasks tiles now have their content list (`feed-list` / `tasks-by-user`) set to `flex:1` inside a flex-column `.tile-body-content` override, so "No open tasks" or "Loading notifications" fills the tile instead of leaving empty space below the bordered box. Overflow scrolls when content exceeds tile height.
- **Reverted `align-items: start` on grid.** Tiles keep their natural height; double-height group tiles no longer pull neighbors up to match. Gaps between rows are acceptable when no tile fits.
- **Team tile toolbar consolidated.** Moved status dropdown, tabs, admin button, unread flash, and popup button from the `panel-header` into the tile-toolbar (tools/actions sections). The Team tile now has one toolbar row matching the compact height of Tasks and Notifications. Tabs are inline in the toolbar via `.presence-tile-tabs-inline`.
- **Group tile grid fix.** Removed `overflow:hidden` from board-group-tile — it was clipping the board's horizontal scrollbar needed for kanban column navigation. Board handles its own overflow via `overflow-x:auto; overflow-y:auto`.
- Cache-bust bumped to `app.js?v=1.95.7`, `styles.css?v=1.87.15`.

### v1.95.5
- **Fixed gaps between tiles (follow-up).** Restored `grid-auto-flow: dense` so later tiles backfill holes — tight masonry-style packing is the expected tiling behavior. Safe now that resize commits once per gesture and the other jumpiness causes are gone. Also root-caused thin dead strips around feed/tasks/presence tiles: the legacy `.panel` class carried `margin: 0 auto 1.25rem` (full-width top-panel era), which shrank + centered those tiles inside their grid areas — added `.dashboard-tile.panel { margin: 0; max-width: none; }` so they stretch to fill their cells.
- **Fixed erratic tile jumping during resize and drag.** Root causes: resize committed span classes + localStorage on every mousemove; the snap threshold was re-measured from the just-reflowed grid each event; SortableJS `forceFallback` floated a detached clone over the grid-locked real tile; every tile animated `transform` with `will-change`. See ISSUES.md ISSUE-013.
- **Resize is now ghost-preview + commit-on-release.** Dragging a corner handle moves only a dashed accent outline (clamped inside the grid); the tile's span class and storage save apply exactly once on pointerup, only if the span changed. Unified mouse/touch via pointer events.
- **SortableJS native drag** (`forceFallback: false`) — the dragged tile stays inside the grid flow and neighbors shift once with the 200ms animation. Sortable instance is created once, not destroyed/recreated on every mount.
- **`mountDashboardTiles()` no longer tears down the DOM.** Tiles are reordered in place via `appendChild` (event listeners, scroll positions, and iframe contents survive); only tiles removed from the layout are swept.
- **Removed dead `toolbar.draggable = true`** from the four tile-chrome creators (pre-Sortable leftover that stole native drags from the tile).
- **Removed `will-change: transform` / transform transition** from all tiles (hover shadow/border transitions kept).
- Cache-bust bumped to `app.js?v=1.95.5`, `styles.css?v=1.87.13`.
- Known follow-up: `renderBoardGroups()` still rebuilds every group tile on data refresh (loses scroll/filter DOM state) — deferred, see ISSUE-013.

### v1.95.6
- **Height resize added to corner handles.** Drag vertically to toggle normal/double height (grid-row span). Ghost preview shows target height; commits on pointerup only (single save).
- **Click-to-edit titles, no more pencils.** All tiles (groups, notes, calendars, local kanban) now enter edit mode by clicking the title text directly. Enter or blur saves; Escape reverts. The edit input only exists while editing and sizes itself dynamically to the text (ch units) so it never pushes buttons to another row.
- **Uniform toolbar layout across all tiles.** New containers: `.tile-toolbar-tools` (tile-specific buttons: filter, template, format, new-task, eye, calendar nav, etc.) and `.tile-toolbar-actions` (Refresh, Minimize, Close). Actions are always last with `margin-left:auto`.
- **Narrow + mobile row behavior.** On `.tile-quarter`, `.tile-half`, and viewport `≤600px`, actions stay on row 1 (top-right); tools drop to row 2. Full-width tiles remain single-row.
- **Consistent button ordering.** When present: Refresh first in actions, then Minimize, then Close. All insertion points (`attachTileCollapseButton`, `ensureTileAutoRefreshButton`, feed eye, tasks buttons, archive/remove) now target the containers.
- **Calendar titles fixed.** Replaced the always-visible static-width input with the same click-to-edit pattern used by groups/notes.
- **Cleanup.** Removed `ensureNotesToolbarRows` and its call sites + related dead CSS (notes-layout-top-row, notes-tile-name hacks, `.btn-edit-group-name` creation).
- Cache-bust bumped to `app.js?v=1.95.6`, `styles.css?v=1.87.14`. See also ISSUE-014.

## Phase 2D-2 — Tile layout redesign

### v1.95.4 (latest)
- **Removed all auto-pinning:** Feed, Tasks, and Team tiles are no longer pinned to a special top row. All tiles are now normal CSS grid tiles in the main `#dashboard-tiles` container.
- **Removed dead infrastructure:** Deleted `#dashboard-panel-row`, `#dashboard-tiles-pinned`, `mountPanelTile`, `syncPanelRowLayout`, `ensurePanelToolbarCount`, `ensurePanelLayoutButtons`, `ensurePanelPinButton`, and all `PINNED_TILE_IDS`/`PANEL_TILE_IDS` constants.
- **Fixed SortableJS:** Now properly destroys old instances before re-creating on `mountDashboardTiles()`. Single instance on `#dashboard-tiles`. No more erratic drag behavior.
- **Fixed grid layout:** Added `grid-auto-flow: dense` to prevent gaps when mixing tile widths.
- **Removed chrome-row wrapper:** Toolbar is now a single row with extras inline. No more two-row layout on desktop.
- **All tiles resizable:** Feed, Tasks, and Team tiles now get resize handles (no longer skipped by pin check).
- **Task tile buttons fixed:** "New task" and archive buttons now insert next to count badge.
- **Mobile logo:** Already positioned correctly (left side of first row).
- Cache-bust bumped to `v=1.95.4`.

- **Drag-to-resize handles:** Removed quarter/half/full/double layout buttons from tile toolbars. Added resize handles in bottom-left and bottom-right corners of tiles. Drag to resize width (snaps to quarter/half/full) and height (normal/double). Ghost outline shows target size during drag. Mobile-friendly with larger touch targets (28x28px).
- **Pin button:** Added thumbtack pin button to all tile toolbars. Pinned tiles appear in a separate row above unpinned tiles. Feed, Tasks, and Presence default to pinned (preserving current layout). Any tile can be pinned/unpinned. Pin state persisted in profile.
- **Generalized pinning:** Removed hardcoded `PANEL_TILE_IDS` restriction. Any tile can now be pinned to the top row. `isTilePinnedToTop()` checks `state.tileLayout.pinned[tileId]` for all tiles.
- **SortableJS cross-container drag:** Pinned and unpinned tiles share a SortableJS `group`, enabling drag between the pinned row and main grid. Dragging a tile out of the pinned row unpins it; dragging into the pinned row pins it.
- **Group tile click-to-edit title:** Group tile name is now static text with a pencil edit button. Click the pencil to edit, Enter/blur to save, Escape to cancel. No more always-visible input field.
- **Filter toggle icon:** Replaced "Show filters"/"Hide filters" text with Tabler filter icon. Outline when collapsed, filled when expanded.
- **Template dropdown consolidated:** Replaced template `<select>` + save icon + delete icon with a single template icon button. Opens a popover with "Save current as template", list of templates (click to apply), and "Delete templates…".
- **Nuke cache icon:** Changed group tile refresh icon from snowflake to Tabler refresh icon. Tooltip changed from "Nuke Cache (refresh tile)" to "Clear Cache and Refresh".
- **Fix horizontal scroll on group tiles:** Changed `.tile-body-content` overflow from `hidden` to `overflow-x: auto; overflow-y: hidden` so kanban board horizontal scroll works.
- **Fix minimize animation:** Collapse/expand now toggles the `tile-body-collapsed` class directly without full re-render, allowing the CSS `grid-template-rows` transition to play smoothly.
- Cache-bust bumped to `v=1.95.4`.

## Phase 2D — Dashboard layout overhaul (Odysseus-style)

- **SortableJS integration:** Replaced native HTML5 drag-and-drop with SortableJS for smooth 200ms reorder animations. Tiles use ghost/chosen/drag CSS classes during reorder. No more full re-render on drop — tiles animate to new positions. Drag handle is the `⋮⋮` hint in each tile toolbar.
- **Responsive grid breakpoints:** Added mid-tier tablet breakpoint (601–1024px → 2 columns). Mobile breakpoint moved from 900px to 600px (single column). Desktop remains 4-column grid.
- **`.tile-three` CSS class:** Added `grid-column: span 3` for three-quarter-width tiles.
- **Smooth collapse animation:** Tile collapse/expand now uses `grid-template-rows` transition (0.3s cubic-bezier) instead of instant `display: none`. Content wraps in `.tile-body-content` div for smooth height animation.
- **Hover glow:** Tiles now show a subtle purple box-shadow (`0 4px 16px rgba(109, 75, 255, 0.12)`) and accent border on hover. Added `will-change: transform` for GPU acceleration.
- **Terminal theme (scrolling data panes):** History notes, feed, event log, and task lists now use DM Mono monospace font, dark `#0d0d0d` background, `#333` border-left entry markers, and `#7c8aff` link color. Hover shows subtle purple highlight.
- **Admin modal terminal theme:** Admin modal gets dark background (`#0a0a0a`), gradient sidebar (`#1a1a1a` → `#0a0a0a`), green accent (`#00ff88`) for active tabs, monospace font on tab buttons, and `text-shadow` glow on active state.
- **DM Mono font loaded** from Google Fonts for terminal theme.
- Cache-bust bumped to `v=1.95.3`.

## Phase 2I — Preview modal + tile revamp

- **Preview modal restructured:** Description moved to top with inline edit (pencil button → textarea with confirm/cancel). Stage split into its own section with dropdown. Tags section moved between Stage and Project Fields.
- **Project Fields in 2-column grid layout** with Supplement Request as full-width first field. Follow-up Due Date gets faint red border/background when overdue.
- **Checklist fields** (Measurement Report, Insurance Documents, Inspection Photos) rendered as 3-column field cards at bottom with green ✓ / muted ✗ icons instead of text Yes/No.
- **PA CONTRACT** rendered as check/X icon in its original field position.
- **Create modal user field overrides:** Date of Loss forced to date picker; Measurement Report, Insurance Documents, Inspection Photos, PA CONTRACT forced to checkboxes. Same overrides applied to deal-edit modal.
- **Tags:** add-tag dropdown now dark-mode styled; High Priority tag chip gets amber highlight in preview modal.
- **Create Project modal header:** fixed scrollbar overlapping header; header stays single row with `flex-shrink: 0` and `white-space: nowrap`; form body scrolls independently.
- **High Priority deal tile highlight:** fixed batch tag endpoint response parsing (`resp.tags` → `resp`); tags now loaded for all groups unconditionally so `oppHasTag` detects High Priority on every group.
- **Deal tile hover:** amber border preserved on hover via `.card--high-priority:hover` rule.
- Cache-bust bumped to `v=1.95.2`.

## Unreleased (Phase 1 follow-up)

- **Documents: sidebar folder tree.** My Documents and Company scopes show a navigable folder tree in the sidebar directly under their scope button. Chevron arrows expand/collapse subfolders. Tree repositions on scope switch. Clicking a folder navigates and highlights the active folder. Tree refreshes on create/rename/delete. New `GET /documents/folders/tree` backend endpoint returns all folders flat for tree building.
- **Documents: XLS icon fix.** Spreadsheet files now correctly show the green XLS icon instead of the blue docx icon (mime type `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` contained substring `"document"` which matched the word check first — reordered checks).
- **Documents: file list folder icons.** Folder rows in the file list now show a larger (24px) muted folder icon for better visibility against the dark theme.
- **Documents: nested folder system.** Added `document_folders` table and `folder_id` column to `project_documents`. Personal and Company scopes now support nested folders with breadcrumb navigation. Backend: `GET /documents/folders`, `POST /documents/folders`, `PATCH /documents/folders/{id}`, `DELETE /documents/folders/{id}` (recursive CTE soft-delete). Personal/Company list endpoints accept `?folder_id=` and return `{documents, folders}`. DB migration: `scripts/migrate_document_folders.py`.
- **Documents: New button.** Toolbar "New" button opens a dropdown with Word Document, Excel Spreadsheet, and New Folder options. Creating a document generates a minimal valid OOXML file via `POST /documents/create` and immediately opens it in the OnlyOffice editor. Documents and folders are created in the current folder.
- **Documents: folder context menu.** Right-clicking a folder shows Rename folder and Delete folder options (with recursive delete confirmation). Folder rows render with a folder icon and navigate into the folder on click.
- **Documents: folder breadcrumb.** Personal and Company scopes show a breadcrumb trail above the file list. Clicking a breadcrumb segment navigates to that level.
- **OnlyOffice editor: title-bar rename.** Added `permissions: { rename: true }` to editor config and an `onMetaChange` event handler in `doc-editor.html` so renaming in the OnlyOffice title bar syncs back to the CRM database via `PATCH /documents/{id}`.
- **Documents: folder_id in upload.** Personal and Company upload endpoints now accept an optional `folder_id` field in the multipart form to place uploaded files into the current folder.
- Bumped static asset cache-bust to `?v=1.88.0` in `index.html`.
- Fixed documents batch-copy endpoint (`db.insert_returning()` was called on an INSERT without `RETURNING id`, causing 500 errors when copying to My/Company docs).
- Created `public/doc-editor.html` to render the OnlyOffice editor from `/api/v2/documents/{id}/editor-config` instead of showing raw JSON. Added `docsApiUrl` to the editor-config response so the page loads the correct Document Server `api.js`.
- Replaced mail header button and email sidebar trigger icons with the mailbox SVG.
- Single-click on a document file name now opens the OnlyOffice editor (was double-click).
- Dark-mode styled the documents right-click context menu.
- Inline rename: when exactly one file is selected, a Rename button appears in the toolbar; clicking it makes the file name editable in the row with inline square-check confirm and × cancel buttons. Context-menu Rename now uses the same inline field.
- Move/Copy popup panel: selecting Move or Copy from the Actions dropdown opens a small panel with destination dropdown (Project / Personal docs / Company docs). When Project is selected, a search field finds matching deals; a square-check button confirms once a project is selected.
- Project documents are now grouped by project with discrete header dividers and file counts.
- Added document sort dropdown in the toolbar: Most recent, Oldest, Largest, Smallest, Name A–Z, Name Z–A.
- Locked opportunity preview modal height so switching to the Documents tab no longer shrinks the window.
- Right-edge trigger stack anchored at `bottom: 67%` with `column-reverse` so the Bookmarks trigger stays fixed at the top 1/3 and minimized Search/Email/Documents triggers stack upward above it.
- Added `searchMinimized` flag so the search sidebar trigger restores correctly when the bookmarks sidebar closes.
- Bumped static asset cache-bust to `?v=1.87.10` in `index.html`.
- Fixed editor-config URL confusion: `docsApiUrl` now uses `DOCS_PUBLIC_URL`; file `downloadUrl` and `callbackUrl` use `CRM_PUBLIC_URL`.
- Added JWT token authorization to `/api/v2/documents/{id}` download so the OnlyOffice Document Server can fetch files without a session cookie.
- Added `CRM_PUBLIC_URL` env variable and updated `.env` / `config.example.env`.
- Started the local `onlyoffice-docserver` container and configured `.env` to use `https://192.168.1.68:9443` for the document API and `http://192.168.1.68:8766` for CRM callbacks/downloads.
- Fixed OnlyOffice editor JWT signing: token now signs the config directly instead of wrapping it in `{"payload": config}`, resolving "The document security token is not correctly formed."
- Added PDF support in editor config (`documentType: "pdf"`, `mode: "view"`).
- Documents toolbar: Upload, Sort, and Rename buttons are now icon-only. Sort uses a custom dropdown menu with a checkmark on the active option.
- Fixed Move/Copy popup closing immediately due to a document click listener; added a just-opened guard.
- Documents modal sidebar is now collapsible via a toggle button (same behavior as the Mail modal sidebar) and auto-collapses on narrow mobile screens.
- Improved mobile modal layout so content no longer gets cut off on very narrow screens.
- **Documents: file-type icons.** Replaced emoji-based file icons with Tabler SVG file-type icons (docx, xls, pdf, image, text, generic) using muted per-type colors (blue for Word, green for Excel, red for PDF, purple for images, slate for text). Stroke width 2 for legibility at 16px. Toolbar "New" button switched to tabler-square-rounded-plus icon.
- **Documents: move-to-personal/company.** "Move to Project…" dropdown renamed to "Move to…" with Personal/Company scope options. PATCH endpoint now accepts `folder_id`, `scope`, `title`, and `opportunity_id`. Batch move supports personal and company destinations.
- **Documents: drag-and-drop.** File rows are draggable; folder rows accept drops to move files into folders (My Documents / Company scope only). Drag-over visual feedback with dashed blue border.
- **Documents: auto-refresh after save-as.** Editor sets `localStorage.crm-docs-refresh` flag on save-as; CRM checks on `window.focus` and reloads list.
- **Documents: OnlyOffice callback fix.** Callback handler now handles status codes 1, 2, 4 (not just 2/6). Added `_download_from_docserver()` helper with JWT auth + SSL bypass for self-signed cert.
- **Documents: inline file rows.** Single-line file rows with size and date separated by `·` divider. Divider lines between rows.
- **Documents: projects view-only.** Upload & New Folder buttons hidden in Projects scope. Move panel supports personal/company/project destinations.
- **Documents: breadcrumb in empty folders.** Breadcrumb navigation renders even when current folder is empty.
- **Sidebar toggle arrows fixed.** Collapsed → `▶`, expanded → `◀` (was reversed).
- Bumped static asset cache-bust to `?v=1.87.11` in `index.html`.

- Refactored header chrome into a single `.hero-header-actions` flex container (admin gear, server-health icon, Sign-out) with transparent hover-only styling (no borders). Eliminates absolute-position overlap and gives the same style on desktop and mobile. Mobile wraps gracefully with compact sizing.
- Reordered `.hero-actions`: Quick Note + New Opportunity buttons now sit left of the global search field; Search modal button sits right of the field.
- Refactored right-edge sidebar triggers into a single `.right-sidebar-triggers` flex column with `column-reverse`. Bookmarks trigger stays fixed at the bottom; minimized Search, Email, and Documents triggers stack upward above it and hide with `display: none` so the stack collapses cleanly when empty. All triggers share the same 2.6rem width and dark bordered styling.
- Added dark bordered square `.modal-header-control` buttons for minimize/close in Email, Documents, and Search modal headers. Buttons sit in a single horizontal row with consistent sizing.
- Fixed admin Infrastructure restart controls: "Restart server process" now spawns a replacement Python process via `subprocess.Popen(start_new_session=True)` before exiting, so the server actually comes back up. "Restart Docker container" now uses the configured `CONTAINER_NAME` (`sietch-crm` via `docker-compose.yml` environment) instead of `socket.gethostname()` (the container id), which was causing Docker 404 / "Nothing matches the given URI".
- Fixed documents upload/search failures caused by missing `company_scope` and `notes` columns in the live DB. Ran `scripts/migrate_documents_scope.py` to add both columns (plus the company-scope index). Removed remaining `notes` column references from active documents queries so the feature works regardless of migration state.
- Bumped static asset cache-bust to `?v=1.87.9` in `index.html`.

- Phase 2I (Preview modal + tile revamp): Preview modal restructured — Description moved to top; "Deal Fields" renamed to "Project Fields" with standard + user fields merged; Stage rendered as dropdown (change stage directly from preview); "Expected Close" renamed to "Follow-up Due Date"; Tags section with inline add/remove (× per tag chip + select dropdown for available tags); Checklist section with specialty checkboxes in 3-column grid. Kanban deal tiles now show created date ("Created: Jul 15") next to due date; native tooltip on card hover shows title — contact — value — due date summary.
- Fixed `bindEventLogModal` crash (undefined `modal` variable) that prevented init() from completing — header buttons (new deal, quick note, documents, etc.) now attach properly.
- Fixed opportunity preview Documents tab crash (`Cannot read properties of undefined (reading 'length')`) caused by a destructuring mismatch (`docs` vs `documents` in `previewDocsState`).
- Removed the per-project project list from the Documents modal sidebar: project documents now appear only in the Documents search result list (grouped by project). The Projects scope remains; it displays the grouped search results instead of a sidebar project picker.
- Updated client-side unreachable-server error text from "Failed to reach CRM" to "Failed to reach server" to match the v3 standalone PostgreSQL architecture.
- Mobile header layout fixed: centered hero logo moved to top-left at a smaller size so it no longer overlaps the title; admin-console (settings) gear and Sign-out button reordered to avoid overlap; hero inner padding increased to clear the top row.
- **Server health indicator:** Small tabler-server icon in the header (next to settings gear) that appears amber when the backend is unreachable and disappears when healthy. 60s health poller skips when tab is hidden (no throttle false positives). `api()` function hooks track failures and successes. No banner, no toast — just the icon.
- **Admin Infrastructure tab:** New tab in admin console showing server status (DB reachability), Docker container health (status, restart count), infrastructure event log (rolling 200 events), and restart controls (restart server process or restart Docker container, both admin-only with confirm dialog).
- **Documents toolbar dropdown:** Delete / Move to Project / Copy to… consolidated into a single "Actions" `<select>` dropdown (reduces toolbar clutter).
- **Docker healthcheck + restart limits:** `docker-compose.yml` now has a healthcheck (`/api/health` every 30s) and `restart: on-failure:5` (max 5 automatic restarts, then stop — admin can see and manually restart from Infrastructure tab).
- **Infrastructure ring buffer:** In-memory rolling 200-event deque in `server.py` tracks errors, restarts, and server events. Logged to on DB connection failure, Document Server unreachable, and restart requests.
- **PATCH method support:** Added `do_PATCH` handler to server — `PATCH /api/v2/projects/{id}` now works for stage updates from the preview modal. Also supports document rename/move via PATCH.
- **Minimize-to-sidebar:** Email, Documents, and Search modals now have a minimize button (universal `—` icon in header). Minimized modals show as icon-only triggers on the right edge of the viewport. Email saves scroll position + selected email; Documents saves scope + search query; Search saves preview tabs (existing behavior). All coordinate with bookmark sidebar (hide when sidebar opens, show when closes).
- **Tabler icons webfont:** Added `@tabler/icons-webfont` CSS for sidebar trigger icons.
- **Search modal header:** Added header row with title and minimize button. Fixed tiny vertical scrollbar in tab bar (`overflow: hidden` instead of `overflow-x: hidden`).
- Phase 2A continuation: search popup modal now opens on Projects tab by default. Projects tab shows search input + stage/owner filters + batch ops (add/remove tag, set stage, export). Tags tab (separate tab) shows tag selector + Search by Tag button to find deals by tag. Tab switching auto-loads data. Error display scoped to active tab's error div. Tag selector deduplicated (removed duplicate from projects tab). Enter key supported on tag select. Preview tabs fall back to Projects on close.
- Fix add tile modal close button and backdrop: added onclick fallbacks directly in HTML elements (the JS event listener was not firing despite correct code — root cause unclear; fallback approach guarantees the buttons work regardless of JS binding timing).
- Added Phase 2H (Documents modal) to sietch-crm-plan.md: shared folder file manager modal with list/upload/delete/rename/copy/move/open-in-editor for Document Server files. Header button with files icon. Refresh dashboard button and Nuke Cache button in group tiles confirmed still useful and kept.
- Phase 2H frontend (part 1): documents modal JS + HTML + CSS. JS: docsState, bindDocumentsModal/openDocumentsModal/closeDocumentsModal, loadDocumentsList/renderDocumentsList (list view with checkboxes, icons, project label, size, date), loadDocsProjects (sidebar project list), uploadDocsToCurrentScope (file picker + drag-drop), batch delete/move/copy, toggleDocSelect/updateDocsToolbar, openDocEditor (double-click), downloadDoc. HTML: #documents-btn header button, #documents-modal shell with sidebar (Projects/My Docs/Company tabs + project sub-list) + toolbar + list area + drop overlay. CSS: all modal styles.
- Phase 2H frontend (part 2): right-click context menu on documents modal rows (open in editor, download, rename, move, copy, delete). Context menu positioned within modal bounds, closes on Escape or outside click.
- Phase 2H: overhauled Documents tab in project preview modal — now shows file icons, size, date, checkboxes for selection, select-all, upload button (with file picker + progress), and batch delete. Replaces simple link list.
- Fixed documents button icon (now uses Tabler files icon). Removed refresh button from header (duplicates browser refresh). Fixed test-server.py with all needed v2 API stubs (/api/v2/me, /api/check-admin, /api/v2/stages, /api/v2/tags, /api/v2/users, /api/v2/contacts, /api/v2/custom-fields, /api/v2/tasks, /api/branding, /api/v2/auth/login, PATCH handler).

- Header buttons (mail-inbox, add-tile, bookmarks trigger) + sign-out now reliably work (previously did nothing or no UI switch). Attached all critical listeners EARLY in init() immediately after guarded login form (using dataset guards to avoid dups) + made showLogin/showApp force display+visibility. Late binds no longer duplicate header attaches.
- Sign out: reliably switches the UI to the login screen immediately (no static dashboard until manual refresh). Wrapped logout fetch in try/catch (always proceed to showLogin); added explicit style.display toggles in showApp/showLogin for robustness.
- Phase 2C admin: contacts tab enhanced with live search + basic add contact form; stages tab implemented with search + add form (POST to /api/v2/stages; refreshes global stages; mirrors contacts UX).
- Phase 2A complete: search popup now has stage + owner filters, select-all checkboxes, batch actions (add tag, remove tag, set stage, export selected), and row-click to preview. Existing rich results + tabbed previews + CSV export retained.
 - Phase 2C: admin tabs filled — Custom Fields (read-only definitions), Tags (list + add form). Existing Users, Stages, Contacts, Event Log, Bot, Branding, Sync retained. New tabs keep icons next to titles per non-neg. Projects managed via search modal (filters + batch ops).
- Phase 2 audit complete (all sub-phases reviewed via code + plans): 2A/2B/2C/2D present; 2E/2F/2G UI not started (backend for photos ready). Updated sietch-crm-plan.md table.
- Plan: added research item to OnlyOffice CRM import phase in sietch-crm-plan.md: continue using export script but capture/store each deal's unique OO numeric id (Deals.aspx?id=NNN) for reliable later sync matching by external id (instead of title); also capture per-deal tags + custom/user fields (currently missing from bulk export).
- Login: removed "Old CRM link" and giant ship watermark (background image + all related CSS/JS application on login screen).
- Admin branding: Primary color input now always shows the *current* color (fetched from /api/branding or falls back to computed --accent / CSS default; never hardcodes blue). Branding form auto-populates when switching to the Branding tab inside admin console. Live color preview bound for the admin tab.
- Admin console: vertical left sidebar tabs (full border highlight when active), tab content displayed directly to the right in side-by-side layout. Large fixed 980px modal window (no dynamic resize on tab switch; mobile uses 95vw fill). Tabs styled as left-aligned buttons. Shortened names (Import Sync etc). Login logo 96px. Gear button overlap fixed. Git+docs.
- Login footer (login page bottom): removed text, just larger sietch-logo-2-nobg1.png (includes name), no button/hover highlight (pointer-events:none + opacity override). Dashboard footer unchanged.
- Hero/logo default: sietch-logo-2-nobg2.png (pure logo); ship hero kept as customizable branding.
- Header: "Sietch CRM" + version number now use normal sentence spacing (nested in one span, reduced internal margin); no logo added to it.
- Admin console button: absolute positioned right in header (left of "Sign out"); removed ineffective margin-left:auto.
- Primary for functional testing: real `server.py` (./start.sh); `test-server.py` only for deliberate chaos/mutation queue experiments.
- Fixed server won't start: .env with unquoted "SMTP_FROM_NAME=Sietch CRM" (space) caused "CRM: command not found" during `source .env` in start.sh. Quoted value in .env + config.example.env. Made start.sh env loading robust (strips+re-quotes values safely).
- Updated AGENTS.md (last session summary) + CHANGELOG.

- Non-negotiables added to plan/AGENTS: preserve data/layout as if using OO API (note types, history with embedded emails, contacts, etc.); refer to main/prod for tiles/preview; implement contacts support; all OO API funcs replaced; focus functional first (sync later); git+docs after changes.
- Admin console: button moved to right of header (left of sign out); no separate branding/bot/event-log buttons; tabs keep current order with exact same SVGs next to titles for Branding, Bot Customers, Event Log. Added Contacts tab stub.
- Header: static "Sietch CRM" to left of version number (under header, static not changeable).
- Logos: footer, favicon, "Sietch CRM" name static/hardcoded (using new assets); ship watermarks remain customizable via branding.
- Flashing on deal tiles: restored original production hover (simple border light-up on mouseover, no re-render); disabled card observer/enrich replaceWith that caused flash on visible/hover (compared to main branch code).
- Branding form moved into admin tab (separate modal removed to consolidate).
- Header static "Sietch CRM" text added left of version.
- Started consolidating branding/bot/logs into admin tabs (stubs; full move next).
- Updated docs with non-negotiables per user direction.

- OnlyOffice->Sietch data import completed and verified (1191 opps, 38898 history events, 16 contacts, 11 users) into sietch_crm; bot login + filtered API queries work.
- Added legacy query param support (filterValue, stageType, opportunityStagesid, contactid) in server.py:_handle_projects_list so frontend group kanbans and search continue to filter correctly after import.
- Skipped dashboard notes/profiles import (start fresh per-user); direct venv run with DB override for testing.
- Quick logo update: dashboard now uses new sietch-logo-2-nobg2.png (pure logo) for header/branding defaults and sietch-logo-2-nobg1.png (logo+name) for footers (where logo was beside "Sietch CRM" text). Updated references in code, HTML, init.sql, README, defaults. (Issues: none major; kept ship logos for watermark/header hero.)
- Phase 2C progress: /api/v2/me now returns camelCase consistent with login; Admin Console Overview shows live current user; Users tab now renders live list of users (read-only, with [admin] badges).
- Fixed kanban display: projects now appear in correct stage columns. `stageId` and `stageType` are now read from top-level fields on opportunity objects (server returns them at root; frontend was looking inside `opp.stage`).
- Fixed `localeCompare` errors: all string comparisons now coerce to `String(...)` defensively.
- Card interactions: removed the per-card "Preview" button. Clicking the project title now opens the preview modal (edit button remains).
- Fixed branding save: `POST /api/branding` moved from the GET handler to the POST/PUT handler (`_handle_api_post_put`).
- Team tile / presence: server already filters inactive users; added `isActive` to `portalUsers` mapping and defensive active-user filter in the presence modal roster.
- Added `db.query_one()` helper and module-level logger for consistent error reporting.
- Fixed remaining `a[1].localeCompare` crashes in user select populates (create/edit/notify/task filters) by ensuring String coercion; server create now accepts stageType. This unblocks creating/editing deals in Sietch standalone.
- Started Phase 2C: added Admin Console button (auto-visible to isAdmin users like bot/kenc via session, no extra login) + tabbed modal stub (Overview + OnlyOffice Sync read-only enrich section + placeholders).
- Per user direction: keep imported data; read-only enrich/sync later via admin (title matching concern noted; will use external_id + richer match or fresh-to-empty if needed). Focus moved to making Sietch functional + next phases per plan (deprioritized further import diagnosis).

## [3.0.0] — 2026-07-18

### Major Changes

- **PostgreSQL migration.** Replaced OnlyOffice CRM dependency with self-contained PostgreSQL database. All data now lives in local database — no external CRM server required.
- **New authentication system.** PBKDF2 password hashing, session cookies, password reset via SMTP. No more OnlyOffice Portal token dependency.
- **New v2 REST API.** Complete rewrite of backend endpoints:
  - `/api/v2/projects` — project CRUD with filtering
  - `/api/v2/tasks` — task management
  - `/api/v2/contacts` — contact directory
  - `/api/v2/users` — user management
  - `/api/v2/documents` — document storage
  - `/api/v2/history` — event history with threaded replies
  - `/api/v2/notifications` — user notifications
- **OnlyOffice Document Server integration.** Documents now open in self-hosted Document Server for viewing and collaborative editing. No dependency on old OnlyOffice Portal for document access.
- **Migration script.** One-time migration from OnlyOffice CRM to PostgreSQL. Preserves all projects, contacts, history events, tasks, and document files.
- **Dead code removal.** Removed mutation queue system, crash banner, mail system, and OnlyOffice Portal URL handling from frontend (~500 lines removed).

### Infrastructure

- PostgreSQL 16 database (Alpine container)
- OnlyOffice Document Server (for document editing)
- Redis (session management for Document Server)
- Docker Compose stack with internal networking

### Breaking Changes

- API paths changed from `/api/2.0/crm/*` to `/api/v2/*`
- Authentication now uses session cookies instead of OnlyOffice Portal tokens
- Document links now point to self-hosted Document Server

### Files Changed

- `init.sql` — Full PostgreSQL schema (34 tables)
- `db.py` — PostgreSQL connection layer
- `auth.py` — Authentication and session management
- `smtp_client.py` — SMTP relay for password resets
- `migrate_from_onlyoffice.py` — Migration script
- `server.py` — Complete rewrite (direct DB queries, v2 API)
- `docker-compose.yml` — Added PostgreSQL, Document Server, Redis
- `Dockerfile` — Added psycopg2-binary, Pillow, ExifRead
- `public/app.js` — API path swaps, dead code removal
- `config.example.env` — Added DB, SMTP, Document Server variables

## [2.2.3] — 2026-07-15

### New features

- **Search popup minimize + right-side Search trigger.** The search modal now has a minimize button that parks all open preview tabs. A vertical "Search" tab appears on the right edge above the bookmarks trigger; clicking it (or the header search button) restores the parked tabs and lands on the Search tab. Parked tabs persist in the user profile (server + localStorage fallback). Hidden when the bookmark sidebar is open.
- **Bookmark sidebar dividers.** Add draggable horizontal dividers in the bookmark tab bar to visually organize saved bookmarks into groups. New divider button in the sidebar header uses the Tabler `divide` icon. Dividers can be dragged between bookmark tiles and removed via hover ×. Stored in the user profile alongside bookmarks.

### Improvements

- **Search popup preview tab limit raised from 5 to 10.**
- **Search results export button is now an icon-only CSV button.** Replaced the "📋 Export CSV" text with the Tabler `file-type-csv` icon.
- **Login screen refreshed.** Title changed to "Vanguard Adjusting Dashboard" and centered. Portal URL field hidden from view. Added discrete "Old CRM link" footer pointing to `office.publicadjustermidwest.com`.

### Bug fixes

- **Login now works when portal URL is only in the request body.** `_handle_login` previously checked `ONLYOFFICE_PORTAL_URL` env / `X-OnlyOffice-Portal` header before reading `portalUrl` from the login form body, causing local logins to fail when the env var was not set. It now reads the body first and uses `portalUrl` as the primary source.
- **Delete note confirm dialog now appears above preview modals.** `#confirm-modal` z-index raised to 2100 so it stacks above the opportunity preview modal (z-index 2000).
- **Note editor paste: stripped formatting and preserved line breaks.** Added paste event handler on `.note-editor` elements that strips all styling (background-color, fonts, classes) from pasted HTML and converts plain text newlines to `<br>` tags. Prevents pre-highlighted text from appearing in notes and ensures line breaks transfer correctly.

### Files changed
- `public/index.html`, `public/app.js`, `public/styles.css`, `server.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [2.2.2] — 2026-07-13

### Bug fixes

- **Login screen: removed duplicate "Logging in…" text.** The `<p id="login-loading">` element duplicated the button's loading state. Removed the element; button text change is sufficient.
- **Login screen: updated portal URL to publicadjustermidwest.com.** Subtitle and input field now reference `office.publicadjustermidwest.com` instead of the old `vanguardadj.com` domain.
- **Email history links in preview modals now load correctly.** Fixed regex at `server.py` that matched `/api/mail/message/{id}` but not `/api/proxy/api/mail/message/{id}` (the actual path from the client). Also reverted `_handle_mail_message_get` to use the user's session token for `filehandler.ashx` — linked emails are accessible to all deal users, no bot token needed.
- **Regex crash in `historyContentIsMailPlaceholder` fixed.** Replaced regex-based escaping with simple string methods (`startsWith`, `endsWith`, `includes`) to avoid regex compilation entirely. The previous regex character class `[.*+?^${}()|[\]\\]` still failed on subjects containing `(` like `(866) 787-8676`.
- **Starting RCV user field character limit doubled.** `parseCustomFieldTextMaxLength` now returns `size * 2` from the CRM field definition mask, applying to both create and edit deal modals.
- **Bot linking: contacts can now have up to 10 linked chats.** Previously, linking a new chat to a contact replaced the existing link. Now each contact can have up to 10 simultaneous bot links. `verify_code()` and `add_mapping()` enforce the limit; `set_verify_chat_id()` finds the correct pending mapping (chatId=0).
- **Delete note confirm dialog now appears above preview modals.** `#confirm-modal` z-index raised to 2100 so it stacks above the opportunity preview modal (z-index 2000).
- **Note editor paste: stripped formatting and preserved line breaks.** Added paste event handler on `.note-editor` elements that strips all styling (background-color, fonts, classes) from pasted HTML and converts plain text newlines to `<br>` tags. Prevents pre-highlighted text from appearing in notes and ensures line breaks transfer correctly.

### Improvements

- **File attachment links in email history notes open in Documents module.** `renderHistoryAttachmentsAside` now links to `DocEditor.aspx?fileid=` (same as the Documents tab) instead of `filehandler.ashx?action=download`.
- **Customer Bot modal: two-column desktop layout.** Modal widens to 960px on desktop (≥900px). Left column: Invite Customer + Existing Mappings. Right column: Broadcast Message + Activity Log. Single column on mobile.
- **Customer Bot activity log: expandable per-user endpoint details.** Each user row has a toggle button (▶/▼) that expands to show endpoint breakdown. Log also shows First request, Last request, and Active days.
- **Customer Bot: deal fields in Telegram deal detail.** Customer-linked users now see Address, Customer Phone, Insurance Carrier, Claim #, and Stage below the deal title (one per row, empty values skipped). Employee view unchanged (history notes with existing buttons).
- **Customer Bot: Project Info button for employees.** Employee deal detail now has three buttons: 📋 Project Info | 📝 Add Note | 🔍 New Search. Project Info shows all deal fields (Stage, Amount, Tags, Description, Contact, Responsible, Created, Due Date, Bid Type, Private) and all user fields inline — no section header. `_extract_bot_user_fields` now returns ALL custom fields, not just the 5 hardcoded ones. `_handle_bot_deals` now passes additional CRM standard fields (description, contact, responsible, created, dueDate, bidType, isPrivate) in the deal entry.

### Files changed
- `public/index.html`, `public/app.js`, `public/styles.css`, `server.py`, `telegram_bot.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [2.2.1] — 2026-07-08

### Bug fixes

- **Server-side tag cache now properly invalidated on per-opp tag mutations.** The cache invalidation condition in `_handle_api_post_put` checked `"/crm/opportunity/tag" in p`, which matches GET paths like `/opportunity/tag/123` but **not** mutation paths like `/opportunity/123/tag`. Added `re.search(r"/crm/opportunity/\d+/tag", p)` so POST/DELETE tag operations correctly invalidate the 10-minute proxy cache. Previously, after adding/removing tags, the preview modal and tile enrichment could serve stale cached tag data for up to 10 minutes.

- **Client-side tag cache bypass on edit and nuke refresh.** Three fixes ensure fresh tags are always fetched after mutations:
  - `enrichOpportunitiesTags` now respects `force=true`: skips the client-side tag cache and always fetches from the server batch endpoint.
  - `submitDealEditForm` and `submitQuickNoteForm` pass `force=true` to `enrichOpportunitiesTags` after saving tag changes.
  - Individual tag fallback requests (when batch endpoint fails) now send `X-Force-Refresh: 1` header when `force=true`, bypassing the server proxy cache.
  - `refreshGroup` clears the entire tag cache on manual Nuke Cache (`_manualGroupTileRefresh`), so all opportunities in the group get fresh tags.

### Improvements

- **Nuke Cache icon and label.** Reverted to original Tabler radioactive (trefoil) icon. Tooltip/aria-label updated to `"Nuke Cache (refresh tile)"`.

### Files changed
- `public/app.js`, `server.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [2.2.0] — 2026-07-07

### Bug fixes

- **`/tag` now correctly lists and filters by tag.** Rewrote `_handle_bot_tags` to extract tags from open deals (the CRM-wide tag definitions endpoint doesn't exist). Fixed tag filter in `_handle_bot_deals`: the per-opportunity tag endpoint returns plain strings, not dicts — now handles both formats. Tag selection now correctly finds matching deals.
- **Usage Log now loads.** Fixed routing bug: `GET /api/bot/usage` was defined in `_handle_api_post_put` (unreachable for GET requests). Moved to `_handle_api_get` at the correct routing position.

### Improvements

- **Broadcast message format.** Server-side now prepends `<b>-System Message-</b>\n\n` and appends `\n\n<i>Please do not reply to this message.</i>` to all broadcast messages.
- **HTML sanitization for broadcast.** Added `_TelegramHTMLSanitizer` (same logic as `telegram_bot.py`) to strip non-Telegram-safe HTML tags from broadcast input before sending. Only `<b>`, `<strong>`, `<i>`, `<em>`, `<u>`, `<ins>`, `<s>`, `<strike>`, `<del>`, `<a href="...">`, `<code>`, `<pre>` are allowed.
- **Bold / Italics toolbar in broadcast UI.** Replaced raw HTML hint text with a toolbar of B / I buttons that wrap selected text or insert empty tag pairs at cursor position.

### Files changed
- `server.py`, `public/app.js`, `public/index.html`, `public/styles.css`, `VERSION`, `README.md`, `CHANGELOG.md`, `AGENTS.md`

## [2.1.0] — 2026-07-07

### Telegram bot — inline keyboards, employee note creation, role-aware onboarding

- **Inline keyboard navigation.** Project search results now render as tappable buttons instead of numbered lists (old number-reply fallback preserved). Deal detail screen has "New Search" and "Add Note" (employee) buttons.
- **Employee note creation.** From any deal detail, employee can tap "Add Note" → type text → choose curated category from inline keyboard (Quick Context, Note, Text, Call, Email, Customer Update). Note is posted via dashboard proxy (`POST /api/bot/note`). Category picker uses regex matching with priority ordering; no "Default" button.
- **Role-aware `/start`.** Linked employees see commands + welcome; linked customers see search prompt; unlinked users see invite code prompt.
- **Role-aware `/help`.** Three variants: customer (basic search), employee (commands + notes), unlinked (invite code instructions).
- **`/projects` command (employee only).** Lists all open projects with inline keyboard.
- **`act:note:` uses `reply_text`.** "Add Note" prompt sends a new reply message instead of editing the deal detail, keeping the deal info visible for reference.
- **Fixed `allowed_updates` bug.** Used plural `"messages"` (silently ignored by Telegram — adding any valid type activated the filter and dropped messages). Fixed to `Update.MESSAGE` / `Update.CALLBACK_QUERY` constants.

### Files changed
- `telegram_bot.py`, `server.py`, `AGENTS.md`, `CHANGELOG.md`, `VERSION`

## [2.0.10] — 2026-07-06

### Hotfix — Group tile "Nuke Cache" (universal)

- Per-group tile refresh button ("Refresh deals") is now **universal cache-buster** for all users/browsers. Clicking it forces a live CRM `/filter` call:
  - Browser: `fetch(..., { cache: "reload" })`
  - Dashboard proxy: skips `_proxy_cache` lookup + store via `X-Force-Refresh` header
  - Response includes `Cache-Control: no-cache, no-store, must-revalidate` + `X-Proxy-Cache: BYPASS-FORCE`
- Button label changed to **"Nuke Cache"** (title/aria-label too). Icon is muted Tabler radioactive (trefoil) symbol. Only the group tile refresh button is affected (no impact on header refresh, feed/tasks/calendar tiles, post-edit refreshes, search, etc.).
- Customer Update history notes now use Tabler `eye-check` icon (instead of speech bubble).
- README.md: permanent toaster header added at the very top: "THIS APP WAS CREATED BY A TOASTER. LOOK ON IT YE MIGHTY AND DESPAIR".
- Version: 2.0.10

### Files changed
- `public/app.js`, `server.py`, `VERSION`, `CHANGELOG.md`, `README.md`

## [2.0.9] — 2026-07-05

### Fixed
- **Backdate controls in deal-edit and quick-note.** Plain native `<input type="date" id="*-note-created" class="note-backdate-input">` placed directly after the note editor (no wrapper, icon, or label). Width tuned (max 138px) so full mm/dd/yyyy is visible. Submission already passed `created` to history events.
- **Quick Context items.** Tabler thumbtack (pin) icon + faint blue background (`rgba(37,99,235,0.08)`) matching High Priority tag style.

### Files changed
- `public/index.html`, `public/app.js`, `public/styles.css`, `CHANGELOG.md`, `VERSION`, `AGENTS.md`, `docs/RELEASE_v2.0.9.md`

### Removed / Scrapped
- **WebKit / Apple-friendly cache-busting attempt (July 4).** Full attempt to add `detectAppleWebKit()`, `state.isAppleWebKit`, `bustCache()` helper, and guarded calls on short-TTL paths (filter/history/tag/customfield) plus premature `Cache-Control`/`Pragma`/`Vary`/`Expires` headers before `send_response` in `server.py:_handle_api_get` on cache miss. This produced `net::ERR_INVALID_HTTP_RESPONSE` (400) on direct http LAN testing (127.0.0.1 + local IPs) for history/filter/batch-tags/individual tag calls after login succeeded. User reported multiple times that "local dev used to work great until the edits"; initial diagnosis wrongly focused on auth/reachability. Entire feature scrapped; all `detectAppleWebKit`/`isAppleWebKit`/`bustCache`/`isDynamicShortTtl` + header-before-response blocks removed. Pre-existing server TTL cache (30s filter, 600s tags/stages), `_proxy_cache` invalidations, `newId()` UUID fallbacks, 502/5xx resilience, friendly tile retry links, LAN IP printing, batch-opportunity-tags, and all prior proxy/auth/upload logic preserved. Documented as expensive disaster and waste of tokens with ignored user feedback.

## [2.0.8] — 2026-07-03

### Infrastructure / Upload fix (post-sherwood-toolbox)
- **Host nginx is now the permanent front-end for `dashboard.publicadjustermidwest.com`.** After the sherwood-toolbox deployment, public traffic no longer goes through the Docker `estimate-nginx` container. The active config is `/etc/nginx/sites-enabled/dashboard.publicadjustermidwest.com`.
- **Restored PDF/image attachments in edit/quick-note/side modals.** Added the three required directives to the host site file (matching sherwood-toolbox convention on the host):
  - `client_max_body_size 100m;`
  - `proxy_request_buffering off;`
  - `proxy_read_timeout 120s;`
- These live inside the https server block before `location /`. Without them, `UploadProgress.ashx` multipart uploads failed with 413 "client intended to send too large body".
- All documentation, deploy scripts, and references updated to reflect the new permanent architecture (host nginx directly to `127.0.0.1:8765`). Old `estimate-nginx`/`/opt/estimate-enhancer/nginx.conf` paths are now explicitly marked historical for the dashboard domain.
- `docker-compose.yml` network declaration left unchanged (harmless, keeps future options open).
- Backups of the host site file, full `nginx -T`, container state, certs, and user data volume were taken before the edit.

### Files changed
- Host nginx site file (server), `docs/DASHBOARD_INFRASTRUCTURE.md`, `docs/PRODUCTION_SERVER_NOTES.txt`, `docs/UPDATE_AND_DEPLOY.txt`, `docs/DEPLOY_v1.1_VERIFY_STEPS.md`, `AGENTS.md`, `docs/MIGRATE_DOMAINS.md`, `docs/CUTOVER_RUNBOOK.md`, `romanian_roadtrip.md`, `FUTURE_FEATURES.md`, `CHANGELOG.md`, deploy/ historical script headers (`nginx-dashboard.conf`, `nginx-dashboard-estimate-nginx.snippet`, `dashboard-add-new-domain.sh`, `dashboard-add-new-domain-v2.py`).

## [2.0.7] — 2026-06-30

### Fixed
- **Preview modal: pasted email content now renders for Email-category notes.** Email-category history events were always collapsed to the generic "An email has been received." summary, even when a user pasted real content into the note. The summary is now shown only for genuine linked emails (messageId present / mail payload) or the CRM placeholder text; otherwise the actual content renders normally.
- **Presence/Team tile: no more UUID display names.** When a CRM user record lacks `displayName` and `userName`, the server now falls back through `email`, `firstName`+`lastName` before using the raw user id. The Team tile also uses the same robust label helper as the modal roster.

### Files changed
- `public/app.js`, `server.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `docs/RELEASE_v2.0.7.md`

## [2.0.6] — 2026-06-29

### Fixed
- **Preview modal: highlighted `<b>` text now readable.** The CRM note editor uses `<b style="background-color: rgb(200, 230, 201);">` for highlighted text. The CSS override now targets `b`/`span`/`mark` elements with `background-color` or `background` inline styles across all preview history body variants (main preview, bookmark preview, search popup), using a darker `rgba(34, 197, 94, 0.65)` background and black text so highlighted content is legible on the dark modal background.
- **Customer Bot modal: deleting one mapping no longer removes multiple mappings.** Delete buttons now use the mapping's unique `chatId`, and the server removes by `chatId` via `remove_mapping_by_chat()`. This fixes the bug where multiple employee mappings (or mappings sharing a `contactId`) were all deleted when removing one.
- **Preview modal: delete button restored for Customer Update and Text/SMS events.** The × button now appears for history categories matching `customer update`, `text message`, `text`, and `sms` (in addition to existing note/comment/activity/meeting/call/task categories). The underlying `DELETE /api/2.0/crm/history/{id}` endpoint already supports these events.
- **Customer Bot: customer mode latest update no longer shows author.** The "Latest customer update" header in non-employee mode now omits the employee name, matching the decision to hide authors from Customer Update events.

### Files changed
- `public/styles.css`, `public/app.js`, `server.py`, `telegram_bot.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `docs/RELEASE_v2.0.6.md`

## [2.0.5] — 2026-06-29

### Added
- **Customer Bot (employee mode): show event author.** Non-mail history events now display the creator's display name next to the date, e.g. `[Note] — Jun 28, 2026 (Ken Chapman)`. Author is intentionally hidden for `Customer Update` events so customers never see employee names.
- **Customer Bot (employee mode): full mail body fetch.** The dashboard proxy fetches the complete email body from the CRM `filehandler.ashx?action=mailmessage&message_id={id}` endpoint and injects it into employee history events, replacing the truncated CRM history content.

### Changed
- **Customer Bot (employee mode): mail body cap and truncation marker.** Long email bodies are capped at 1200 characters and end with `[truncated]` so users know the full message continues in CRM.
- **Customer Bot: distinct footer.** The closing prompt now has a separator line and is italicized so it doesn't blend into email/note content.
- **Customer Bot: customer update author hidden.** `Customer Update` events no longer show the author name.

### Fixed
- **Customer Bot (employee mode): HTML parse errors from email addresses.** Forward/reply attribution lines like `Grasshopper <notifications@grasshopper.com>` are now HTML-escaped so Telegram doesn't treat the email address as an unsupported tag.
- **Customer Bot: plain-text fallback stripped tags.** If Telegram rejects the HTML message, the fallback now strips HTML tags instead of sending literal `<b>`/`<i>` text to the user.
- **Customer Bot: HTML-safe message truncation.** When a deal detail exceeds Telegram's length limit, truncation now closes open HTML tags and removes partial tags so the message stays valid HTML.
- **Customer Bot (employee mode): tight email formatting.** Raw CRM email HTML is converted to plain text with normalized whitespace so forwarded emails no longer have leading spaces or words broken across lines.
- **Customer Bot (employee mode): forwarded-header parsing.** The bot correctly extracts `From`/`Date` from forwarded email headers and no longer swallows the first paragraph of the body.

### Files changed
- `server.py`, `telegram_bot.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `ISSUES.md`, `docs/RELEASE_v2.0.5.md`

## [2.0.4] — 2026-06-28

### Added
- **Customer Bot: employee access mode.** Admins can now generate invite codes for employees by checking "Employee access (view all deals)" in the Customer Bot modal. Employees see all open deals across the CRM (no contact filter) and up to 5 recent events per deal, each labeled with its history category.
- **Customer Bot: employee mapping support in backend.** `crm_bot_store.py` persists an `employee` flag on codes and mappings. `server.py` `/api/bot/deals` accepts `employee=true` and returns an `events` array with `categoryName`. Invite code generation, cancellation, unlinking, and nickname changes all support employee mappings.
- **Customer Bot: employee rendering in Telegram bot.** `telegram_bot.py` passes the employee flag through search/selection flows and renders deal detail with `[Category]` labels for employees.
- **Customer Bot: employee UI affordances.** Modal title switches to "Invite Employee", contact picker is hidden, and existing mappings show an "Employee" badge.

### Fixed
- **Customer Bot: number replies no longer re-fetch deals.** The bot now caches the last search results per chat (`_last_deals`) and interprets "1", "2", ... replies from the cache instead of calling the CRM API again.
- **Customer Bot: history API uses correct sort column.** Changed `/api/2.0/crm/history/filter` from `sortBy=date` to `sortBy=created` to match the native CRM field and return the most recent events reliably.
- **Customer Bot: reduced history API timeout.** History calls now use a 10s timeout (down from 30s) so a slow/hanging history endpoint fails fast and does not block the entire response.
- **Customer Bot: CRM HTML sanitized for Telegram.** Added `_sanitize_html()` in `telegram_bot.py` to decode HTML entities, replace `<br>` with actual newlines, strip unsupported tags, balance Telegram-safe tags, and escape bare `&` characters inside tag attributes. This fixes "Something went wrong" crashes on deals with complex note content.

### Files changed
- `crm_bot_store.py`, `server.py`, `telegram_bot.py`, `public/app.js`, `public/index.html`, `public/styles.css`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [2.0.3] — 2026-06-27

### Added
- **Customer Bot: search by project title.** Linked customers now type a project name (or part of it). The bot searches open deals by title: 0 matches → not found; 1 match → full detail; 2+ matches → numbered list, reply with a number for detail. Added `?search=` parameter to `/api/bot/deals` (server-side filtering on already-fetched contact deals).
- **Customer Bot: `/help` command and help trigger words.** Customers can send `/help`, "help", or "?" for instructions.

### Changed
- **Customer Bot modal renamed.** User-visible title and button label changed from "Bot Customers" to "Customer Bot".
- **Customer Bot welcome message simplified.** After linking, customers are told to send a project name instead of receiving a full list.

### Fixed
- **Mobile: bot button actually moves below sign-out.** Previous CSS override was overridden by the desktop `.bot-customers-btn` rule due to source order. Moved mobile header-button overrides to after the desktop rule so the bot button sits at `right: 1rem` (directly under sign-out) and event-log at `right: 3.75rem`.

### Files changed
- `telegram_bot.py`, `server.py`, `public/app.js`, `public/index.html`, `public/styles.css`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `docs/RELEASE_v2.0.3.md`

## [2.0.2] — 2026-06-27

### Fixed
- **Presence: stale autoStatus also stripped in Team tile.** The v2.0.1 cleanup only ran in the modal roster; the embedded dashboard tile (`renderPresenceTileCompact`) now clears `autoStatus`/`status`/`inferred` for offline users too.

### Files changed
- `public/app.js`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `docs/RELEASE_v2.0.2.md`

## [2.0.1] — 2026-06-27

### Added
- **Nickname field for bot customer mappings.** New text input in the "Invite Customer" form to label each code (e.g. "Office manager"). Nickname shown in the Existing Mappings list with a ✎ edit button (inline prompt). New server endpoint `PUT /api/bot-customers/nickname`. Stored in `crm_bot_store.py` mapping entries and preserved through the verify-code flow. (`FEAT-025`)
- **Rich text (HTML) in bot messages.** All Telegram bot `reply_text` calls now use `parse_mode="HTML"`. CRM note content with `<b>`, `<i>`, etc. renders as formatted text. Plain-text fields (titles, stage names) are HTML-escaped to prevent breakage.
- **Crash banner: 15s grace period after tab resume.** Suppresses the amber banner for 15s when the tab becomes visible, preventing false positives from stale-connection failures due to browser throttle during background.

### Changed
- **Bot project summary cleaned up.** Latest customer note and amount removed from the summary list (only title + status shown). Full details including note and amount still visible in the detail view when customer replies with a number.

### Fixed
- `.gitignore` updated to exclude `romanian_roadtrip.md`.
- **Mobile: bot & event-log buttons no longer overlap header.** Stacked below sign-out on a second row (`@media max-width: 640px`).
- **Mobile: opportunity preview modal fills screen.** Overrode `96vw` width to `100%` inside the modal container to prevent right-side cutoff.
- **Presence: stale autoStatus no longer leaks for offline users.** After merging cache + snapshot, `autoStatus`/`status`/`inferred` are cleared for any entry not currently `online`. Prevents "Reviewing: … · last seen 23h ago" text.
- **Presence: team roster no longer goes blank on poll failure.** `fetchPresenceSnapshot()` catch no longer wipes `state.presenceData` — keeps last good snapshot so the tile/modal stays populated. Amber banner still shown.

### Files changed
- `public/app.js`, `public/styles.css`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `docs/RELEASE_v2.0.1.md`

## [2.0.0] — 2026-06-26

### Added
- **Telegram customer bot (`@vanguardupdates_bot`).** New async Python bot (`telegram_bot.py`) using `python-telegram-bot` v22.8. Customers send their invite code to get linked; then they can view open projects and reply with a number for full details.
- **Invite-code-based customer linking.** Admin generates an 8-char code in the dashboard modal, tells the customer, customer types it to the bot. Codes expire after 48 hours. Persisted via `crm_bot_store.py`.
- **Bot Customers admin modal.** Robot SVG trigger button in the header (right: 9.5rem, next to event-log). Searchable contact picker (CRM contact search), note category dropdown for history curation, code generation with Copy + expiry countdown + Cancel, existing mappings list with Linked/Pending badges and × unlink.
- **7 new server endpoints** for bot customer management, code verification, deal queries, and admin check. Bot CRM session managed via dedicated `bot@vanguardadj.com` account (token cached 50 min).
- **Admin detection from CRM `isAdmin` field.** `/api/2.0/people/@self` response checked; falls back to `kenc@vanguardadj.com` email match. Presence admin gating uses `state.currentUserIsAdmin`.
- **Bookmarked deals limit** increased from 15 to 20.

### Changed
- **Customer-facing bot text** uses "projects" instead of "deals".
- **Bot button** moved from `right: 10.5rem` to `right: 9.5rem` for consistent header spacing.
- `.env` and `config.example.env` updated with `TELEGRAM_BOT_TOKEN`, `BOT_CRM_EMAIL`, `BOT_CRM_PASSWORD`.

### Files changed
- `telegram_bot.py` (new), `crm_bot_store.py` (new)
- `server.py`, `public/index.html`, `public/app.js`, `public/styles.css`
- `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `README.md`, `config.example.env`
- `docs/RELEASE_v2.0.0.md`, `docs/GITHUB_RELEASES.md`

## [1.91.3] — 2026-06-26

### Added
- **Editable deal title in deal-edit modal.** Title input added at the top of the form (discreet, transparent background, no border, accent underline on focus). Validates non-empty on save. All entry points covered (card edit button, preview modal, search preview, bookmark sidebar).
- **CRM iframe proxy investigation:** Added `/crm-proxy/` test route to `server.py`. Confirmed `X-Frame-Options` can be stripped, allowing the native CRM to be embedded in an iframe. Documented findings and the recommended subdomain proxy approach in `FUTURE_FEATURES.md` (FEAT-024). Not implemented — parked.

### Fixed
- **Event note line breaks preserved.** Browser contenteditable `<div>` wrapping (Chrome wraps each paragraph in `<div>`) is now normalized to `<br/>` tags before sending to the CRM. Multi-line notes no longer arrive as a single text blob. Fix applies to both deal-edit and quick-note modals (both go through `noteContentToHtml`).
- **Create opportunity modal — tags moved after custom fields.** Tags selector was moved below the dynamic custom fields section (Inspection Photos checkbox area) and before the Private checkbox, matching the requested form order.

### Files changed
- `public/app.js`, `public/index.html`, `public/styles.css`, `server.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`, `FUTURE_FEATURES.md`

## [1.91.2] — 2026-06-25

### Fixed
- Dashboard no longer hangs for ~10 seconds after deal edit from bookmark preview / quick note. Removed redundant `refreshAll()` call from `submitDealEditForm` post-edit chain (which re-fetched every API, cleared all caches, and rebuilt all group tile DOM). Now runs preview, board, and bookmark tab refreshes **in parallel** via `Promise.all()` instead of sequentially via `.then()` chaining.
- Post-edit preview refresh now caps history pagination at 2 pages (100 items, down from 10 pages / 500). Full history is still loaded on initial preview open; only the auto-refresh after edit/note uses the fast path.
- Quick-note post-save chain also parallelized for consistency.

### Files changed
- `public/app.js`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [1.91.1] — 2026-06-25

### Fixed
- Event log no longer clears on hard refresh: `loadEventLogFromStorage()` is now called during `init()` so the localStorage backup is applied before the server merge runs.
- GUID validation error ("Guid should contain 32 digits with 4 dashes") when posting an event note with attachments AND notify users: `notifyUserList` is now sent as individual form-urlencoded parameters (`notifyUserList=guid1&notifyUserList=guid2`) instead of a JSON-stringified array, matching ASP.NET MVC's `List<Guid>` model binder convention.
- `validNotifyUserList` now filters with `isGuid()` instead of `filter(Boolean)`, rejecting non-GUID values before they reach the OnlyOffice API (JSON path).
- `validFileIds` now validates numeric format (`/^\d+$/`), rejecting non-numeric file IDs before sending to the upload endpoint.

### Files changed
- `public/app.js`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [1.91.0] — 2026-06-24

### Added
- Server-side event log persistence (`event_log_store.py`): 7-day rolling retention with 1,000-entry cap, per-user per-portal JSON files.
- Admin event-log tab: visible only for kenc@vanguardadj.com; shows user dropdown populated from CRM display names, filtered to users with event logs.
- Discrete CRM health check: `GET /api/health` endpoint; "Check CRM status" button in event log modal (no polling).
- GUID validation on history event creation: `notifyUserList` and attachment `fileIds` are validated client-side with `isGuid()` before sending to OnlyOffice; invalid entries are dropped with a console warning.
- Upload response validation: file IDs returned from the upload handler are checked for valid GUID format.

### Fixed
- Quick Note and Deal Edit modals no longer auto-close after 2.5s on failure; the modal stays open so the user can retry or copy their work.

### Files changed
- `event_log_store.py` (new), `server.py`, `public/app.js`, `public/index.html`, `public/styles.css`, `Dockerfile`, `test-server.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [1.90.1] — 2026-06-24

### Fixed
- `server.py` `do_POST` no longer crashes with `AttributeError: 'super' object has no attribute 'do_POST'` on non-API POST requests; now returns HTTP 405.
- Expanded linked emails in deal preview modal and bookmark preview once again fill the full available modal width. The `.opp-preview-mail-embed` panel is now appended directly to `.opp-preview-history-item` instead of inside the flex row shared with attachments.
- DM messages now show a discreet timestamp below each message bubble.
- Team tile popup button (⤢) now uses event delegation so it opens on the first click even after tile re-renders.
- Emoji picker no longer detaches when scrolling: it is fixed to the viewport and repositions on scroll/resize so it stays pinned to the emoji button.
- Mobile emoji picker now opens below the emoji button instead of being anchored to the bottom edge of the screen.

### Changed
- Toast, CRM sync status, and note queue indicators are now stacked vertically in a shared container. By default the stack is on the bottom-right; when a bookmark preview is open it moves to the bottom-left so indicators do not overlap the preview panel.

### Files changed
- `public/app.js`, `public/index.html`, `public/styles.css`, `VERSION`, `CHANGELOG.md`

## [1.90.0] — 2026-06-23

### Domain migration to publicadjustermidwest.com + notification cache rollback

- **Domain migration docs:** Added `docs/MIGRATE_DOMAINS.md` with full cutover guide for CRM droplet (DigitalOcean one-click Docker setup) and dashboard droplet (`estimate-nginx` reverse proxy).
- **Cutover runbook:** Added `docs/CUTOVER_RUNBOOK.md` as a one-page checklist; dashboard side runs first to minimize downtime.
- **Domain fallback updates:** Updated default portal URL fallbacks in `config.example.env`, `server.py`, and `public/app.js` to `https://office.publicadjustermidwest.com`.
- **Production context:** Updated `AGENTS.md` production dashboard URL to `https://dashboard.publicadjustermidwest.com`.
- **Notification cache rollback:** Reverted v1.88.0 server-side CRM mail-based notification cache and the two follow-up mail-parser fixes. Feed returns to using CRM history events directly. `notification_store.py` removed.
- **Files changed:** `AGENTS.md`, `CHANGELOG.md`, `VERSION`, `config.example.env`, `docs/CUTOVER_RUNBOOK.md`, `docs/MIGRATE_DOMAINS.md`, `public/app.js`, `server.py`

## [1.87.6] — 2026-06-23

### Chunked card rendering + initial-load responsiveness

- **Chunked card rendering:** `renderGroupBoard` now renders cards in batches of 20 with `setTimeout(0)` between batches, yielding the main thread so the event loop can service clicks (e.g. changelog modal close button) during initial load. Column shells (headers + empty bodies) are appended to the DOM immediately so the kanban layout is visible before cards populate progressively. Previously, all cards in a group were created in one synchronous loop — with 3 groups × 500 deals this blocked the main thread for seconds, triggering the browser's "page unresponsive" warning.
- **Changelog delay increased:** `maybeShowChangelog` delay raised from 300ms to 1500ms so the modal appears after the worst of the tile rendering is underway, not competing for main thread time during the initial render burst.
- **Files changed:** `public/app.js`, `public/index.html`, `VERSION`, `CHANGELOG.md`

## [1.87.5] — 2026-06-23

### Presence hardening — smart heartbeat + beforeunload + auto-status write-back + stale cleanup

- **Smart heartbeat (visible flag):** Heartbeats now include a `visible` flag — `send()` sends `{visible: true}` when the tab is focused, `sendBeacon()` sends `{visible: false}` on background tab / beforeunload. Server only bumps `lastDashboardActivity` when `visible=True`, so auto-status expires naturally after 5 min of background-tab inactivity instead of staying alive indefinitely.
- **Beforeunload handler:** Tab/window close now sends `navigator.sendBeacon` with `{offline: true}` so the server marks the user offline immediately instead of 10+ min later. Cleaned up in `stopPresenceHeartbeats`.
- **Auto-status write-back:** When the server filters out a stale `autoStatus` (>5 min) from the response, it now also writes the cleared value to disk via `clear_auto_status()`. Prevents stale values from persisting across sessions when the client's 5-min timeout never fires (browser closed unexpectedly).
- **Stale record cleanup:** `clean_stale_presence_records()` iterates all presence files and clears `lastHeartbeat` + `autoStatus` for records with heartbeats >3h old. Called on each presence GET. Handles edge cases from closed browsers that never sent beforeunload.
- **High Priority amber styling on bookmark sidebar tabs:** `.bookmark-tab--high-priority` CSS class with same amber treatment as card tiles — `rgba(240,180,41,0.08)` background, `var(--warn)` border, 3px amber left accent. `renderBookmarkTabs()` detects High Priority tag via `oppHasTag()`, checking deal `_cachedData` then falling back to group opportunities.
- **Files changed:** `presence_store.py`, `public/app.js`, `public/styles.css`, `server.py`, `VERSION`, `CHANGELOG.md`

## [1.87.4] — 2026-06-23

### Event log + indicator stacking + deal-edit optimization + refreshing spinner

- **Event log:** New header button (clipboard-list icon) opens a persistent event log modal showing all recent deal edits, note saves, attachment uploads, and deletes with timestamps. Persisted in localStorage (max 200 entries). Includes clear-all button.
- **Indicator overlap fix:** `#toast` (z-index 2003), `#note-queue-list` (z-index 2002), and `.crm-sync-status` (z-index 2001) now stack vertically without overlapping — sync-status moved to `bottom: 3.5rem` (above toast at 1.5rem), note-queue at `bottom: 5.5rem` (left side). When bookmark sidebar or a modal blocks the right side, toast and sync-status shift to the left; note-queue stays left always. CSS `.bookmark-open` rules handle the sidebar case; JS `repositionRightIndicator()` handles modals.
- **Optimized deal-edit submission:** Replaced 3 separate GET+PUT cycles (due date, stage, custom fields) with a single `updateOpportunityBulk()` call. Attachment uploads now run in parallel (`Promise.all`) instead of sequential. Dynamic close timer: base 2.5s + 0.5s per MB of attachments (prevents premature modal close during large uploads). Tag operations throttled to 150ms between sequential calls.
- **Refreshing indicator:** After deal-edit or quick-note save, the status bar now shows a persistent "Refreshing CRM data..." spinner during deferred board/preview refresh work (instead of going silent). Spinner hides when all deferred operations complete. Sequential promise chains prevent main-thread contention.
- **Improved attachment upload response parsing:** Multiple extraction strategies (raw JSON, HTML-wrapped extraction, field fallbacks) with more robust error messages.
- **Server timeout:** `urllib.request.urlopen` timeout increased to 120s for large file uploads.
- **Files changed:** `public/app.js`, `public/index.html`, `public/styles.css`, `server.py`, `VERSION`, `CHANGELOG.md`

## [1.87.3] — 2026-06-19

### Hotfix: CRM-down resilience + inbox shows all conversations + tag-column/stale-column fixes

- **CRM-down resilience — presence user ID cache:** `_fetch_crm_user_id` called the CRM on every request — when CRM goes down, `_require_auth` fails for all presence endpoints, killing team view and messaging entirely. Added `_crm_user_id_cache` dict (token → user_id) so the first call caches the result; subsequent requests bypass CRM. Logout clears the cache entry.
- **CRM-down resilience — overlay-only users in snapshot:** When the CRM people API is unreachable, the presence snapshot now includes users with heartbeat records in the local store (with `displayName: uid` fallback), so the team view shows active users even without CRM data.
- **Inbox shows all conversations:** `get_recent_dms_for_user` previously returned the 50 most-recent messages globally — a single thread with 50+ messages after "Load earlier" would crowd out every other conversation. Rewritten to return the **latest message per conversation partner**, ensuring each distinct person appears at most once in the inbox regardless of thread depth.
- **DM back button + tab switching:** Back button now renders inbox instantly from cached snapshot then background-refreshes. Messages tab hides the DM thread and clears `presenceSelectedUserId` before rendering inbox, fixing overlap/empty-state issues.
- **Stale tag columns:** `groupOpportunities` auto-detect path filters out tags not in `state.allTags` before creating columns; opps with only stale tags fall to "Untagged".
- **Null-guard in `updateAllCardCopies`:** Added `if (!boardEl) return;` guard to prevent crash when the group tile DOM is not present during targeted card update.
- **Files changed:** `presence_store.py`, `public/app.js`, `server.py`, `VERSION`, `CHANGELOG.md`

## [1.87.2] — 2026-06-18

### Hotfix: Mobile emoji picker + auto-status server expiration + bookmark backdrop + batch tag search + search result enhancements

- **Emoji picker on mobile:** `showPopupEmojiPicker()` and `showInlineEmojiPicker()` now detect narrow viewports (< 480px) and position the picker full-width below the message input area (left+right anchored), so it opens to the left and doesn't block the textarea or get cut off.
- **Auto-status server-side expiration:** Server now strips `autoStatus` from presence responses when the user is not online or `lastDashboardActivity` is older than 5 minutes (client-side timeout may not have fired if the browser was closed). Added `PRESENCE_AUTO_STATUS_TIMEOUT_S = 300` constant. Same check applied to the caller's own `me` record.
- **Bookmark sidebar preview backdrop:** New `.bookmark-preview-backdrop` overlay (rgba black 55%, z-index 1499) shown when a bookmark preview tab is active, matching the dimming behavior of other modals.
- **Batch tag fetching:** New server endpoint `GET /api/batch-opportunity-tags?ids=1,2,3` fetches tags for multiple opportunities in parallel server-side (`ThreadPoolExecutor`). Client's `enrichOpportunitiesTags()` now calls this batch endpoint first, falling back to individual requests if it fails. This eliminates the N+1 bottleneck in tag search.
- **Search result bookmark ribbon:** Each search result row now has a bookmark toggle button (bookmark ribbon SVG) to the left of the "+ Tab" button. `refreshAllBookmarkButtonStates()` updates these buttons alongside existing bookmark buttons.
- **Search result CSV export:** "📋 Export CSV" button at the bottom of search results downloads a CSV with columns: Deal Title, Stage, Due Date, Contact, Bid Value. UTF-8 BOM included for Excel compatibility.
- **Files changed:** `public/app.js`, `public/index.html`, `public/styles.css`, `server.py`, `VERSION`, `CHANGELOG.md`

## [1.87.1] — 2026-06-17

### Hotfix: Cross-user cache contamination + date timezone shift

- **Root cause — proxy cache key missing user identity:** `_cache_key` in `server.py` used only `method:api_path:query:portal`, so one user's CRM filter results were served to other users within the 30-second TTL. This caused users to see wrong deal counts, stale due dates, and other users' personalized data. Fix: added session token to cache key so each user's data is segregated.
- **Date-only parsing for expectedCloseDate:** `new Date(raw)` on the CRM's timezone-qualified ISO string caused a one-day date shift in negative UTC offset timezones. Added `parseCrmDateOnly()` helper that extracts `YYYY-MM-DD` and builds a Date at local midnight. Applied in 6 consumers: `isRedOpportunity`, `formatOppDueLabel`, `dueDateToInputValue`, `formatPreviewDueDate`, `oppDueDateMs`, and bookmark tab due label. No changes to datetime fields (notes, feed, calendars, tasks).
- **Force refresh after mutations:** Changed 4 mutation fallback calls from `refreshGroup(group)` to `refreshGroup(group, { force: true })` so the client-side 30-second cache is bypassed after edits/deletes, ensuring the group UI immediately reflects the change.
- **Files changed:** `server.py`, `public/app.js`, `VERSION`, `CHANGELOG.md`

## [1.87] — 2026-06-17

### Presence auto-status fixes & enhancements
- **Root cause — auto-status not displaying:** `updateInferredStatus` was changed to send `{ autoStatus: text }` only, but the server and rendering expected `{ status, autoStatus, inferred }`. This broke both new-server rendering (no auto-status shown) and old-server compatibility (empty string rejected as 400).
- **Fix — dual-compat payload:** `updateInferredStatus` now sends `{ status: text, autoStatus: text, inferred: true }` — the server stores both fields, new server renders auto-status on the right, old server reads `status` directly. The tile "Online" option value was changed from `""` to `"Online"` so old server accepts it. `syncTileStatusSelect` uses strict `inferred === true` and falls through gracefully for old server (undefined).
- **DND / manual status preservation:** `confirmCustomStatus` for a manual set now sends `{ status, inferred: false, autoStatus: "" }` and sets `state._suppressAutoStatus = true` to block auto-status overwrites. Clearing to "Online" sends `{ status: "Online" }` (no autoStatus) and re-enables auto-status. Auto-status never overwrites a manually-set DND.
- **AFK auto-clear race condition:** `noteDashboardActivity` fires on every mousedown. When it detected AFK and sent `{ status: "Online", autoStatus: "" }`, it could race with `updateInferredStatus` and wipe out the auto-status. Fix: AFK clear now sends `{ status: "Online" }` **without** `autoStatus` so the server preserves whatever `updateInferredStatus` set. Added `_afkClearSent` debounce to only send once per AFK cycle.
- **Auto-status timeout:** New `PRESENCE_AUTO_STATUS_TIMEOUT_MS = 300000` (5 min). When `updateInferredStatus` sets a preview/edit/note auto-status, a timeout is scheduled. If no new activity fires within 5 minutes, the auto-status is cleared (sends `{ autoStatus: "" }`) so it doesn't look like the user is still reviewing the same deal.
- **Bookmark & search preview auto-status:** `updateInferredStatus("preview", title)` now fires when opening a deal from the bookmark sidebar (`activateBookmarkTab`) and when opening/switching search popup preview tabs (`openSearchPreviewTab`).
- **Presence rendering adaptive:** Tile and popup rendering detect `serverHasInferred` per row. New server shows manual `(DND)` in bold next to name, auto-status on the right. Old server falls back to showing all non-"Online" statuses on the right.
- **`set_status` preserves autoStatus:** Server-side `presence_store.set_status` now only writes `autoStatus` when explicitly provided (`autoStatus is not None`), never clobbering it with `None`/default.
- **`_suppressAutoStatus` guard:** New state flag blocks `updateInferredStatus` when a manual status (DND, AFK) is active. Reset on AFK clear or manual clear to "Online".
- **Popup status picker compatibility:** Uses same payload patterns as the tile (`{ status: "Online" }` for clear, `{ status, inferred: false, autoStatus: "" }` for manual). Added `_suppressAutoStatus` integration.
- **Error toast removal:** All `showToast` calls removed from presence fetch error handlers — failures are silent.
- **Files changed:** `public/app.js`, `public/styles.css`, `server.py`, `presence_store.py`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

### Bookmark tab card styling
- Added `border: 1px solid var(--border)`, `border-radius: 6px`, stronger `box-shadow`, and increased `margin-bottom` to 4px on `.bookmark-tab` so each bookmark card stands out as its own card.
- **Files changed:** `public/styles.css`

### Preview collapse chevron
- Added a right-facing chevron button (matching the bookmark sidebar toggle) to the left of the deal title in the bookmark preview header. Clicking it calls `closeBookmarkPreview()` — same behavior as clicking the active deal name tab.
- **Files changed:** `public/index.html`, `public/app.js`

## [1.85] — 2026-06-16

### Bookmark sidebar — collapsible right sidebar with bookmarked deal previews
- **Feature:** Vertical bookmark trigger tab on right edge (one-letter-per-row "Bookmarks" text). Click opens a sidebar with deal tabs.
- **Bookmark button:** Ribbon icon on every deal card (bottom-right) and search popup preview headers. Filled = bookmarked, outline = not. Click toggles state.
- **Sidebar:** 220px strip with vertical deal tabs showing stage dot, title, stage name, due date. Filled ribbon icon on each tab removes bookmark. Drag-and-drop reordering of tabs. Max 15 bookmarks.
- **Preview panel:** 880px deal detail preview (standard fields, user fields, description, history, documents) using same renderer as modal preview. Refresh button, edit button (opens edit modal to the left), filled ribbon to remove bookmark.
- **Persistence:** Bookmarks survive logout/login via localStorage + server user profile.
- **Edit button:** Opens the deal edit modal positioned to the left of the sidebar preview so both are visible simultaneously.
- **Tab shadows:** Subtle box-shadow + margin on each tab for visual distinction.
- **Sidebar toggle:** Chevron-right arrow (not filled ribbon). Preview panel expands left of the strip.
- **Files changed:** `public/index.html`, `public/app.js`, `public/styles.css`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [1.8.2] — 2026-06-15

### Search popup — large modal with tabbed deal preview
- **Feature:** New search button in the header (magnifying glass icon, `btn-secondary`, left of the existing search field). Opens a large modal with a search bar and results list.
- **Search:** Searches all open deals (`stageType=0`) via `/api/2.0/crm/opportunity/filter?filterValue=...`. Results are enriched with tags. Search triggers on Enter key press.
- **Results list:** Compact single-line rows showing: deal title (bold), stage · due date · contact · bid value. Inline buttons: "Preview" (opens a tab) and "Open in CRM" (new tab).
- **Preview tabs:** Max 5 tabs. Each tab shows the full deal preview (standard fields, user fields, description, history & notes, documents) using the exact same rendering as the existing preview modal. Each tab has a deal title and a × close button.
- **Search tab:** Always present, styled with a left accent border and search icon. Clicking it returns to the search results.
- **Edit button:** Inside each preview tab, top-right. Opens the deal edit modal pinned to the left (same behavior as the existing preview modal).
- **Refactoring:** `renderOpportunityPreviewBody()` now delegates to `renderOpportunityPreviewContent(container, data)` — the same rendering logic works in both the old modal and the new popup tabs.

### Bug fixes
- **Tab content shrinking on switch:** `activateSearchPopupTab()` used a broad selector `[data-tab-content]` that accidentally matched the inner Details/Documents tabs inside the preview body. Switching outer tabs hid both inner tabs, making the preview appear empty/shrunken. Fix: selector narrowed to `.search-popup-tab-content` only.
- **Missing search input on modal open:** Child combinator `>` in the selector fix prevented matching the search tab content (nested inside `.modal-card-search-popup`). Fix: changed to descendant selector `.search-popup-tab-content`.
- **Height instability:** `#search-popup-preview-containers` and the search tab content both had `flex: 1` as siblings, splitting the modal space 50/50. When search tab was active, the empty preview containers ate half the height. Fix: `activateSearchPopupTab` now hides the preview containers when search tab is active.
- **No width scroll on tab bar:** Tab bar uses `overflow-x: hidden` with `flex-shrink: 1` on tabs and `text-overflow: ellipsis` on tab titles.
- **Centered error for max 5 tabs:** Error message now renders as a centered banner in the search results area instead of a background toast.

- **Files changed:** `public/index.html`, `public/app.js`, `public/styles.css`, `VERSION`, `CHANGELOG.md`, `AGENTS.md`

## [1.8.1] — 2026-06-14

### Deal caching — refresh now pulls fresh data
- **Root cause:** `refreshAll()` cleared in-memory caches then immediately rehydrated from IndexedDB before the clear transaction committed, repopulating stale data. Also, server-side proxy cache (30s TTL for single-opp, 600s for tags) returned stale data after tag/note edits.
- **Fix:** `refreshAll()` no longer rehydrates from IndexedDB — hydration only happens once on initial page load via `initCaches()`. `enableCachePersistence()` is now idempotent (guards against double-wrapping). Server-side cache invalidation now covers `DELETE` mutations and invalidates the single-opp `GET:/api/2.0/crm/opportunity/{id}` cache line when tags change. The preview modal refresh button (`⟳`) now appends `?_t=Date.now()` to bypass the proxy cache entirely.
- **Files changed:** `public/app.js`, `server.py`

### Email width — expanded emails now fill available space
- **Root cause:** `.mail-detail` and `.opp-preview-mail-embed` had no explicit `width: 100%`, and the mail inbox modal was using `.mail-expanded-embed` (a class with no CSS rules) instead of `.opp-preview-mail-embed`.
- **Fix:** Added `width: 100%; box-sizing: border-box` to `.mail-detail`, `.opp-preview-mail-embed`, and `.opp-preview-mail-body`. Changed the mail inbox JS to use `.opp-preview-mail-embed` class.
- **Files changed:** `public/styles.css`, `public/app.js`

### UI freeze when returning from background tab
- **Root cause:** When a tab is backgrounded, Chrome throttles timers and defers IntersectionObserver callbacks. Returning to the tab fires all deferred callbacks simultaneously, triggering up to 25+ concurrent `fetchOpportunityCustomFields()` calls (5 per group × 5+ groups).
- **Fix:** Added a 50ms debounced batch collector for intersection callbacks. Added `document.visibilitychange` listener that disconnects all observers when hidden and re-observes after a 500ms delay when returning. `enqueueOpportunityCustomFieldEnrich()` skips work entirely when `document.visibilityState === 'hidden'`.
- **Files changed:** `public/app.js`
- **Issue:** ISSUE-004 — resolved

### Attachment note indicator — queue status now visible
- **Root cause:** `createOpportunityHistoryEvent()` returned `{ queued: true }` on transient CRM errors but callers didn't check the return value, so notes appeared "sent" even when they were only queued for retry.
- **Fix:** `createOpportunityHistoryEvent()` now explicitly returns `{ queued: true }` or `{ success: true }`. `submitDealEditForm()` and `submitQuickNoteForm()` check the return value and show a toast: "Note queued for retry (CRM is temporarily down). Check the header indicator." The note queue indicator in the header was also made more visible (amber background, larger font).
- **Files changed:** `public/app.js`, `public/styles.css`

### Quote font — easier to read at small size
- **Root cause:** The daily quote used "Instrument Serif" (decorative italic serif) at 0.8rem, which was tight and hard to read.
- **Fix:** Changed to `var(--font)` (DM Sans / system-ui sans-serif), increased to 0.85rem, added `font-weight: 500`, increased line-height to 1.45. Reverted to italic style per user request.
- **Files changed:** `public/styles.css`

### Preview modal stale data — manual refresh bypasses cache
- **Root cause:** The preview modal refresh button (`⟳`) re-fetched the same API paths, which were cached by the server-side proxy for 30 seconds (opportunity data, history) or 600 seconds (tags, custom fields).
- **Fix:** Added `bustCache(path)` helper that appends `?_t=Date.now()`. The preview modal refresh button now passes `force=true` through the entire chain: `openOpportunityPreviewModal(…, true)` → `fetchOpportunityPreviewData(…, true)` → each individual fetch function. This guarantees fresh data on manual refresh.
- **Files changed:** `public/app.js`

### Amber border lost after deal edit
- **Root cause:** `fetchOpportunityForUpdate()` returns the core opportunity object but does **not** include tags. The single-opp refresh path in `submitDealEditForm()` and `submitQuickNoteForm()` updated the card with the untagged opportunity, so `renderCard()` couldn't detect "High Priority" and the amber border/background disappeared.
- **Fix:** Added `await enrichOpportunitiesTags([updatedOpp])` immediately after `fetchOpportunityForUpdate()` in both submit paths. This fetches tags for the updated opportunity before rendering the card, preserving the amber styling.
- **Files changed:** `public/app.js`

### Changelog popup — shown once per version on first login
- **New feature:** Modal that displays `CHANGELOG.md` content on first login after a version update. Uses the existing `renderBasicMarkdown()` renderer (headers, lists, bold, italic, links, code). Close button + Escape + backdrop click dismiss the modal. The seen version is persisted in `localStorage` under `changelog_seen_version`, so the modal only reappears when the version changes.
- **Files changed:** `public/index.html`, `public/app.js`, `public/styles.css`, `server.py`
- **Server endpoint:** `GET /api/changelog` returns `CHANGELOG.md` as `text/markdown`

---

## [1.8.0] — 2026-06-12

### Stale Deals Tile — Attempted, debugged, and scrapped
- **Attempt A (activity-based)**: Tried to use `state.feedRawItems` (CRM notifications feed) to find last activity per opportunity. Failed because feed only covers last 30 days (max 150 events), so most opportunities had no activity found.
- **Attempt B (due-date-based)**: Switched to `expectedCloseDate` on opportunity objects. Added threshold dropdown (1 week / 30 days / 90+ days). Still failed to list any deals in any time period during testing.
- **Scrapped**: Tile removed from UI and code entirely. All stale deals functions, constants, CSS, and HTML removed from `public/app.js`, `public/index.html`, `public/styles.css`.
- **Root cause**: `expectedCloseDate` on open deals was not reliably past-due in the test data. The concept of "stale" needs a different data source (e.g., actual last-modified timestamp on opportunities, which the CRM API does not provide directly).
- **Future**: May revisit with a proper CRM-side query or different staleness metric. See ISSUES.md for full post-mortem.

### Fix: Production version display showing "vdev"
- **Root cause**: `Dockerfile` did not copy the `VERSION` file into the container. `server.py` fell back to `APP_VERSION = "dev"`, so the UI showed `vdev`.
- **Fix**: Added `COPY VERSION ./` to `Dockerfile`.
- **Verify**: On next Docker build, `GET /api/config` should return `"version": "1.8.0"`.

## [1.7.6] — 2026-06-12

### Feed: notify auto-inject experiment (tried and reverted)
- Attempted Part A: auto-inject `[Notified: Name1, Name2]` text into event content when creating history events with notify users, so the keyword filter could find them.
- Added `renderFeedNotificationItem` detection of `[Notified: ...]` suffix with `.feed-notified-suffix` CSS (smaller/italic/muted).
- **Reverted**: User reported text was "squished against the note" and same font size. Feature scrapped — manual `@ken` keyword filter works fine.

### Feed: mail events removed
- Removed `fetchFeedMailInitial` call, mail batch branch in `loadMoreNotificationFeed`, and `mailExhausted` from `feedCanLoadMore`. Mail events (CRM. New event added to...) no longer appear in the feed.

### FEED_MAX_EVENTS: 200→150→100→150
- Settled at 150 (mail removal already reduces noise sufficiently).

### UI: Preview modal fields styled as cards
- Each `.opp-preview-field` now has `border`, `border-radius: 8px`, `box-shadow`, and `background: var(--surface-2)` — like edit modal fields.
- Checkbox values ("Yes"/"No") render as small pill `.field-value-tag`.
- Success probability field removed from preview.

### UI: Tasks rendered as cards (not rows)
- `.task-row` now has `border-radius: 8px`, `border: 1px solid var(--border)`, `box-shadow`, and `margin-bottom` instead of `border-bottom`.
- Full-width layout toggle button removed from tasks tile and CRM notifications tile toolbars.

### Server: gzip + caching
- Static files served with `Cache-Control: public, max-age=86400`.
- Gzip compression for all responses (`Content-Encoding: gzip`; app.js 553KB→134KB, 75% reduction).
- Proxy-side response cache: 60s TTL for tag/stage/customfield, 15s for filter/history.

### Other
- `modal-card-deal-edit .modal-form` now has `scroll-padding-bottom: 4rem` so content doesn't hide behind sticky action bar.

## [1.7.5] — 2026-06-11

### Performance: tag cache, custom field cache, filter result cache
- **Tag cache** (`state.oppTagCache`, 5-min TTL): checked in `enrichOpportunitiesTags` before per-opp `tag/{oppId}` API calls; stored on fetch success; cleared on `refreshAll`; invalidated per-opp after tag mutations in `submitDealEditForm`, `submitQuickNoteForm`, and mutation queue handler.
- **Custom field cache** (`state.oppCustomFieldCache`, 5-min TTL): checked in `enqueueOpportunityCustomFieldEnrich` (cached → immediate `updateOpportunityCardDom`, no queue); stored in `fetchOpportunityCustomFields`; cleared on `refreshAll`; invalidated per-opp after `updateOpportunityCustomFieldsViaPut`.
- **Filter result cache** (`state.filterResultCache`, 30-sec TTL): caches raw API response from `/api/2.0/crm/opportunity/filter` before client-side filtering. Keyed by `groupId + baseQs` only (tagTitles/red filters excluded since they're client-side). Moved store *before* client-side filtering so adding/removing tag filters still hits the same cache entry.
- New helpers: `createTtlCache(ttl)` for filter result cache, `createOppCache(ttl)` for opp-specific caches.
- Cache invalidation in mutation handlers, deal edit, quick note.

### UX: tile loading indicator (200ms debounce)
- `refreshGroup` shows "Refreshing deals…" with spinner in tile board area after 200ms debounce; cleared on both success and error paths.

### DM linkify
- New `linkifyUrls(container)` function (TreeWalker pattern) converts bare `https?://` URLs to `<a>` tags inside `.presence-msg-text`; called at end of `renderDMLog`.

### Feed cap reduction
- `FEED_MAX_EVENTS` 200→150, `FEED_DAYS` 90→30 to reduce feed API payload on every refresh.

### Performance: batch tag enrichment
- **Replaced serial 12-at-a-time per-opportunity tag fetches with fully concurrent `Promise.allSettled`.** Previously, `enrichOpportunitiesTags` batched 12 concurrent requests in a `for` loop with `await`, creating N/12 serial rounds of network waterfall (e.g., ~13 rounds for 150 opps). Now all per-opportunity tag requests fire concurrently — the browser manages its 6-per-domain connection pool naturally, and the function returns once all responses settle. Eliminates the serial blocking gap between batches and reduces wall-clock time for tag resolution by ~10x (from 13+ round-trips to 1 effective round-trip).
- No change to `state.allTags` loading (tag definitions are still fetched in one batch via `loadAllTags()`).

### UX: CRM loading indicator
- **Bottom progress bar** now shows "Loading CRM data…" during `refreshAll` and stays visible until ALL CRM tile data (groups, feed, tasks) finishes loading in the background. The dashboard renders immediately; the indicator tracks completion via a promise callback, not by blocking.
- Calendar tiles (3rd‑party Proton Calendar) fire independently and don't affect the indicator.
- **Refresh button spins** while CRM data is loading (CSS animation on the SVG icon), stops when background CRM loads complete.
- `loadExpandedDashboardTiles` separates CRM-origin tiles from calendar tiles; only CRM promises are tracked for the indicator. The function always returns a promise for completion observation without blocking.
- `isCrmOnlyTileId` helper distinguishes CRM-origin tiles (groups, feed, tasks) from non-CRM tiles (calendar).
- `hideCRMSyncStatus` respects `_crmRefreshing` flag: mutation status messages can briefly override the text, but `hideCRMSyncStatus` restores "Loading CRM data…" when a global refresh is in progress.

### Mail inbox fixes and enhancements
- Restored pagination: added `&page=` param back to conversations API (server supports it).
- Added "Mark all loaded as read" toolbar button for bulk marking.
- Restored account selector pulldown: unhidden, functional change listener, filters by `&accountId=`.
- Restored unread badge on mail header button: removed `style="display:none"` and early return guard.
- Link debug: added `console.warn` for primary link failure + `console.warn` for fallback failure so root cause is visible in DevTools.
- Updated link toast to "Linked as note (primary link failed)" when falling back.
- Cache-Control: all static files now served with `no-cache, must-revalidate` (not just favicons).
- Updated button title from "CRM Mail Inbox (viewer only)" to "CRM Mail Inbox".
- Added `console.debug` of conversation object keys on load to diagnose missing read flag fields.

### Mail quick view UX polish
- Made close button larger (removed `btn-small`, added explicit padding/font-size).
- Moved limitations note from footer to right sidebar as a bullet list under "Quick View Limitations".
- Attachments now pre-render "Open in Mail" link immediately when expanding an email (no download attempt cascade).
- Added today counter `N (Today)` in toolbar showing how many of the loaded conversations are from today.
- Search now passes `&search=` param to conversation API so it actually filters server-side.
- Query param `search` added to `loadMailMessagesForModal`.
- Attachment filenames are plain text (no click handler) to avoid noisy 404/403 console errors; only "Open in Mail" link is actionable.

### Bug fixes
- Feed notifications: filtered out raw JSON/mail-metadata dumps (`{from "...", ...}`) that appeared as broken text in the feed.
- Quick edit modal: sticky save/cancel buttons at bottom of card; note editor max-height reduced to 6rem with resize disabled, preventing buttons from being pushed off-screen on smaller screens.
- Deal preview: hid "Actual close" field per request.

### User fields in deal edit
- Added "Show User Fields" toggle button to deal edit modal that reveals editable custom user fields (reuses create-opp field rendering).
- Fields are pre-populated with the deal's current saved values; text, textarea, select, checkbox, and date types supported.
- Changes are submitted via `updateOpportunityCustomFieldsViaPut` on save.
- User fields container has `max-height: 30vh` with internal scroll so it doesn't push save/cancel buttons off-screen.

### Dashboard performance: CRM tile isolation
- CRM-dependent tile loads (groups, feed, tasks, calendars) are now fired as background promises — they no longer block `refreshAll()` or the initial dashboard render.
- `loadExpandedDashboardTiles` no longer `await`s CRM tile data; tiles update asynchronously when their data arrives.
- `refreshGroup` defers DOM rendering via `setTimeout` so deal edits and other mutations don't freeze the UI during group board re-render.
- Status text updates immediately instead of waiting for all CRM tiles to finish.
- Local-only features (notes, calendars from cache, profile, layout) are fully isolated from CRM server latency.

### Other
- Created `docs/DEPLOY.md` with correct production path `/opt/vanguard/onlyoffice-crm-kanban`.
- Updated reference in `UPDATE_AND_DEPLOY.txt` to point to `docs/DEPLOY.md`.

## [1.7.0] — 2026-06-11

### FEAT-002: Custom user fields on opportunity create (ISSUE-001 — FIXED)
- Custom fields on new opportunity create now actually persist in CRM. Root cause: `collectCreateOppCustomFieldValues()` used `[data-custom-field-id]` which matched the wrapper `<div>` (set by `renderCreateOppCustomFields`) instead of the actual `<input>`/`<select>`/`<textarea>`, so `.value` was always `undefined` and every field was silently skipped.
- Fix: finds the wrapper div by data attribute, then queries `input, select, textarea` inside it to get the real user-entered value.
- Also corrected `buildCustomFieldListForApi` to `{key, value}` format (camelCase, no duplicate `Key`/`Value` props) and added `customFieldList` to create body alongside per-field POST fallback.
- `CREATE_OPP_USER_FIELDS_ENABLED=true`. Tested and confirmed end-to-end (dashboard → proxy → CRM).

## [1.6.1] — 2026-06-11

### FEAT-003: Attachments on event notes
- Full support for attaching files (≤25 MB each) when creating event notes from Deal Edit and Quick Note (including side-by-side from preview and prefill from notes tile).
- Native CRM upload flow: client uploads via proxied UploadProgress.ashx (multipart, using current UserID), then history create uses form-urlencoded with repeated `fileId[]`.
- Text is always priority (note sent even if some/all uploads fail; per-fail error toast).
- Pre-submit selected files: plain filename (size) list with × remove (no icons).
- Right-side status list in header (near mutation-sync-status): shows pending + all completed note actions; success = checkmark, fail = ✕; completed items auto-clear after exactly 10 seconds.
- Reuses existing preview attachment rendering, queue resilience (history items), rich note editor, and currentUserId.
- Server proxy updated to correctly forward form-urlencoded (for history) and multipart (for uploads) so attachments work through the deployed dashboard (extra hop).
- Hotfix for production uploads: added `X-OnlyOffice-Portal` header to the direct `UploadProgress.ashx` fetch (was missing, causing attachments to fail on the droplet proxy while local and text notes succeeded). Confirmed working after nginx client_max_body_size + proxy buffering config.

### Additional fixes
- Mobile vertical stacking of deal preview + deal/quick-note editor: improved dynamic measurement, ResizeObserver, and constraints in `layoutSideBySideDealEditAndPreview` to prevent overlap at top when stacked.
- Hanging after deal edits and event notes (prod): deferred the preview re-open in `submitDealEditForm` (was immediate heavy fetch+render); board refreshes already deferred. (Does not happen on local test server.)
- Added version number (v1.6.1) next to "Sign out" button (in header meta, to the right of portal URL, properly spaced).
- Rolled back "show empty stages" checkbox visibility for tag-sorted groups (the change had broken group tiles rendering); now only shown for stage groupBy as before.

See AGENTS.md for implementation details. These (plus proxy support for attachments on server) are released as v1.6.1.

## [1.6.0] — 2026-06-10

### Post-1.4.5 bug fixes (cross-device reliability + no-UI-hang resilience)
- DM "of the day" inbox now correctly shows read/unread cross-device: `renderPresenceInbox` isUnread logic changed to only flag when *latest* message in thread is incoming (from other) and ts > lastReadDms; self-sent responses no longer make threads appear unread on other devices. Added `markPresenceDMRead` immediately after successful DM send.
- Eliminated random UI hangs / "nothing clickable" / browser "page not responding" after quick notes and other pushes: post-submit refreshes (preview + refreshGroup/refreshAll) now always deferred via setTimeout (non-blocking); same for queue processor's refreshAll on stage/due/tag mutations. History notes already skipped from full refresh.
- Status and DM read state (lastReadDms) persistence reinforced across devices and page refreshes/logins (server already persisted in per-user presence json; client now always syncs on snapshot + explicit mark on send/open; no resets except on explicit logout clearing hb).
- Mobile quick-edit from preview no longer overlaps: `layoutSideBySideDealEditAndPreview` now dynamically measures actual side card bottom + 4px gap and repositions preview top (and max-height) on mobile so borders almost touch cleanly (no hard 260px offset).

### Additional fixes
- Quick note prefill from notes tile now correctly renders as rich formatted HTML (markdown converted via `renderBasicMarkdown` into the contenteditable) + explicit clear of the editor on open. (Previously plain text/markdown would appear unformatted.)

See AGENTS.md last-session summary for details. These changes (plus the earlier proxy response handling) are now released as v1.6.0.

## [1.4.5] — 2026-06-09

### Side-by-side preview note editor (quick note / edit from preview)
- Edit note button (or quick note in preview context) now opens as a separate "side popup" to the left of the preview modal on desktop (or fixed to the top of the preview on mobile <700px width).
- Both preview (history/details) and the note editor (rich B/I/U/H formatting, tags, due, notify) remain fully interactive simultaneously (pointer-events:none on side modal container + auto on card; fixed positioning + z; preview backdrop provides dim).
- Auto-refreshes preview on successful note submit (quick note or deal-edit note) so new history event appears immediately.
- Escape in preview closes side editor first (keeps preview open).
- Added manual refresh button (⟳) in preview head (left of ✎ edit) to force re-fetch current opp preview (useful after side note or external changes).
- Delete × button now available on event note history items inside preview (only for note-category non-mail events; uses confirmDialog + DELETE /api/2.0/crm/history/{id}; re-opens preview to refresh list).

### Presence / Team: AFD (away from dashboard) status + accuracy
- "Online" = tab visible + recent heartbeat (<10m).
- "Away from dashboard" (AFD): tab backgrounded but still has active session (stale hb record, not cleared by logout; subtle slate-gray dot + "Away from dashboard (N)" section in roster).
- "Offline": signed out (manual or auto 3h) — server clears lastHeartbeat on /api/logout so immediately offline (or aged >3h).
- Auto-logout aligned to 3h (was 4h); timer + visibility listeners.
- "Last CRM (proxy):" confirmed only from real proxied /crm/ or /people/ calls (touch_crm_activity), not heartbeats.
- Roster splits Online / Away from dashboard / Offline; compact tile respects AFD dots.

### Feed / notifications: today indicator
- All notifications from the current day (local date match on it.date) now get a subtle white left border line (`.feed-item-today` + border-left: 3px solid rgba(255,255,255,0.25); adjusted padding) in the CRM notifications feed.

### Crash / 5xx resilience (CRM unreachable banner)
- On 502/5xx (or transient proxy/CRM down errors) from api() or presence: shows persistent amber crash banner on right side of header meta ("CRM is temporarily unreachable and may have crashed. Refresh again in 30 seconds or contact system administrator.").
- Banner is subtle amber (#f59e0b bg, dark #1f2937 text for readability in dark/light), non-dismissible except by successful CRM response (onCRMSuccess hides) or page reload.
- No raw error toasts for transients during crash; status stays usable.
- All tiles still render (board, feed, tasks, etc.); CRM-pulling tiles/sections show no content (empty state from failed loads) while non-CRM parts (local notes, layout) continue.
- Quick note / side edit from preview still works (local), and preview auto-refreshes on submit even during partial outage.
- 30s guidance in message; throttle via existing transient queue logic.

### Documentation & Housekeeping
- Version bumped to 1.4.5 everywhere (VERSION, AGENTS.md, README, CHANGELOG, new RELEASE_v1.4.5.md, docs/GITHUB_RELEASES.md, docs/UPDATE_AND_DEPLOY.txt, docs/DEPLOY_*.md).
- Added detailed release notes + updated AGENTS.md "Post-v1.2 shipped items" with the new UX (side preview editor + delete + refresh, AFD presence, today lines, crash banner + partial render).
- Session changes from crash sim, side note flow, delete, feed today, presence AFD all documented.
- Local server close + prod deploy checklist followed (see below).

See also [docs/RELEASE_v1.4.5.md](./docs/RELEASE_v1.4.5.md) for the full GitHub release text.

## [1.4.1] — 2026-06-08

Patch release to ensure reliable deployments after v1.4.0.

### Deployment / Docker fix
- Updated Dockerfile to explicitly `COPY presence_store.py` (along with the other .py modules). The v1.4.0 tag's Dockerfile was missing it, causing `server.py` to fail on `from presence_store import ...` at container startup. This resulted in the dashboard container crash-looping (Restarting (1)) and nginx returning 502 Bad Gateway on production deploys.
- Added verification steps and warnings in docs/UPDATE_AND_DEPLOY.txt and docs/DEPLOY notes to always check that the built image contains all required Python modules (presence_store.py is critical for the Team/Presence feature introduced in v1.3+).
- In future, any new .py modules added to server.py must be added to the Dockerfile COPY line to prevent similar post-deploy 502s.

### Documentation & Housekeeping
- Version bumped to 1.4.1.
- CHANGELOG, new RELEASE_v1.4.1.md, README, AGENTS.md, VERSION, docs/GITHUB_RELEASES.md, docs/UPDATE_AND_DEPLOY.txt updated.
- The hotfix was applied on the production droplet via local Dockerfile edit + rebuild (as the tag was already cut), then the source Dockerfile was corrected and released.

See also [docs/RELEASE_v1.4.1.md](./docs/RELEASE_v1.4.1.md) for the full GitHub release text.

## [1.4.0] — 2026-06-08

Focus on stabilizing and polishing the local kanban tile UX and the Team/Presence demo indicators + inbox read/unread cues (from extensive live testing session).

### Local Kanban fixes
- Moved editable board name up into the standard tile title bar (toolbar) like other tiles (notes/groups); removed internal name input/duplicate; now uses dataset.tileLabel + dblclick contentEditable on .tile-toolbar-title with persist + no more .tile-toolbar-title hide.
- Column edit (✎) button now left of × (delete).
- Stage column color picker now visibly updates header via `<span class="column-dot">` (always shown, not just card borders or when populated).
- Fixed "add task" inline input crash: `Uncaught NotFoundError: removeChild` (node no longer child, possibly moved in blur). Used `let submitted`, `setTimeout(0, cleanup)` + guards in blur/keydown paths for +status/+task.
- Scrapped broken column drag/reorder (draggable, 'col:' data, makeReorderDrop, body drop col handling) per request (it never worked reliably, bubbled etc.).
- Edit button for columns now includes ◀ / ▶ slide buttons to move stage left/right (array splice + rebuild + re-open edit UI).

### Presence / Team demo indicator + inbox cues
- Added (and made persistent for every new user/session) client-side demo message with concise team presence/messaging explanation (no admin mention): "Team presence shows online users (green dot + count). Click the Team button to view roster and switch to Messages tab for DMs with replies and read status."
- Blue unread bubble (top-right on Team) now reliably appears/stays on reload (sync force in ensurePresenceOnLogin + re-assert in every badge update) until user explicitly clicks the demo in inbox.
- Introduced `demoMessageReadThisSession` flag (per page load) + `!demoMessageReadThisSession` guard for forcing blue "1" and header update. Decoupled from lastRead/ts to stop on/off flashing.
- On close of team popup: header indicators refreshed so count reflects only un-clicked messages.
- Messages inbox now has clear read/unread indication via dark/light shading: `.unread` gets surface-2 + blue left border accent + blue ● dot; `.read` gets opacity fade + muted text. Demo row specially forced by session flag (always "unread" visually until clicked, with "just now (demo)" label to avoid bad timestamps).
- Demo injection always ensures example msg in inbox list (re-added after server overwrites) but respects the read flag for count + shading.
- All updates to docs, new RELEASE, session chat log.

### Documentation & Housekeeping
- Version bumped to 1.4.0.
- CHANGELOG, new RELEASE_v1.4.md, README, AGENTS.md, VERSION, GITHUB_RELEASES.md, UPDATE_AND_DEPLOY.txt, docs/SESSION_2026-06-07.md (chat history + date stamp) updated.
- Local kanban + presence live fixes from verbatim user reports fully documented.

See also [docs/RELEASE_v1.4.md](./docs/RELEASE_v1.4.md) for the full GitHub release text.

## [1.3.0] — 2026-06-07

Major live-testing follow-up focused on the new Team / Presence feature (user status, basic DMs, inbox, admin tools) plus UI polish for the modal.

### Presence / Team (new feature, completed from user verbatim list)
- Header button (users icon, secondary style, left of New Tile) opens popup instantly (early bind + no blocking await on CRM loadPortalUsers; immediate render from cache + background refresh).
- Roster: users from CRM /people (same list as notify), online (green dot + heartbeat <10m), idle (>2h amber), offline (dark), "last seen X" via formatTimeAgo for offline.
- Status: dropdown (not buttons) with "Online" default + 3 templates ("Estimating - Do Not Disturb", "AFK - Lunch", "AFK - Pesky In-laws") + Custom... (emoji support, 120 char limit). "Status:" label to left of dark-mode select. Persisted server-side.
- DMs: click any user (online or offline) to open thread; send/receive works to offline users (delivered on next login). Basic history shown.
- Inbox / message tracking: "Messages" tab (separate from Team roster) shows recent DMs (from myRecentDms) with snippets, timestamps, click-to-thread, and × clear/archive per conversation. Thread also has Clear button. Backend uses per-convo JSON files under data/presence-messages/.
- Read receipts: "read" indicator appears on your sent messages once recipient opens the thread.
- Reply to message: click any prior message in thread to reply; quoted context shown in smaller font (with "Replying to:") above your text in the bubble (Signal/WhatsApp style). reply_to + reply_text carried in messages.
- Emoji selector: 😊 button in DM input opens compact picker; inserts at cursor. Messages support Unicode emojis.
- Color coding: received messages always green bubble; your sent messages use current accent color. Per-direction (1:1 DMs).
- Self-online indicator on header button: green dot (`.is-online::before`) even when alone (separate from count badge). Red pulsing flash (`.has-waiting-messages::after`) for unread DMs (separate from online count).
- Admin section (kenc@vanguardadj.com only): shows last CRM/dashboard activity minutes. Hidden by default with "Admin" toggle button in actions bar; button only visible on Team tab (not Messages tab); "Admin" text when hidden.
- Popup polish: taller messaging areas (.presence-list and .presence-dm now 480px max), message viewing area (#presence-dm-log) has min-height + larger font for more history visible at once. Entire popup window is now user-resizable (resize: both + min/max on #presence-modal .modal-card; only this modal affected).
- Reliability: direct presenceFetch (with X-OnlyOffice-Portal header) so /api/presence/* routes (users, snapshot, status, dm get/post, heartbeat, clear) are hit instead of proxy fallback. Routing hardened with early startswith checks + explicit 404 for unknown presence paths. No more generic "Failed" / 404s once server restarted.
- (Note: one remaining live bug with reply context not always rendering in sent history bubbles is paused per explicit request; core reply send/receive + preview + embedded text works.)

### Documentation & Housekeeping
- Version bumped to 1.3.0.
- CHANGELOG, new RELEASE_v1.3.md, README, AGENTS.md, VERSION, GITHUB_RELEASES.md updated.
- All presence-related live feedback (instant button, no errors, default Online, Status: label + dark select, online/offline + last seen, full inbox with delete, indicators, admin hidden+tab-only, taller/resizable popup, replies+emojis+colors+reads) documented.
- Reply context display bug in chat history explicitly noted as paused.

Full details in conversation history and source (public/app.js renderPresence* + openPresenceDMThread, presence_store.py, server.py handlers, styles.css presence rules).

See also [docs/RELEASE_v1.3.md](./docs/RELEASE_v1.3.md) for the full GitHub release text.

## [1.2.0] — 2026-06-06

Post-v1.1.0 testing follow-up release. All items from the explicit post-deploy list plus live feedback (collapse behavior, keyword semantics, template UX, tasks list modal, crash banner polish, styling, icon, docs) were implemented, tested, and shipped.

### Presence / Team follow-ups (from live session feedback)
- Presence button now opens instantly (removed blocking CRM `loadPortalUsers` await; immediate render from cache + background refresh).
- DM "click user", send, and status change no longer error ("Could not load messages" / "unable to send" / "could not set status"). Root cause: direct presence fetches now send `X-OnlyOffice-Portal` header (via new `presenceFetch` helper) so server resolves portal the same as profile/proxy calls.
- Default status "Online" (picker pre-selects it; list suppresses the literal "Online" text for roster cleanliness).
- "Status:" label to left of dark-mode `<select>` (uses `--surface-2` etc); sections "Online (n)" / divider / "Offline (n)" + "last seen X" for offline rows already wired.
- Inbox / message tracking: modal now shows "Recent messages" section (from `myRecentDms` in snapshot) with per-convo snippets, timestamps, click-to-open thread (works for offline users), and × clear per row. Thread view also has "Clear" button. Backend: `clear_conversation`, DELETE `/api/presence/dm?with=...` wired + store unlink.
- Self-online indicator: green dot on the header presence button icon (`.is-online::before`) driven by snapshot; visible even when you are the only one (separate from count badge and red "waiting messages" flash).

### Fixes & Polish
- **Tile collapse & minimize (notes, calendar, groups)**: Notes and calendar tiles now properly collapse (body content hidden while toolbar remains); added CSS overrides to defeat tile-specific min-heights. Calendar double-height now scales the month grid vertically (body flex + grid + day min-heights). CRM group tiles support half/quarter width while collapsed (minimized bar stays narrow).
- **Group removal persistence**: Removing a group tile now calls `saveUserProfileToServer({quiet:true})` immediately so the deletion survives quick reloads.
- **Tasks rows**: Description is now shown (`.task-desc`, truncated in compact rows) alongside title, using the same patterns as other task rendering.
- **CRM notifications keyword filter**: Switched from OR to strict **AND** semantics for comma-separated keywords. Input is split on `,`, trimmed; a notification matches only if *every* token is present in the blob (`tokens.every`). Single keyword continues to work. Placeholder text updated to reflect inclusive/AND behavior.
- **Preview modal persistence**: Launching "edit" from the opportunity preview no longer auto-closes the preview; after save the preview data is refreshed in place.
- **Favicon / logos**: Updated to the white ship variant for better contrast and visibility across themes.
- **Linked email errors**: "Message ... wasn't found" cases in opportunity preview now show a friendly message instead of the raw error.
- **Tasks list modal styling**: Light backgrounds (body #ffffff, rows #f8f9fa with hover, done state dimmed + strikethrough), high-contrast text (#212529), muted meta, explicit header contrast for readability. Accent color on checkboxes.
- **CRM backend crash / 5xx banner**: Fully implemented. `api()` wrapper detects `!res.ok && status >= 500` (or JSON parse failure) on paths containing `/crm/` and calls `showCrmCrashNotification()`. Banner uses the exact requested wording: "OnlyOffice CRM backend error (e.g. 502 on history). Dashboard may be out of date. Refresh page now — recommended in ~30 seconds." Includes "Refresh page now" button (location.reload()). Throttled (~15s), role=alert, sticky. Any successful CRM-path response auto-clears it. Testable by DevTools Network throttling or blocking /api/proxy/api/2.0/crm/* .

### Features / Enhancements
- **Full tasks list modal**: New discreet button (minimalistic light file-cabinet SVG icon) placed in the tasks panel header (flex layout, left of the user filter). Opens `#tasks-list-modal`. 
  - Fetches open tasks (`isClosed=false`) + completed (`isClosed=true`) and merges.
  - "Show completed" toggle re-renders the filtered set.
  - Each row: checkbox (close/reopen via POST /close or /reopen with optimistic update + re-render), title (deep link via existing `crmTaskUrl`), due date, responsible person.
  - "New Task" footer button opens the existing create-task modal.
  - Matches existing task row/checkbox/close patterns; reuses profile schedule + api wrapper.
- **Group templates — delete only**: Replaced the prior "manage/edit + delete via prompts" with a clean delete-only experience. New `openTemplateDeleteModal()` lists every saved template across all groups; each entry has an × button. Click → confirm → remove from the group's templates array → `saveUserProfileToServer` → refresh all group template `<select>` elements and the list itself. Toolbar button on groups updated to open the delete modal. No rename/edit paths remain.
- **Tasks list icon**: Final icon is a minimal, light, stroke-based file-cabinet SVG (after iterations from emoji); placed inside the header construction for the new button (`id="tasks-list-btn"`).

### Documentation & Housekeeping
- **AGENTS.md** (new root file): Persistent project context loaded automatically. Covers architecture summary, key reuse patterns (profile, api(), tile renderers, modals, attachTileCollapseButton, etc.), the exact post-1.1 user list, run/test/deploy commands, and instructions to prefer `/load` or welcome picker + `cd` first instead of full context dumps on every session.
- Removed the entire FEAT-009 (CRM CRASH notification) section from FUTURE_FEATURES.md (the banner was already implemented and shipped; kept only the implementation recap in conversation for testing guidance).
- CHANGELOG, RELEASE notes, README, GITHUB_RELEASES.md, AGENTS.md, and VERSION updated for v1.2.0.
- AccuLynx API research (FEAT-008) remains in FUTURE_FEATURES.md under "Other ideas" (added earlier per request; not implemented).

Custom fields (ISSUE-001) research artifacts from pickup remain disabled and are not part of this release. See AGENTS.md and FUTURE_FEATURES.md for current roadmap.

See also [docs/RELEASE_v1.2.md](./docs/RELEASE_v1.2.md) for the full GitHub release text.

## [1.1.0] — 2026-06-05

See also [docs/RELEASE_v1.1.md](./docs/RELEASE_v1.1.md) for the full GitHub release text.

### Fixes
- CRM notifications feed empty after pagination refactor — restored bulk history + mail loading.
- Feed no longer shows “no events” while mail/history is still loading.
- Hidden notifications: server-backed entries with snapshots; restore via toolbar modal.
- Archived notes tiles no longer auto-deleted from server profile.
- Removed non-working archived-notes UI from **Add tile** (moved to notes **File** menu).
- Group profile saves omit embedded opportunity lists (refetch on expand).

### CRM notifications (new / improved)
- **90-day** feed window (was 30).
- **200-event** load cap.
- Loading spinner in feed header; scroll to load more (until cap).
- 5-minute in-session feed cache.
- Hidden-notifications manager (hide per item, review/restore, 30-day retention).

### Notes tiles (new / improved)
- Server persistence per user; archive retained on server.
- **File** menu: `.txt` / `.md` export, archive, duplicate, **restore from archive** (by date → fills current note).
- Presets: **Daily**, **Claim checklist** (CRM checkbox fields).
- Quarter width; Edit/Preview; icon toolbar; save footer (time + stats).
- Removed link-deal from notes; **Daily Standup** renamed to **Daily**.

### Dashboard performance
- Minimized tiles skip CRM fetches until expanded.
- Parallel login bootstrap; deferred per-card custom-field enrichment (`IntersectionObserver`).

### UI
- **Red X** remove icon (groups, calendar, notes).
- **Save template** floppy-disk icon on groups.
- Minimize + icon-based layout controls on all tiles.
- Kanban card **preview** → opportunity preview modal.

---

## [1.0.0] — 2026-06-05

First production-ready release on GitHub.

### Core dashboard
- OnlyOffice CRM kanban with opportunity groups (stage/tag filters, templates, red-deal filter).
- Pinned **CRM notifications** and **Tasks** panels with layout controls.
- Global opportunity search in the header.
- Create opportunity and quick-note modals; deal edit from cards.

### Tiles
- **Add tile**: opportunity groups, calendar (ICS feed), notes.
- Per-user dashboard profile on server (`/api/user-profile`): groups, layout, calendars, notes, feed keyword, hidden feed keys.
- Calendar monthly view with timezone selector and event detail modal.

### Deploy & ops
- Docker Compose deploy to DigitalOcean (`dashboard.vanguardadj.com`).
- GitHub Actions deploy workflow; production server notes and estimate-nginx network in compose.
- Documentation: `DEPLOY.md`, `docs/UPDATE_AND_DEPLOY.txt`, `docs/PRODUCTION_SERVER_NOTES.txt`.## [Unreleased]

### Fixed
- `server.py` `do_POST` no longer crashes with `AttributeError: 'super' object has no attribute 'do_POST'` on non-API POST requests; now returns HTTP 405.

