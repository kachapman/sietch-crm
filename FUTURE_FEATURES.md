# Future features — Sietch CRM Dashboard

Roadmap and implementation notes for planned work. Production: **https://dashboard.publicadjustermidwest.com**.

Related docs:

- **[ISSUES.md](./ISSUES.md)** — open bugs with acceptance criteria
- **[docs/UPDATE_AND_DEPLOY.txt](./docs/UPDATE_AND_DEPLOY.txt)** — ship changes to GitHub and the server
- **[Toaster_Features](./Toaster_Features)** — widget ideas inspired by major CRMs

---

## Document Server Integration (FEAT-022) — IMPLEMENTED in v3.0.0

**Status:** ✅ Completed. OnlyOffice Document Server (Community Edition) integrated for document viewing/editing.

### Architecture

- **Document Server** runs as a standalone Docker container (onlyoffice/documentserver:latest)
- **Link-out initially:** Dashboard shows document links that open in new tab on Document Server
- **Embedded editor:** Future phase — iframe embed in dashboard
- **Auth:** Document Server uses JWT tokens signed by dashboard
- **Storage:** Documents stored locally, accessed via Document Server API

### Implementation

- `server.py` — Document storage endpoints, Document Server proxy, JWT signing
- `docker-compose.yml` — Added docserver + redis services
- `init.sql` — Added `project_documents` table for file metadata
- `public/app.js` — Document link updates, upload UI stubs

### Migration Script Fixes (v3.0.0)

Known issues in `migrate_from_onlyoffice.py` to fix:
- `event_id` always NULL — history events not linked to projects
- `uploaded_by` uses wrong user — defaulting to admin instead of original uploader
- No `mime_type` — file type not preserved
- No retry on download — network failures skip files

---

## Offline Resilience — Mutation Queue for CRM writes (implemented)

Client-side queue (localStorage) + background retry worker for transient failures (network, 5xx from proxy/CRM, cross-droplet blips).

- All important mutators (stage change, due date, tags, history/notes, task create + close/reopen) now transparently queue on transient errors while preserving optimistic UI.
- FIFO replay on recovery with status badge ("N pending"), toasts on enqueue + successful sync, and light reconciliation (loadTasks / refreshAll).
- Hard errors (validation, auth, etc.) continue to fail immediately with original UX.
- No server.py changes required for this feature. Fully additive in public/app.js following existing api() + mutator + refresh patterns.
- See the session plan.md (in .grok history) for the full approved design, verification matrix, and rationale.

This directly addresses the "Offline Resilience (Mutation Queue)" part of the prior implementation suggestions. The real-time SSE polling bridge remains future work.

## FEAT-021 — Header quick note + icon actions (implemented)

### Shipped behavior

- **New opportunity** — icon-only **clipboard-plus**, `btn-primary` (bright blue).
- **Quick note** — icon-only **notebook-pen**, `btn-primary` (same blue).
- **Add tile** / **Refresh all** — icon-only, `btn-secondary`.
- **Quick note** modal: search/select opportunity (required), **note type** dropdown (CRM history categories), optional **Change Deal Due Date** and tags, required event note, notify users.
- Reuses deal-edit CRM helpers: `updateOpportunityDueDate`, `applyDealTagChanges`, `createOpportunityHistoryEvent`.

### Files

- `public/index.html`, `public/styles.css`, `public/app.js`

### Labels

All opportunity due-date fields in modals use **Change Deal Due Date** (deal-edit, quick-note, create-opportunity).

---

## FEAT-001 — Tile opportunity preview popup (priority: high)

### Goal

When the user clicks an **opportunity card** on a group tile (not the ✎ edit button), open a **read-only preview modal** that shows:

1. **Opportunity fields** — title, stage, value, contact, due date, responsible, tags, and visible **user fields** (custom fields).
2. **Recent history** — up to **10** CRM events for that opportunity (newest first).

Actions in the modal:

- **Open in CRM** (existing deep link)
- **Edit deal** — opens the existing deal-edit modal (`openDealEditModal`)
- **Close** / Escape

### UX (recommended)

