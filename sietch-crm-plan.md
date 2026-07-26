# Sietch CRM v3 Migration Plan

**Branch:** `new-crm` (pushed to `git@github.com:kachapman/sietch-crm.git`)  
**Target:** Fully replace the OnlyOffice CRM dependency with a self-contained PostgreSQL-backed Sietch CRM, then cut over from `main`.

This document is the single source of truth for phases, progress, and open decisions. It is updated after every commit.

---

## Decisions already confirmed

| Question | Answer |
|----------|--------|
| Plan file name | `sietch-crm-plan.md` (this file) |
| Branch | `new-crm` until cutover |
| Card title click | Opens the preview modal. No CRM deep-link needed (we are migrating away from OnlyOffice CRM). |
| Export format | One JSON file per entity type (`contacts.json`, `opportunities.json`, `stages.json`, `tags.json`, `tasks.json`, `history.json`, `files.json`). Easier to inspect, transfer, and retry partial imports. |
| File attachments in export | Metadata + URLs by default. Downloads can be fetched during import if still reachable. |
| Hourly sync | Bidirectional during the transition period. |
| Document handling | OnlyOffice Document Server (already integrated) for future document viewing/editing. |
| sortablejs | CDN `<script>` tag (keep vanilla JS). |
| Profile pictures | Phase 2G. |
| Calendar WebDAV | Deferred. |
| Email classifier | Deferred until after deployment. Branch: `email_scanner`. |
| Testing | Automatic tests during local test-server deployment preferred; manual testing also acceptable. |

---

## Non-negotiables (CRITICAL - do not violate)

- **Preserve all current CRM data and layout as if we are still using the OnlyOffice API.** 
  - Same note types (history_categories), opportunities, note history (with embedded emails in history events), contacts, tasks, stages, tags, custom/user fields, etc.
  - When in doubt, refer to last known production version (main branch) details for what the user is displaying and using in deal tiles, preview modals, feed, etc.
  - All links, data, and functionality must continue to work.
- **Do not change anything that does not explicitly need to be changed.**
- **Implement/replace all OO API dependent functionality** with local DB equivalents (v2 API, etc.) without breaking existing data or UI.
- Example: Implement a contacts section/tile/admin support so old contacts data can be imported and all contact-based functionality (in deals, customer bot modal, previews, etc.) still functions.
- **Focus first on making the CRM fully functional with the new DB and UI changes.** Move data sync / full import to later phase.
- **Git version and update docs after every significant change** so context is not lost.
- Preserve exact hover behavior on deal tiles (mouse over lights up borders) from production.

---

## Current state

- `new-crm` is the working branch.
- The dashboard runs standalone with PostgreSQL (sietch_crm), native auth, v2 API, Document Server.
- One-time import of OnlyOffice data completed (1191 opps, 38898 history, 11 users, 16 contacts, 18 stages, 21 tags defs). Data KEPT (no wipe). Tags/tasks per-opp not transferred in bulk export; will be addressed via future read-only enrich/sync (not blocking functionality).
- Focus: make Sietch fully functional standalone first (create/edit deals, no reliance on OO for core). Import diagnosis deprioritized; move through phases per plan, revisit import/sync later.
- Bot account and other admins with `is_admin` in the DB work for login.
- Admin UIs (branding etc) auto-available to isAdmin users (no extra login).
- Local run: use venv + DB_HOST=127.0.0.1 if needed; server binds 0.0.0.0 and prints LAN URLs.

---

## Migration strategy

**Option A — API migration via bot credentials.**

1. Export all CRM data via the OnlyOffice API using the bot credentials on the CRM droplet.
2. Transfer the exported JSON files to the dashboard droplet.
3. Import the JSON into local PostgreSQL.
4. Run `migrate_dashboard_data.py` to remap local profile data (notes, tiles, etc.) to the new PostgreSQL user IDs.
5. During the transition period, run a **hourly bidirectional sync** with OnlyOffice to keep both systems in sync.
6. Once the dashboard is fully self-contained and verified, stop the sync and decommission the OnlyOffice CRM dependency.

---

## Phases

### Phase 1: Foundation + Core CRM (mostly complete)

| Sub-phase | Status | Notes |
|-----------|--------|-------|
| 1A: Infrastructure | ✅ | `init.sql`, `db.py`, `docker-compose.yml`, `Dockerfile`, `.env`/`config.example.env` all created. |
| 1B: Auth system | ✅ | `auth.py`, `smtp_client.py`, login/logout/reset endpoints, session cookies. |
| 1C: Migration script | ✅ | `migrate_from_onlyoffice.py` created. Needs `--export-only` mode for Phase 2. |
| 1D: Core API endpoints | ✅ | `server.py` rewritten to direct DB queries. |
| 1E: Frontend API path swaps | ✅ | `app.js` API paths swapped. |
| 1F: Threaded replies | ✅ | API endpoints and UI exist. |
| 1G: Bot + Presence | ✅ | Bot and presence adapted to local DB. Telegram notification dispatch exists. |

### Phase 1 follow-up fixes (done)

Goal: Fix the remaining UI/JS bugs so the dashboard is usable against the local v2 API. (Additional fixes applied to ensure create/edit works with imported data.)

- [x] **Kanban display:** ...
- [x] **JS `localeCompare` crashes:** All calls (including missed Map entry sorts in user selects for create/edit/notify/task filters) now use `String(...)` coercion defensively.
- [x] **Card title interaction:** ...
- [x] **Create/edit fixes:** Server create now accepts stageType (no longer hardcodes 0). JS user selects fixed to prevent a[1].localeCompare errors during open/create/edit modals. 
- [x] **Branding save:** ...
- [x] **Team tile active-user filter:** ...

### Phase 2: UI Enhancements + Features

| Sub-phase | Status | Notes |
|-----------|--------|-------|
| 2A: Search modal expansion | ✅ | Full-text search (title, description, contact, custom fields), custom/user field filters, tag filter merged into Projects tab, sort options, server-side pagination (50/page), "+ Tab" adds preview in background, "Open in CRM" links removed, softened title/checkbox contrast. Original scope (stage/owner filters, batch ops, rich results, select-all, row-click preview) already shipped. |
| 2B: Project card click behavior | ✅ | Cards click → preview (side/full) or edit; Phase 1 follow-up completed. |
| 2C: Unified Admin Modal | ✅ | Vertical sidebar tabs (overview/sync/users/stages/custom-fields/contacts/tags/branding/bot/logs); custom fields read-only, tags add, contacts/stages add+search, sync stubs; icons per non-neg. Projects managed via search modal (filters + batch ops). |
| 2D: Tile layout refactoring | ✅ | CSS grid (spans, double-height, responsive), SortableJS drag-drop + ghost/chosen/drag classes + layout buttons; smooth collapse animation; terminal theme on data panes + admin modal; hover glow. |
| 2E: Photo gallery | ✅ | Photos tab in preview modal with upload, thumbnail grid, quota display, delete, and lightbox. `POST /api/v2/projects/{id}/photos`, `GET /api/v2/photos/{id}` + `?thumbnail=1`, Pillow thumbnail + EXIF extraction. |
| 2F: Notification drawer | 🔲 | Feed tile + keyword filter exists; no slide-out drawer with inline replies. |
| 2G: User profile + notification overhaul | 🔲 | Profile modal (avatar, name, password, notification prefs), @-mention system replacing multi-select notify, feed tile shows only @-mentions + task assignments, lightweight project.html page for mobile Telegram links, notification dispatcher wired to preferences. |
| 2H: Documents modal | ✅ | Full file manager: three scopes (project/personal/company), nested folders in personal/company, breadcrumb navigation, New button (Word/Excel/Folder), folder context menu (rename/delete with recursive CTE), inline rename, move/copy popup, batch ops, search, icon-only toolbar, sidebar toggle, drag-drop upload. Document Server used as editor with title-bar rename sync. |
| 2I: Preview modal + tile revamp | ✅ | Description→top, "Project Fields" merged, Stage dropdown, "Follow-up Due Date", interactive Tags (add/remove), Checklist 3-col checkboxes, kanban created date + native tooltip. Inline project-fields editor added. Browser verified. |
| 2J: Re-import CRM data | 🔲 | Re-run `migrate_from_onlyoffice.py` export script (with tasks and user/custom fields fixed) then import into new CRM to verify all data displays correctly in preview modals, deal tiles, kanban stages, tags, and custom fields. |

### Phase 2A Details: Search Modal Expansion (FEAT-007 Phase D)

#### Goal
Expand the search modal from a simple title-search popup into a full filterable project directory that matches the OnlyOffice CRM search experience: full-text search across deal and user fields, custom-field filters, tag filtering, sort options, and paginated results.