- Reuse existing `.modal` / `.modal-card` patterns from `deal-edit-modal` and `create-opportunity-modal`.
- New elements in `public/index.html`: e.g. `#opp-preview-modal`, `#opp-preview-body`, `#opp-preview-history-list`.
- Card click: on `.card` (not `.card-edit-btn`, not `.card-title-link`) → `openOpportunityPreviewModal(oppId, group)`.
- Loading state: spinner or “Loading…” while fetching.
- History list: category title, author, date, plain-text snippet (strip HTML like notifications tile).

### API (already used elsewhere in the app)

| Data | Endpoint | Notes |
|------|----------|--------|
| Full opportunity | `GET /api/2.0/crm/opportunity/{id}` | May need `.json`; merge with card cache if fresh enough |
| Custom field values | `GET /api/2.0/crm/opportunity/{id}/customfield` (or `/customfields`) | Same fallbacks as `loadOpportunityDetails` in `app.js` |
| Definitions | `GET /api/2.0/crm/opportunity/customfield/definitions` | Labels for user fields |
| History (10 events) | `GET /api/2.0/crm/history/filter?startIndex=0&count=10&entityType=opportunity&entityId={id}` | Reuse `unwrapHistoryEvents()` |

### Implementation steps

1. **HTML** — Add preview modal skeleton (title, fields grid, history `<ul>`, footer buttons).
2. **CSS** — `public/styles.css`: compact field grid, history row typography; max-height + scroll for history.
3. **JS** — `openOpportunityPreviewModal(opp, group)`:
   - `Promise.all([ fetch opportunity, fetch custom fields, fetch history ])`
   - Map fields using existing helpers: `formatMoney`, `getOpportunityContactLabel`, `resolveOppStageId`, `customFieldLabel`, etc.
4. **Wire click** — In `renderCard()`, `card.addEventListener('click', …)` with guard for edit button / external link.
5. **Keyboard** — Escape closes preview; focus trap optional (match other modals).
6. **Test** — Opportunity with 0, 1, and 10+ history events; long HTML notes; missing contact.

### Acceptance criteria

1. Click card body → preview opens &lt; 2s on typical deal.
2. History shows ≤ 10 items, newest first.
3. Edit button in preview opens deal-edit modal; preview closes or stacks cleanly.
4. “Open in CRM” opens correct OnlyOffice opportunity URL.
5. No regression on ✎ edit or title link (still open edit / new tab).

---

## FEAT-002 — Fix New Opportunity custom user fields (ISSUE-001) (COMPLETED in v1.7.0)

**Status:** ✅ Completed 2026-06-11. `CREATE_OPP_USER_FIELDS_ENABLED=true`. All acceptance criteria met.

**Root cause:** Two bugs — (1) `collectCreateOppCustomFieldValues()` DOM selector matched the wrapper `<div>` instead of the actual `<input>`, so every field was silently skipped; (2) `buildCustomFieldListForApi()` returned `{Key, Value}` (PascalCase) format which, combined with flat `customField_{id}` fields, caused 400 errors.

**Fix:** Query `input, select, textarea` inside the wrapper div; use only `{key, value}` camelCase in `customFieldList`; include `customFieldList` in create body alongside per-field POST fallback. See CHANGELOG.md and [ISSUES.md](./ISSUES.md) for full details.

---

## FEAT-003 — Attachments on event notes (COMPLETED in v1.6+)

Implemented: native UploadProgress.ashx + history form-urlencoded with fileId[], text priority, 25MB, plain no-icon selected list, right-side 10s queue list near #mutation-sync-status (all items, ✓ success, ✕ fail), works in deal-edit/quick-note/side/notes-tile paths.

See CHANGELOG and public/app.js (uploadAttachmentForNote, extended createOpportunityHistoryEvent, submit paths, note queue render + 10s prune).

### Problem (historical)

The deal-edit modal only supported **plain-text** event notes (old hint removed). 

### Research (completed)

1. **Native CRM capture** (user provided curl data) — used exactly.
   - Network tab: look for `files`, `upload`, `history`, `attachment` endpoints and request shape.

2. **Likely OnlyOffice Workspace patterns** (confirm on your portal)
   - File upload: `POST /api/2.0/files` (multipart) → returns file id.
   - History event may accept file references in `content` HTML, `fileIds`, or a separate link API after event creation.