#### Backend changes
- Extend `GET /api/v2/projects` so `filterValue` searches across `title`, `description`, `contact` (first/last/company), and all custom-field values using `ILIKE '%q%'`.
- Add `customFieldFilters` JSON array parameter: `[{ fieldId, value, operator }]` where `operator` is `equals` or `contains`.
- Add `tagId` parameter for server-side tag filtering.
- Extend `sort_by` to support `date_created`, `title`, `bid_value`, and `stage`.
- Change default `count` to 50 for search-modal queries; add `startIndex` pagination.
- Add `GET /api/v2/projects/count` returning `{ count: N }` for the same filters, used for pagination.
- Add trigram (GIN) indexes on `opportunities.title`, `opportunities.description`, `contacts.first_name`, `contacts.last_name`, `contacts.company`, and `opportunity_custom_field_values.field_value` for fast `ILIKE` queries.

#### Frontend changes
- Open the search modal to the first page of open projects (50/page) instead of an empty state.
- Search input uses debounced (300 ms) `input` events; empty search returns all open projects.
- Merge the Tags tab into the Projects tab as a single-select tag filter.
- Add sort dropdown (newest, oldest, title A/Z, bid high/low, stage A/Z).
- Add dynamic custom-field filter rows: field select + type-specific value control + remove button.
- Move all filters (search, stage, owner, tag, sort, custom fields) to the server.
- Add pagination controls (Prev / Page N of M / Next).
- "+ Tab" button adds a preview tab in the background without switching; row click opens and switches.
- Upgrade the dashboard header search to use the same full-text search.

#### Files
- `server.py`
- `init.sql`
- `public/index.html`
- `public/app.js`
- `public/styles.css`
- `CHANGELOG.md`, `AGENTS.md`, `FUTURE_FEATURES.md`

---

### Phase 2I Details: Preview Modal + Kanban Tile Revamp

#### Goal
Since we are no longer bound by OnlyOffice CRM field structures, redesign the opportunity preview modal and kanban deal tiles to be more usable and informative. All changes are UI-only; backend and data model remain unchanged.

#### Preview Modal Changes

**Layout restructuring:**
- Move **Description** to the top of the modal, above "Deal Fields" section
- Rename **"Deal Fields"** → **"Project Fields"**
- Remove the separate **"User Fields"** section; merge all user-defined fields into the Project Fields section
- Make all user fields display in **2-column grid** (instead of current 1-column) to better use available modal width
- Make the **Stage** field visually distinct — styled as a dropdown/pulldown menu that allows changing the stage directly from the preview modal
- Rename **"Expected Close"** → **"Follow-up Due Date"**

**Tags in preview modal:**
- Add ability to **add** new tags from inside the preview modal (inline tag input + add button)
- Add ability to **delete/remove** existing tags from inside the preview modal (× button per tag chip)

**Specialty checkboxes (3-column layout):**
- Make the three specialty field checkboxes — **Measurement Report**, **Insurance Documents**, **Inspection Photos** — display as **3 columns side by side** (inline, not stacked)

#### Kanban Deal Tile Changes

**Due date area (right of "Due [date]"):**
- Show the **created date** of the deal to the right of the "Due [date]" line in the deal tile
- Format: small, subtle text (e.g., "Created: Jul 15")

**Discrete tooltip:**
- Add a **discrete tooltip** trigger on each deal tile (distinct from the red "due date" styling)
- The discrete tooltip shows additional info without alarming color coding (neutral gray styling, not red)

#### Implementation Notes
- All field labels and section headers are UI strings only — no database column renaming
- Stage change from preview modal should call `PATCH /api/v2/projects/{id}` (or equivalent) to update the stage
- Tag add/remove from preview modal should call the appropriate history event API to record the tag change

### Phase 2H Details: Documents Modal

#### Goal
A unified Documents experience across three scopes — **per-project**, **personal (per user)**, and **company common** — with batch operations, search across all project documents, and an overhauled in-project file manager. The Document Server is used purely as an embedded editor; it has no file management API of its own.

#### Data Model

**Table: `project_documents`** — add columns via migrations:

```sql
-- Migration 1 (2026-07-20): scope + notes
ALTER TABLE project_documents
  ADD COLUMN company_scope BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN notes TEXT;
CREATE INDEX idx_documents_company ON project_documents(company_scope) WHERE company_scope = TRUE;

-- Migration 2 (2026-07-20): folder support
CREATE TABLE document_folders (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES document_folders(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'company')),
    uploaded_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE
);
ALTER TABLE project_documents ADD COLUMN folder_id INTEGER REFERENCES document_folders(id);
```

**Scope derived from columns:**
- `opportunity_id IS NOT NULL` → **project document** (belongs to that project)
- `uploaded_by IS NOT NULL AND opportunity_id IS NULL AND company_scope = FALSE` → **personal document**
- `company_scope = TRUE` → **company document**