3. **Proxy changes (`server.py`)**
   - Today the proxy is JSON-oriented. File upload may require streaming multipart through the dashboard proxy without breaking body parsing.
   - Option A: upload directly from browser to `office.publicadjustermidwest.com` with session/cookie (complex cross-origin).
   - Option B: extend `server.py` to forward `multipart/form-data` to the portal (preferred for single-origin dashboard).

4. **UI**
   - `<input type="file" multiple>` in deal-edit fieldset.
    - Show selected file names; max size aligned with nginx `client_max_body_size` (100m on host nginx for dashboard.publicadjustermidwest.com).
   - On submit: upload files first, then create history event with returned ids embedded per API spec.

5. **Acceptance criteria**
   - Attach one PDF/image in deal-edit note → visible on same event in native CRM.
   - Error toast if upload fails; deal stage/tag/due saves still work if note fails (or all-or-nothing — decide in UX).

### Risk

If the portal API only allows attachments through the full CRM UI (undocumented), fallback is **“Open in CRM to attach files”** link until API is confirmed.

---

## FEAT-022 — In-modal document editor tab (deferred)

**Status:** Deferred. Not scheduled until the Document Server integration is fully unlocked and stable.

### Goal

Instead of opening documents in a separate browser tab, add an **in-modal editor tab** inside the existing Documents modal. A selected document opens in a new tab within the modal where the user can view/edit it via the Document Server, without leaving the dashboard.

This is intentionally **not** a dashboard tile. It reuses the existing Documents modal chrome (scopes, folders, toolbar) and simply keeps editing inside the same context.

### Why deferred

The Documents modal already uses the Document Server for editing by opening the editor in a separate tab/window. Before embedding it inside the modal we need:

1. Document Server JWT and callback wiring proven stable in production.
2. Save callbacks, title sync, and permissions working reliably.
3. CSP / iframe rules confirmed for the dashboard origin.
4. Any other Document Server features (co-edit, comments, versions, etc.) unlocked and understood.

Until then, documents continue to open in a separate tab/window as they do today.

### Implementation outline (for later)

**Frontend**

1. In the Documents modal, add a tab bar for open documents (`<editor-tab>`).
2. When the user opens a document, create an inline tab containing an iframe or `DocsAPI.DocEditor` instance.
3. Track open tabs in docs modal state; close tab removes the editor instance.
4. Keep the file list/folder sidebar visible so users can switch documents without closing the modal.

**Backend**

- Reuse existing editor-config endpoint; no new endpoints required for Phase 1.
- If save callbacks need to target the modal instead of the separate tab, surface callback status via the existing `/api/v2/documents/{id}/editor-config` or a lightweight status poll.

### Files (when implemented)

| File | Role |
|------|------|
| `public/index.html`, `public/app.js` | Documents modal tab bar, editor lifecycle |
| `public/styles.css` | Modal tab bar, editor iframe sizing |
| `server.py` | Existing editor-config/callback reused |
| `Toaster_Features` | Cross-reference |

---

## FEAT-023 — Stale Deals Tile (SCRAPPED in v1.8.0)

**Status:** ❌ Abandoned 2026-06-12. See ISSUE-004 in ISSUES.md for full post-mortem.

### Why it was scrapped
- **Attempt A (activity-based)**: Used `state.feedRawItems` (CRM notifications feed) to find last activity per opportunity. Failed because feed only covers last 30 days (max 150 events). Most opportunities had no activity found.
- **Attempt B (due-date-based)**: Switched to `expectedCloseDate` on opportunity objects. Added threshold dropdown (1 week / 30 days / 90+ days). Still failed to list any deals in any time period during testing — `expectedCloseDate` was not reliably past-due on open deals.
- **Result**: All code removed from `public/app.js`, `public/index.html`, `public/styles.css`.

### Future possibilities
- A proper CRM-side query for "last modified" timestamp (not available in current API).
- A different staleness metric (e.g., "no note added in 30 days" using a dedicated endpoint).
- Revisit if CRM API provides opportunity `updatedAt` or `lastActivityDate` in the future.

---

## FEAT-007 — Advanced Opportunity Search (enhanced CRM-style filtering)

**Status:** In progress — Phase D implementation active on `new-crm` branch. Expanding the search modal into a full filterable project directory with full-text search across deal and user fields.

### What the native CRM search does

In OnlyOffice CRM, the Opportunities page has a search/filter panel that lets you:
- Filter by stage (single or multi-select)
- Filter by responsible user (team member)
- Filter by tags (multi-select)
- Filter by custom/user fields (e.g., "Claim Type", "Loss Date", "Adjuster")
- Filter by date range (created, expected close, last modified)
- Filter by deal value (min/max)
- Full-text search across title, description, contact name
- Save and name filter combinations

### Current dashboard implementation

- **Header search bar** and **search modal** currently search by **title only** via `filterValue`.
- Search modal has stage/owner filters, batch ops, rich results, select-all, and row-click preview.
- Tags are searched via a separate Tags tab.
- No custom-field filtering, no sort options, no server-side pagination.

### Why this matters

The user explicitly needs to find **opportunities with similar custom/user fields** (e.g., "all claims with Claim Type = Hail Damage", "all deals with Loss Date in March"). This is a primary CRM workflow that currently requires leaving the dashboard.

### Implementation phases

| Phase | Feature | Status | Notes |
|-------|---------|--------|-------|
| A | Rich search results (show stage, due date, value, contact in dropdown) | ✅ Shipped | Stage/owner filters, batch ops, select-all, row-click preview. |
| B | Filter by stage + responsible user | ✅ Shipped | Included in Phase 2A. |
| C | Filter by tags | ✅ Shipped | Separate Tags tab; being merged into Projects tab in Phase D. |
| D | **Filter by custom/user fields + full-text search + pagination + sort** | ✅ Shipped | Server-side `customFieldFilters`, full-text `filterValue` across title/description/contact/custom fields, 50-item pagination, sort dropdown, tag filter merged into Projects tab, "Open in CRM" links removed, softened title/checkbox contrast. |
| E | Date range filters (created, closing, last modified) | 🔲 Planned | Needs dedicated UI (from/to pickers). |
| F | Full-text search (description, contact, company) | 🔵 In Progress | Rolled into Phase D. |
| G | Saveable searches / named filters | 🔲 Planned | Persist in user profile. |

### Priority: Phase D first

Custom/user field filtering plus full-text search is the most-used workflow. It leverages:
- `state.customFieldDefs` (already loaded in dashboard)
- `/api/v2/projects` local v2 API (extended with `customFieldFilters`, `tagId`, full-text `filterValue`, and pagination)
- New `/api/v2/projects/count` endpoint for pagination totals
- Trigram indexes for fast `ILIKE` across text fields

### Files to modify

- `server.py` — Full-text WHERE, custom-field JOINs, tag filter, sort by stage, count endpoint, pagination default
- `init.sql` — Trigram and B-tree indexes
- `public/app.js` — Search logic, filter UI state, custom-field rows, pagination, background tab add
- `public/index.html` — Search modal filter panel (remove Tags tab, add tag/sort/custom-filter/pagination controls)
- `public/styles.css` — Filter rows, pagination bar, mobile stacking
- `CHANGELOG.md`, `AGENTS.md` — Release notes and session summary

### See also

- `sietch-crm-plan.md` — Phase 2A Details section
- `Toaster_Features` — Cross-reference for CRM search patterns
- `ISSUES.md` — No blocking issues

---

## Other ideas (backlog)

See **[Toaster_Features](./Toaster_Features)** for dashboard tile/widget ideas (pipeline metrics, stale deals, email, etc.).