**Folders:** Only personal and company scopes support nested folders. Folders use self-referencing `parent_id`. Deleting a folder recursively soft-deletes all subfolders and their documents via CTE.

**File storage paths:**
- Project: `DOCUMENT_STORAGE_PATH / shared / project / {opp_id} / {filename}`
- Personal: `DOCUMENT_STORAGE_PATH / shared / personal / {uploaded_by} / {filename}`
- Company: `DOCUMENT_STORAGE_PATH / shared / company / {filename}`

#### Backend API

**New endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v2/documents/personal?folder_id=` | List current user's personal docs + folders in current level |
| `GET` | `/api/v2/documents/company?folder_id=` | List all company-shared docs + folders in current level |
| `GET` | `/api/v2/documents/search?q=&project_id=` | Search all project docs; `project_id` optional; results grouped by project |
| `GET` | `/api/v2/documents/folders?scope=&folder_id=` | List folders at a given level |
| `POST` | `/api/v2/documents/folders` | Create folder: `{name, scope, parent_id?}` |
| `PATCH` | `/api/v2/documents/folders/{id}` | Rename folder: `{name}` |
| `DELETE` | `/api/v2/documents/folders/{id}` | Delete folder recursively (subfolders + documents) |
| `POST` | `/api/v2/documents/create` | Create blank doc: `{type: "word"|"excel", title, scope, folder_id?, opportunity_id?}` |
| `PATCH` | `/api/v2/documents/{id}` | Rename (`{title, notes}`) or move to project (`{opportunity_id}`) |
| `POST` | `/api/v2/documents/{id}/copy` | Copy doc; body: `{opportunity_id?, company_scope?}` (scope determined by which param is set) |
| `POST` | `/api/v2/documents/batch-delete` | Batch soft-delete; body: `{ids: []}` |
| `POST` | `/api/v2/documents/batch-move` | Batch move; body: `{ids: [], opportunity_id}` |
| `POST` | `/api/v2/documents/batch-copy` | Batch copy; body: `{ids: [], opportunity_id?, company_scope?}` |
| `GET` | `/api/v2/projects/simple` | Lightweight project list for picker (id, title, stage) — recent 20 |

**Keep existing:** download, editor-config (with `permissions.rename` + `onMetaChange` for title sync), per-project list/upload, single delete.

**Permissions:**
- Delete: own doc OR admin → can delete; company doc by non-owner → 403
- Rename/Move: own doc OR admin → can modify
- Copy: any authenticated user
- Upload to company: any authenticated user

#### Frontend: Documents Modal

**Trigger:** Header button with files icon (`icon-tabler-files`), next to email inbox button.

**Modal layout** (Google Drive / OneDrive inspired, 900px wide, 80vh tall):

```
┌─────────────────────────────────────────────────────────────┐
│  Documents                              [Search...🔍]  [×] │
├──────────────┬──────────────────────────────────────────────┤
│ SCOPES      │  [Upload]  [Delete]  [Move]  [Copy]           │
│              │  ──────────────────────────────────────────    │
│ ○ Projects >│  ☐ 📄 estimate_v2.docx  Project A  2MB  Jul 15 │
│   Project A │  ☐ 📄 photo.jpg         Project A  340KB Jul 12 │
│   Project B │  ☐ 📄 claim-form.pdf    Project B  120KB Jul 10 │
│   ...       │  ...                                            │
│ ○ My Docs   │                                                 │
│ ○ Company   │                                                 │
└──────────────┴──────────────────────────────────────────────┘
```

**Scope sidebar (200px, left):**
- **Projects** (expandable): shows 5 most-recent projects; clicking a project shows its docs in main area; "Search all projects" at bottom → activates search mode
- **My Docs**: flat list of current user's personal docs with nested folder navigation
- **Company**: flat list of company-shared docs with nested folder navigation

**Main area toolbar:**
`[Upload]  [New ▾]  [Actions ▾]  [Sort]  [Rename]  "N selected"  [Clear]`

**New button dropdown:** Word Document · Excel Spreadsheet · New Folder. Creates in current folder; documents open in OnlyOffice editor immediately.

**Folder navigation:**
- Folder rows render with folder icon + name; click to navigate into
- Breadcrumb trail above file list: `My Documents / Invoices / Q3` — click any segment to navigate
- Context menu on folders: Rename folder, Delete folder (recursive with confirmation)

**List view columns:** `☐` checkbox, icon, title, size, modified date (hover for uploader)

**Right-click context menu:** Open in editor · Download · Rename · Move to Project... · Copy to... · Delete

**Project picker modal** (for Move/Copy): searchable list of all projects, recent 5 pinned at top, then alphabetical. Shows project name + stage.

**Upload:** drag-and-drop zone overlay (highlight entire modal on dragover) + "+ Upload" button opens file picker. Per-file progress bar.

**Search mode** (Projects → Search all, or typing in global search): results grouped under each project header, collapsible. Project headers are bold section titles.

**Empty states:**
- Project (no docs): "No documents in this project. Upload files or drag them here."
- My Docs (empty): "No personal documents yet. Upload files or copy from a project."
- Company (empty): "No company documents yet. Upload shared resources here."
- Search (no results): "No documents match your search."

#### Frontend: Overhaul Existing Documents Tab

The Documents tab inside the opportunity preview modal gets the same file manager list UI:
- Same list columns: checkbox, icon, title, size, date
- Same batch toolbar when items selected
- Upload button visible
- "Open in editor" link per document
- Right-click context menu
- Search within project (simple filter input)

#### UI/UX Patterns (per modern file manager research)

- **Selection**: Checkbox on hover, Shift+click range, Ctrl+click toggle
- **Batch toolbar**: Sticky top bar appears when items selected, shows count
- **Context menu**: Right-click on row (or "..." button for touch/accessibility)
- **Drag & drop**: Upload by dragging files onto the modal
- **List columns**: Name, Project (in search/all scopes), Size, Modified, Owner
- **Sort**: Click column headers, asc/desc toggle
- **Loading**: Skeleton rows on initial load and during operations
- **Errors**: Toast notifications for success, inline errors for failures

#### Implementation Order

1. DB migration (add columns + indexes)
2. Backend: personal list, company list, rename, batch-delete, batch-move, batch-copy, search (grouped), project/simple endpoint
3. Header button (files icon) + Documents modal shell (open/close, scope sidebar, basic list render)
4. My Docs list + upload + delete
5. Company Docs list + upload + delete
6. Project Docs list in modal + search-all mode
7. Batch operations: Delete, Move, Copy
8. Context menu
9. Overhaul existing Documents tab in project preview modal (same list UI + batch ops)

### Phase 2G Details: User Profile Modal + Notification System Overhaul

#### Overview

A. Profile modal (avatar, name/email, password change, notification prefs)
B. Profile picture upload with thumbnail generation
C. @-mention autocomplete replacing old `<select multiple>` notify list
D. Feed tile shows only @-mentioned + task-assigned items (not all events)
E. Notification preferences wired to dispatcher (dormant until system active)
F. Lightweight `project.html` page for mobile Telegram notification links

#### Part A — Profile Modal

**Header button:** `#profile-btn` in `.hero-header-actions`, left of sign-out. Uses `icon-tabler-user-square-rounded` SVG. Always visible when logged in (not admin-only).

**Modal sections:**
1. Avatar circle (initials fallback, or uploaded image) + display name + email
2. Contact details form: Display Name, First Name, Last Name, Email (read-only)
3. Change Password: Current + New + Confirm fields
4. Notification Preferences: In-dashboard toggle, Telegram toggle (dormant)

**Backend endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `GET /api/v2/me` | Fetch current user | Extend to include `avatarUrl` |
| `PUT /api/v2/me` | Update own profile | New — display_name, first_name, last_name |
| `POST /api/v2/my/avatar` | Upload profile picture | New — multipart, Pillow 200x200 thumbnail |
| `GET /api/v2/my/avatar` | Serve avatar | New — `?thumbnail=1` for 64x64 |
| `DELETE /api/v2/my/avatar` | Remove avatar | New — reverts to initials |
| `GET /api/v2/me/notification-prefs` | Fetch prefs | New — reads `notification_preferences` table |
| `PUT /api/v2/me/notification-prefs` | Save prefs | New — upserts `notification_preferences` table |
| `POST /api/v2/auth/change-password` | Change password | **Already exists** |

#### Part B — Avatar Upload

**DB migration:** `ALTER TABLE users ADD COLUMN avatar_url TEXT;`

**Storage:** `data/avatars/{user_id}/avatar.jpg` (200x200) + `thumb_avatar.jpg` (64x64)

**Handler reuses:** `_parse_multipart()`, Pillow thumbnail generation, `_json_response()`

#### Part C — @-Mention System

**Replaces:** `<select id="deal-edit-notify">`, `<select id="quick-note-notify">`, `populateNotifyUserSelect()` and wrappers, `#create-opp-notify` checkbox (dead code).