| ID | Idea | Effort |
|----|------|--------|
| FEAT-004 | Persist `estimate-network` in `docker-compose.yml` on server | Low |
| FEAT-005 | Custom fields on **edit** deal (not only create) | Medium |
| FEAT-006 | Export group/opportunities to CSV | Medium |
| FEAT-007 | **Advanced Opportunity Search** — filter by stage, user, tags, custom fields, date range (see detailed section below) | High |
| FEAT-022 | **In-modal document editor tab** — open documents inside the existing Documents modal instead of a separate page/tab. Deferred until Document Server features are fully unlocked. | Deferred |
| FEAT-023 | **Documents: Save-as conversion** — export doc to PDF/ODT/Markdown from the documents modal context menu or toolbar. OnlyOffice Document Server supports format conversion via callback, but a simpler approach is server-side conversion using LibreOffice headless (`soffice --convert-to pdf`). Needs Docker container with LibreOffice or a dedicated conversion API. | Medium |
| FEAT-025 | **Project number + email auto-linking** — expose a unique project number (or short ID) on each opportunity; user includes it in the subject line when forwarding emails to the CRM inbox; a classifier or regex on incoming mail auto-links the email to the matching project's history. | High |
| FEAT-026 | **Full dashboard light mode** — a theme toggle (dark ↔ light) that reskins the entire dashboard UI. Final cosmetic phase after all functional work is complete. | Low |

### FEAT-008 — AccuLynx API research (post-v1.1, from user list)

**Status:** Research complete; suggestions documented for future implementation. No code changes yet. Abandoned for now (user feedback: document and park).

#### Findings (from AccuLynx public docs + integrations, June 2026)
- AccuLynx is a leading all-in-one roofing/claims/estimating CRM and business management platform (sales, production, finance, operations). Used by contractors for leads, jobs, contacts, estimates, milestones, photos, materials pricing, etc.
- **API:** Public REST API v2 at `https://api.acculynx.com/api/v2`. 
  - Auth: Bearer token (API key). Admin creates/names key in AccuLynx Account Settings → API section. Include `Authorization: Bearer <key>` on all requests.
  - Rate limits + terms apply (see their docs).