**How it works:**
1. User types `@` in note editor → dropdown appears at cursor
2. Typing after `@` filters `state.portalUsers` by name prefix
3. Selecting a user inserts a styled `@Username` chip (non-editable inline element)
4. On submit, `extractMentionsFromContent(html)` parses chips → user IDs
5. IDs sent as `notifyUserList` to `POST /api/v2/projects/{id}/history` (existing endpoint)

**No backend changes needed** — existing `history_notify_users` + DB triggers handle notification creation.

#### Part D — Feed Tile Overhaul

**Current:** Fetches ALL history events via `/api/v2/projects/0/history`, shows everything.

**New:** Fetches from `GET /api/v2/notifications` (existing endpoint), shows only:
- `note_tagged` events (user was @-mentioned)
- `task_assigned` events (user was assigned a task)

**Changes:**
- Replace `loadCrmRelationshipNotifyEventsBulk` with `GET /api/v2/notifications`
- Remove ~200 lines of old feed parsing functions
- Add mark-as-read on item click + mark-all-read button
- Add unread count badge

#### Part E — Notification Preferences

**Table:** `notification_preferences` (already exists, unused).

| Key | Default | Purpose |
|-----|---------|---------|
| `in_dashboard` | `true` | Show in feed tile |
| `telegram` | `true` | Send Telegram alerts |
| `email_digest` | `disabled` | Future: daily/immediate |

**Dispatcher integration:** Before sending Telegram message, check `notification_preferences.telegram` for recipient. If false, skip.

#### Part F — Lightweight Project Detail Page

**URL:** `/project/{id}` (serves `public/project.html`)
**Auth:** Requires session cookie (same as dashboard)
**Query params:** `?event={event_id}` to highlight specific event

**Content:** Full project details (title, stage, value, contact, due date, tags, custom fields) + event history. Mobile-optimized, no tile grid.

**Server route:** Serve static `project.html` for `/project/\d+` paths. Client-side JS reads the project ID from URL, fetches via existing API endpoints.

**Telegram message format:**
```
📋 Ken Chapman tagged you in a note on Smith Roofing Claim
"Inspection completed, photos uploaded..."
🔗 View project → {DASHBOARD_URL}/project/1042?event=5678
```

#### Implementation Order

1. DB migration (avatar_url column)
2. Backend endpoints (PUT /api/v2/me, avatar, notification prefs)
3. Profile modal HTML + CSS + JS
4. Header button + early binding
5. @-mention autocomplete component
6. Remove old notify multi-selects + dead code
7. Feed tile: switch to GET /api/v2/notifications
8. Remove old feed parsing functions
9. Lightweight project.html page + server route
10. Notification dispatcher: preference check + project link
11. CHANGELOG + AGENTS update

#### Files Modified

| File | Changes |
|------|---------|
| `init.sql` | `avatar_url` column migration |
| `server.py` | PUT /api/v2/me, avatar endpoints, notification prefs endpoints, /project/{id} route |
| `public/index.html` | #profile-btn header button, #profile-modal, mention dropdown, remove old notify selects |
| `public/app.js` | Profile modal JS, @-mention autocomplete, feed tile rewrite, remove old feed parsing |
| `public/styles.css` | Profile modal styles, avatar circle, mention dropdown/chips, project.html styles |
| `public/project.html` | New lightweight project detail page |
| `notification_dispatcher.py` | Preference check, project link in messages |
| `CHANGELOG.md` | Release notes |
| `AGENTS.md` | Session summary |

### Phase 3: Email + IMAP ✅ SCOPE DEFINED (awaiting start)

**Architecture:** Separate Docker container (`vanguard-mail-scanner`) running a Python IMAP daemon using `imap_tools`. Communicates with the dashboard via `POST /api/v2/mail/*` endpoints in `server.py`. Shares the `vanguard-internal` Docker network. Can be updated independently (separate compose file). Pulls the mature scanner daemon, classifier engine, and admin panel UI from the `email_scanner` branch (219 commits), adapting them for IMAP instead of CRM API calls.

**Scope decisions:**
- Both shared CRM inbox(es) + per-user inboxes (via `mail_accounts` table)
- Full email storage in DB (all modules/shutdown of OnlyOffice — self-contained)
- At least every 5 minutes polling (configurable)
- Attachments stored in DB as base64 (self-contained, no file system)
- Separate container so scanner/daemon/classifier can be updated independently
- Inboxes/settings accessed via modal (not sidebar) — aligns with existing scanner admin panel pattern
- Pagination on all mail API endpoints; UID watermark for incremental sync of large volumes
- Per-account/inbox filtering; email tagging; templates; drafts

- **3A: IMAP sync module.** Pull `mail_scanner.py` from `email_scanner` branch (classifier engine, action toggles, dedup logic). Replace CRM API mail calls with `imap_tools` IMAP fetch. Add pagination, UID watermark-based incremental sync for large volumes, `[#DEAL-ID]` subject auto-link regex, per-account folder filtering, attachment fetch/storage. New `mail_tags`, `mail_templates` tables in `init.sql`. Store full emails (body_html, body_text) in `mail_messages`.

- **3B: Scanner admin panel UI.** Pull `scanner/scanner_service.py` from `email_scanner` branch. Adapt for IMAP account management: add/edit/delete mail accounts, inbox configuration, per-inbox filtering, tag management, templates, drafts compose. Modal-based (not sidebar). Includes connection indicator, log viewer, reprocess endpoint. Also pull `scanner/docker-compose.scanner.example.yml`, `scanner/.env.example`, adapt for IMAP settings.

- **3C: Deal title tooltip + copy.** Small icon next to deal title in preview modal showing `project_number` as tooltip; click copies to clipboard. Users include `[#12345]` in email subject → auto-linked to that deal.

- **3D: Universal CRM inbox UI.** Modal showing all emails from shared CRM inbox(es) for record keeping/linking. Access limited to select users (admins + designated staff). Reuses existing inbox modal pattern in `app.js`.

**Not built yet:** ML classifier training (dry-run on `email_scanner` branch), OAuth2 (deferred), IMAP IDLE/push (polling only).

**Deferred until Sietch deployment is ready and OnlyOffice CRM mail module shutdown is scheduled.**

### Phase 4: Bidirectional Sync

- 4A: `sync_worker.py` background service, hourly sync.
- 4B: Sync monitor tab in admin modal.

**Status:** Tables (`sync_watermarks`, `opportunity_changes`) already exist in `init.sql`. Sync worker not yet implemented.

### Phase 5: Cutover + Decommission

- 5A: Beta deployment on `crm.publicadjustermidwest.com`.
- 5B: Gradual user migration.
- 5C: Archive OnlyOffice droplet, redirect dashboard domain, remove sync tables/code.

### Research item: OnlyOffice CRM import phase (for future sync/enrich)
- Consider still using the export script path (`migrate_from_onlyoffice.py --export-only` + `import_json_export.py`), but ensure **every deal is ID'd by its unique OnlyOffice number** (the `id` from the opp object, as shown in `https://office.publicadjustermidwest.com/Products/CRM/Deals.aspx?id=828`).
- Store the OO id (as `external_id` or similar column on `opportunities`) during import. Future API sync (Phase 4) can then reliably locate + match the correct deal inside Sietch by this stable id instead of title (e.g. "Storyboard on Ramada (Steve Krajczar)").
- Also address current export limitation: bulk opp export (via `/filter`) + import currently misses per-opportunity **tags** and **user/custom field values** (only global tag list and custom field *definitions* are exported). Extend export (parallel to how per-opp history is pulled) to capture full `tags` + `customFieldList` so they survive roundtrip or can be used in later sync.

---

## Files to create

| File | Purpose | Phase |
|------|---------|-------|
| `sietch-crm-plan.md` | This plan | ongoing |
| `import_json_export.py` | Import exported JSON into PostgreSQL | 2 (export tooling) |
| `sync_worker.py` | Hourly bidirectional sync | 4 |
| `scanner/mail_scanner.py` | IMAP sync daemon + classifier engine (pulled from email_scanner branch) | 3 |
| `scanner/scanner_service.py` | Scanner admin panel HTTP service (pulled from email_scanner branch) | 3 |
| `scanner/Dockerfile` | Scanner container image (adapted from email_scanner branch) | 3 |
| `scanner/docker-compose.scanner.yml` | Scanner compose file | 3 |
| `scanner/.env.example` | Scanner env template (IMAP, poll interval, fetch limit) | 3 |
| `init.sql` | Add `mail_tags`, `mail_tag_assignments`, `mail_templates` tables | 3 |

## Files to modify