- **Key endpoints** (examples; full reference at https://apidocs.acculynx.com ):
  - Jobs: list, get by id (includes contacts, milestones, etc.).
  - Estimates: list/get (with includes=job,createdBy,sections...).
  - Contacts, users, milestones, webhooks/subscriptions for events.
- **Existing integrations:** Hover (measurements/photos), Make/Zapier, HubSpot, accounting, etc.
- **Suggestions for future (low-effort start on local dashboard):**
  - Store AccuLynx API key in user profile (local dev only for safety).
  - Manual "Import from AccuLynx" or small "AccuLynx Jobs" header widget / toaster: fetch recent jobs, map to opp create (title, contact, value, custom fields for claim # etc.).
  - Webhook receiver (if running locally) for auto-create/update on job events.
  - Benefits for Sietch CRM: faster data transfer from field tools into OnlyOffice CRM, less double entry.
  - Risks: API keys, mapping/deduping, only useful when dashboard runs on same machine as user workflow.

**References:** https://apidocs.acculynx.com, existing Hover/Make integrations.

---

## Suggested implementation order

**Status:** Investigated 2026-06-26. Not implemented — parked for future consideration.

### Goal

Embed the **native OnlyOffice CRM mail module** (or the full CRM) inside the dashboard as an iframe tile or popup modal, giving access to 100% of CRM functionality without leaving the dashboard.

### Background

The API-based mail module approach was tried multiple times (ISSUE-002) and failed on:
- Read/unread status pushes to server being unreliable
- Account/inbox selector dropdown never working
- Various incomplete API coverage

### Investigation findings (2026-06-26)

**Headers from `office.publicadjustermidwest.com`:**
- `X-Frame-Options: SAMEORIGIN` — blocks framing from other domains
- No CSP `frame-ancestors` restriction
- No CORS headers

**Quick test:** Added `/crm-proxy/` route to `server.py` that fetches CRM pages and strips `X-Frame-Options`. Confirmed the page loads through the proxy with framing headers removed.

**Critical blocker (asset resolution):** The CRM's HTML and JS use **absolute paths** for assets and API calls:
- Assets: `/skins/default/...`, `/discbundle/common/css/...`
- API calls: `/api/2.0/mail/conversations/...`

When loaded under `/crm-proxy/` on the dashboard domain, these resolve to the dashboard's server, not the CRM portal.

### The cleanest path forward (subdomain proxy)

A separate **nginx proxy subdomain** — no URL rewriting, no Python overhead, everything works natively.

1. **DNS:** Point `crm-proxy.dashboard.publicadjustermidwest.com` to the dashboard droplet IP
2. **nginx:** Add a `server_name` block that proxies ALL traffic to `https://office.publicadjustermidwest.com` and strips `X-Frame-Options`
3. **Iframe:** Dashboard loads `https://crm-proxy.dashboard.publicadjustermidwest.com/addons/mail/Default.aspx`
4. **Auth:** The `oo_token` cookie must be scoped to `.publicadjustermidwest.com` (parent domain) so both subdomains can read it. nginx passes it as the `Authorization` header.

**Why this works:** Because the iframe loads from a **different subdomain** (not a sub-path), absolute paths like `/api/2.0/...` and `/skins/default/...` all resolve to the same nginx proxy → portal. No HTML rewriting needed. The CRM's own JS works unmodified.

### Risks

| Risk | Mitigation |
|------|-----------|
| CRM updates change bundles or HTML | Monitor on update; fix nginx config if needed |
| `oo_token` cookie scope change | Set on `.publicadjustermidwest.com` parent domain |
| nginx caching stale assets | Short cache TTL on proxy responses |
| WebSocket / SignalR connections | May need separate proxy config |
| Performance under load | nginx handles proxying in C — negligible overhead |

### Files (when pursued)

| File | Change |
|------|--------|
| Production nginx config (`/opt/estimate-enhancer/nginx.conf`) | New `server_name` block for `crm-proxy.*` |
| `public/app.js` | Add "Open CRM Mail" tile/modal with iframe |
| `public/index.html` | Iframe container in modal or tile body |
| `public/styles.css` | Iframe sizing, full-height modal |
| `server.py` | (local dev) Catch-all proxy for `/crm-proxy/*` with body forwarding, auth |

---

## FEAT-025 — Project Number + Email Auto-Linking

**Status:** Planned — detailed design below.  
**Priority:** High  
**Area:** Opportunity preview modal, email modal, server-side mail processing

### Problem

When a user forwards an email to the CRM inbox (to link it to a specific project/opportunity), they currently have to manually open the deal-edit modal and paste a link or type the deal name. The CRM's native mail linking searches by contact name or deal title, which is ambiguous when multiple deals share similar names. There is no reliable, machine-readable identifier the user can include in the email subject line.

### Solution

Expose a **unique project number** (or short alphanumeric ID) on each opportunity. The user copies this number into the email subject line when forwarding. A server-side classifier or regex on incoming CRM mail events detects the number pattern, looks up the matching project, and auto-links the email to that project's history — eliminating the manual linking step.

### Design

#### Project number assignment

| Approach | Pros | Cons |
|----------|------|------|
| **A. Sequential integer** (`#1001`, `#1002`, …) | Human-readable, short, predictable | Requires a DB sequence; gaps on delete; not meaningful |
| **B. Short hash** (`PRJ-A3F`, `PRJ-7K2`) | No sequence, collision-resistant | Slightly harder to type; still compact |
| **C. Custom field** (existing `customFieldList`) | No schema change | Not enforced; users can edit/delete it; no uniqueness guarantee |

**Recommended: Approach A** — sequential integer with a configurable prefix (default `#`). Stored in a new `project_number` column on the `opportunities` table (or `project_documents` if reusing existing schema). Auto-assigned on opportunity creation; never reused.

#### Where the number is shown

1. **Opportunity preview modal** — displayed prominently next to the title, e.g. `#1042 — Smith Roofing Claim`. Copy-to-clipboard button.
2. **Deal-edit modal** — shown in the header or a read-only field (non-editable once assigned).
3. **Card on group tile** — optional: small `#1042` badge in the card corner (configurable via profile setting).

#### Email subject syntax

The user includes the project number in the email subject line using a recognizable syntax:

```
Fwd: Re: Inspection notes [#1042]
```

or

```
Fwd: Claim update PRJ-1042
```

The classifier looks for:
- `[#<number>]` — bracketed hash + digits (primary pattern)
- `PRJ-<number>` — prefix + digits (alternative)
- `<number>` alone — fallback, but lower confidence (may match unrelated digits)

#### Server-side classifier (`server.py`)

A new endpoint or background hook on the CRM mail webhook/notification path:

1. When a new mail event arrives in the CRM (detected via the existing `/api/2.0/crm/history/filter` polling or a future webhook), extract the subject line.
2. Run the regex patterns against the subject.
3. If a match is found:
   - Look up the project by `project_number` in the local DB.
   - If found, call the CRM's `PUT /api/2.0/mail/crm/link.json` to auto-link the email to the matched opportunity.
   - Log the auto-link event in the infra ring buffer.
4. If no match or ambiguous, skip (user can still manually link via the email modal sidebar).

#### Classification models considered

| Model | Complexity | Accuracy | Notes |
|-------|-----------|----------|-------|
| **Regex only** | Low | High for structured syntax | Best starting point; `[#\d+]` and `PRJ-\d+` patterns |
| **Keyword + regex** | Low | High | Add "project", "claim", "job" context words around the number |
| **ML classifier** | High | Potentially higher for noisy subjects | Overkill for v1; regex covers 95%+ of intentional use cases |

**Recommendation:** Start with regex. The user is the one typing the number, so they control the format. A simple regex with two patterns covers the use case without infrastructure overhead.

#### DB schema change

```sql
ALTER TABLE opportunities ADD COLUMN project_number INTEGER UNIQUE;
CREATE SEQUENCE project_number_seq START 1001;
```

Or, if using the existing `project_documents` table for number assignment, add a `project_number` column with a unique constraint.

#### Files to modify

| File | Change |
|------|--------|
| `server.py` | Auto-assign `project_number` on opp creation; expose in opp GET response; new `/api/v2/projects/{id}/number` endpoint; mail classifier hook |
| `init.sql` | `project_number` column + sequence |
| `public/app.js` | Display number in preview modal + deal-edit; copy-to-clipboard button; card badge (optional) |
| `public/styles.css` | Number badge styling, copy button |
| `public/index.html` | Number display slot in preview modal markup |

#### Acceptance criteria

1. Every opportunity has a unique `project_number` assigned at creation.
2. Preview modal shows `#<number>` next to the title with a copy-to-clipboard button.
3. When a user forwards an email with `[#1042]` in the subject, the email is auto-linked to project #1042 within a reasonable polling interval.
4. If no match is found, no error is raised — the email simply isn't auto-linked.
5. The number is non-editable by users (read-only).

---

## FEAT-026 — Full Dashboard Light Mode

**Status:** Planned — final cosmetic phase.  
**Priority:** Low  
**Area:** Entire dashboard UI (`public/styles.css`, `public/app.js`, `public/index.html`)

### Goal

Provide a **theme toggle** (dark ↔ light) that reskins the entire dashboard. The current dark theme is the default. Light mode inverts surfaces, borders, text, and accents to produce a clean, readable light UI.

### Scope

| Element | Dark (current) | Light |
|---------|----------------|-------|
| Background | `#0f172a` (slate-900) | `#ffffff` or `#f8fafc` |
| Surface | `#1e293b` (slate-800) | `#ffffff` |
| Surface elevated | `#334155` (slate-700) | `#f1f5f9` |
| Text | `#e2e8f0` (slate-200) | `#0f172a` (slate-900) |
| Muted text | `#64748b` (slate-500) | `#64748b` (same) |
| Borders | `#334155` (slate-700) | `#e2e8f0` (slate-200) |
| Accent | `#3b82f6` (blue-500) | `#2563eb` (blue-600, slightly darker for contrast) |
| Cards/tiles | Dark surfaces with subtle borders | White/light surfaces with subtle shadows |

### Implementation approach

1. **CSS custom properties** — all colors already use CSS variables (`--bg`, `--surface`, `--text`, `--border`, etc.). A `[data-theme="light"]` selector on `<html>` overrides every variable.
2. **Toggle** — small sun/moon icon button in the header (next to the sign-out button or inside the profile dropdown).
3. **Persistence** — stored in `user_profile_store` (server) + localStorage fallback. Applied on page load via `document.documentElement.dataset.theme`.
4. **No JS class toggling** — CSS variables handle everything; no per-component class swaps needed.
5. **Scope** — all dashboard UI (tiles, modals, sidebar, header, footer, admin console). Document Server iframe remains unchanged (OnlyOffice has its own theme).

### Files to modify

| File | Change |
|------|--------|
| `public/styles.css` | `[data-theme="light"]` variable overrides at `:root`; toggle button styles |
| `public/app.js` | Toggle handler; load theme from profile/localStorage on init; persist on change |
| `public/index.html` | Toggle button in header; `<html data-theme="dark">` default |
| `user_profile_store.py` | Persist `theme` preference (dark/light) |
| `server.py` | Expose `theme` in profile payload |

### Acceptance criteria

1. Toggle switches between dark and light mode instantly (no page reload).
2. All dashboard surfaces, text, borders, and accents are readable in both modes.
3. Theme persists across sessions (server + localStorage).
4. Default is dark (current behavior unchanged for existing users).
5. Document Server iframe is unaffected.

---

## Phase 2G — User Profile Modal + Notification System Overhaul

**Status:** In progress on `new-crm` branch.

### What it covers

| Part | Description |
|------|-------------|
| **A. Profile modal** | Header button (user-square-rounded icon, left of sign-out) → modal with avatar, name/email, password change, notification prefs |
| **B. Avatar upload** | Profile picture with Pillow thumbnail (200x200), initials fallback, stored in `data/avatars/{user_id}/` |
| **C. @-mention system** | Replaces old `<select multiple>` notify list with inline `@username` autocomplete in note editors |
| **D. Feed tile overhaul** | Shows only @-mentioned + task-assigned items via `GET /api/v2/notifications` (not all events) |
| **E. Notification prefs** | Dormant toggles in profile modal (in-dashboard, Telegram), wired to `notification_preferences` table |
| **F. Lightweight project page** | `public/project.html` — mobile-optimized page for Telegram notification links, requires auth, full project details + event history with `?event={id}` highlight |

### Why the notification system changed

The old system used a `<select multiple>` to pick notification recipients — clunky, too many users listed. Modern pattern: `@username` autocomplete inline in the note editor, like Slack/GitHub. Only @-mentioned users get notified. Task assignments also generate notifications.

The old feed tile showed ALL history events from ALL projects. New feed shows only items relevant to the current user: @-mentions and assigned tasks.

### Telegram integration

The `notification_dispatcher.py` background thread already polls `notifications` table and sends Telegram messages. Changes:
- Add project link to messages: `{DASHBOARD_URL}/project/{id}?event={event_id}`
- Check `notification_preferences.telegram` before sending
- The lightweight project page requires auth (session cookie) and shows full project details + highlighted event

### Files

| File | Changes |
|------|---------|
| `init.sql` | `avatar_url` column on `users` table |
| `server.py` | PUT /api/v2/me, avatar endpoints, notification prefs, /project/{id} route |
| `public/project.html` | New lightweight project detail page |
| `public/index.html` | Profile button, profile modal, mention dropdown, remove old notify selects |
| `public/app.js` | Profile modal JS, @-mention autocomplete, feed rewrite, remove old parsing |
| `public/styles.css` | Profile modal, avatar, mention chips, project.html styles |
| `notification_dispatcher.py` | Preference check, project link in messages |

---

## Suggested implementation order

1. **FEAT-001** — Preview popup ✅ Done.
2. **FEAT-002** — Custom fields on create ✅ Done.
3. **FEAT-003** — Note attachments ✅ Done.
4. **FEAT-025** — Project number + email auto-linking ✅ Done.
5. **OAuth2 for Outlook.com** — Next priority. DB columns exist but zero functional code. Full stack needed: Azure app registration, OAuth redirect/callback, token exchange/refresh, XOAUTH2 IMAP auth, OAuth2 SMTP auth, "Sign in with Microsoft" UI.
6. **FEAT-022** — In-modal document editor tab (deferred — after Document Server features stable).
7. **FEAT-024** — Native CRM iframe embed (investigated, not implemented — see above).
8. **FEAT-026** — Full dashboard light mode (deferred — final cosmetic phase).
9. Pick items from **Toaster_Features** by priority.