| File | Changes |
|------|---------|
| `public/app.js` | Phase 1 follow-up: kanban fields, `localeCompare`, card title click, active-user filter. Phase 2I: preview modal restructuring (description first, stage dropdown, tag add/remove, specialty checkbox 3-col). Phase 3: inbox modal UI, pagination, tag filtering, template/draft compose, deal title tooltip+copy. |
| `server.py` | Phase 1 follow-up: move `POST /api/branding` to `_handle_api_post_put`. Phase 2C: admin handlers. Phase 2I: stage update endpoint for preview modal. Phase 3: `/api/v2/mail/*` endpoints (inbox fetch, link email, tags, templates, drafts). Phase 4: sync endpoints. |
| `public/index.html` | Phase 2C: Unified Admin Modal. Phase 2E: photo tab. Phase 2G: profile modal. Phase 2I: preview modal layout changes. Phase 3: inbox trigger (modal, not sidebar). |
| `public/styles.css` | Phase 2C: admin theme. Phase 2D: grid layout. Phase 2I: preview modal field grid, specialty checkbox 3-col, discrete tooltip styling. Phase 3: inbox modal styles. |
| `migrate_from_onlyoffice.py` | Phase 2: add `--export-only` mode. |
| `migrate_dashboard_data.py` | Phase 2: fix user ID mapping. |
| `AGENTS.md` | Updated after every session. |
| `CHANGELOG.md` | Updated after every release/fix. |

---

## Progress log

- 2026-07-20: Added Phase 2I: Preview modal + tile revamp (description→top, Project Fields, 2-col user fields, stage dropdown, Follow-up Due Date, tag add/remove, specialty 3-col, deal tile created date, discrete tooltip).
- 2026-07-19: Created `sietch-crm-plan.md` with confirmed decisions from chat history.
- 2026-07-18: `9bb823c` — CSV import and project-list fix.
- 2026-07-18: `7f28153` — Fix projects list stage/contact field indices and add CSV import script.
- 2026-07-18: `a2e8cb2` — Fix JS syntax error after `uploadAttachmentForNote` refactor.
- 2026-07-19: Phase 1 follow-up fixes committed (kanban fields, `localeCompare`, card title→preview, branding POST route, active-user filter). Verified locally.
- 2026-07-19: Started Phase 2 export tooling: `--export-only` + `import_json_export.py` skeleton added to `migrate_from_onlyoffice.py`. Fixed user_id=1 fallback in profile migration. Footer made static (bottom of content flow).
- Quick logo update: Replaced dashboard logos with new assets/sietch-logo-2-nobg*.png (nobg2 for pure logo in header/branding defaults; nobg1 for footers that had logo + name text beside it). Updated all references in HTML, server.py, init.sql, README. Progress: Phase 1 fixes complete (localeCompare, create/edit now functional). Issues encountered: import left tags/tasks incomplete (deprioritized per direction); multiple title matches possible for future enrich (will use external_id). Continuing to Phase 2C admin console expansion.
- Advanced 2C: normalized /api/v2/me to camelCase (consistent isAdmin etc); added live Overview (shows current user from session) + functional Users tab (lists all users read-only via /api/v2/users with admin badges). Sync tabs remain stubbed. Header buttons (mail, add-tile, bookmarks) + sign-out fixed with early listener attach + robust show/hide (part of making UI functional before deeper 2C tabs).
- Consulted plan: still in Phase 2 / 2C focus (admin tabs filling, contacts). Phase 1 + research item complete. Moving forward on 2C.
- 2C contacts tab: enhanced with live search, dynamic list from /api/v2/contacts, basic add contact form (uses existing POST). Read-only display for import preservation; supports func in deals/bot.
- 2026-07-18: `60d880b` — Add dashboard-local data migration tooling.
- 2026-07-18: `b7d091b` — Expose dashboard on `0.0.0.0` and DB on `127.0.0.1:5432`.
- 2026-07-18: `0fb82e3` — Fix local deployment for Podman.
- 2026-07-18: `4e1ecde` — Phase 1F/2E: Document Server integration.
- (Earlier commits: Phase 1A-1D foundation, Phase 1E API swap, Phase 1G bot/presence.)

---

## Next action

Non-negotiables in place. Admin: button position fixed, tabs with icons (current order), contacts stub added for preserve, redirects for old modals. Header static "Sietch CRM [ver]". Logos static. Flashing fixed to original hover (no re-render). Git committed + docs updated after changes.

Header core buttons (mail/add-tile/bookmarks) + sign-out now functional. 2C admin contacts tab enhanced (search + add). Continue filling admin tabs content (move forms, e.g. stages), ensure contacts full for func, keep all preserve rules. Sync moved later. Focus functional new DB/UI. Advance Phase 2C.

Update AGENTS/CHANGELOG/plan after every change.
