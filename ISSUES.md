# Known issues

## ISSUE-015 — Fresh databases don't self-build schema (failed migrations recorded as applied)

**Status:** 🟡 Open (observed + repaired live 2026-10-26, proper fix pending)
**Priority:** High
**Area:** Backend boot (`db.py`, `init.sql`, `migrations/`)

### Summary

`init.sql` is only ever applied by the postgres docker entrypoint
(`docker-compose.yml` mounts it into `/docker-entrypoint-initdb.d`), which
runs solely against a brand-new database volume. `db.init_db()` at server boot
runs only the `migrations/*.sql` runner — never the base schema. On a truly
fresh database (e.g. dev `sietch_crm` dropped + recreated 2026-10-26), every
migration fails against missing base tables, and worse, the runner **records
each failed migration as applied** (`db.py` "record as applied to prevent
retry loop"), so subsequent boots never retry them. The server then runs with
~5 tables and 500s across most endpoints.

### Repro (dev, 2026-10-26)

1. `DROP DATABASE sietch_crm; CREATE DATABASE sietch_crm;`
2. Boot server → ~38 `Migration *.sql failed (relation ... does not exist)` warnings, all recorded.
3. `GET /api/v2/mail/unread-count` (and most other endpoints) → 500.

### Repair performed live

Applied `init.sql` manually via `docker cp` + `psql -f` (62 tables, zero
errors), cleared the bogus `_applied_migrations` rows so the boot runner
re-applied 001–038 cleanly (one benign 031 already-exists warning), verified
login/create/history/delete end-to-end, deleted the verification deal.

### Proper fix (pending)

Make a fresh boot self-sufficient: apply `init.sql` (or an equivalent
base-schema step) from `db.init_db()` when the database is empty, BEFORE the
migration runner — and/or stop recording failed migrations as applied (retry
next boot instead). Verify with: fresh DB → boot → zero migration failures →
smoke test green.

### Files involved

| File | Role |
|------|------|
| `db.py` | `_run_pending_migrations` records failures as applied; no base-schema step |
| `init.sql` | Base schema, only wired to the postgres entrypoint |
| `docker-compose.yml` | Mounts init.sql into initdb.d (first-volume-boot only) |

## ISSUE-014 — Tile toolbar uniformity, click-to-edit titles, height resize

**Status:** ✅ Implemented 2026-07-21 (Phase 2D-3 follow-up; committed, NOT pushed)  
**Priority:** High  
**Area:** Dashboard tiles (`public/app.js`, `public/styles.css`)

### Summary

Toolbar buttons wrapped unpredictably on narrow tiles and mobile. Pencil edit buttons and statically-sized title inputs pushed other buttons onto extra rows. Height could not be resized. Minimize/Close were not reliably top-right, and Refresh was not consistently before them.

### Changes

- **Height resize:** Corner handles now resize both width (quarter/half/full) and height (normal/double). Ghost preview shows target size during drag; changes commit once on pointerup.
- **Title editing:** Removed all pencil/edit buttons. Clicking the title text itself enters inline edit. The input is only present while editing and sizes itself to the text (`Nch` units) so it never pushes buttons.
- **Uniform toolbar structure:** All tiles now use `.tile-toolbar-tools` (tile-specific buttons: filter, template, format, new-task, eye, calendar nav, etc.) and `.tile-toolbar-actions` (Refresh, Minimize, Close). Actions are last with `margin-left:auto`.
- **Narrow/mobile rows:** On `.tile-quarter`, `.tile-half`, and `≤600px` viewport, actions stay on row 1 (top-right); tools drop to row 2. Full-width tiles stay single-row.
- **Ordering guarantees:** When a refresh button exists it is first in actions, followed by Minimize then Close. All chrome creators and button inserters were updated to use the containers.
- **Cleanup:** Removed dead `ensureNotesToolbarRows`, related top-row hacks, and all `.btn-edit-group-name` creation. Calendar tiles no longer use a permanently visible static-width input.

### Files changed

| File | Role |
|------|------|
| `public/app.js` | `buildTileToolbarShell`, `makeTileTitleEditable`, height support in `bindTileResize`, restructured chrome binders, rerouted button insertion, removed stale notes toolbar code |
| `public/styles.css` | `.tile-toolbar-tools` / `.tile-toolbar-actions`, content-sized titles, narrow + mobile order rules, removed dead notes top-row / edit-button CSS |
| `public/index.html` | Cache-bust `v=1.95.6` / `v=1.87.14` |

---

## ISSUE-013 — Dashboard tiles jump erratically during resize/drag ("invisible grid")

**Status:** ✅ Fixed 2026-07-21 — Phase 2D-3 (pending user browser verification; committed, NOT pushed)  
**Priority:** High  
**Area:** Dashboard tile grid (`public/app.js`, `public/styles.css`)

### Summary

After the Phase 2D/2D-2 layout overhaul, tiles jumped around erratically during resize and drag. The dashboard felt like "an invisible grid holding the tiles in place."

### Root causes (five compounding defects)

1. **`grid-auto-flow: dense`** (styles.css `.dashboard-tiles`) — any span change re-packed ALL tiles into the first available gaps, so resizing/dragging one tile reshuffled the whole board. It also made visual order diverge from saved `tileLayout.order`.
2. **Resize committed live on every mousemove** (`bindTileResize` → `setTileWidth` → `saveLayoutToStorage` + `applyTileLayoutClasses`) — localStorage write + profile-save scheduling + actual `grid-column: span N` class flip ~60×/second during a drag.
3. **Snap threshold measured from a moving target** — `getComputedStyle(grid).gridTemplateColumns` re-read every mousemove, right after the previous mousemove changed the spans; the column width being measured against shifted mid-drag → oscillation.
4. **SortableJS `forceFallback: true`** — the element following the mouse was an absolutely-positioned clone outside grid flow while the real tile stayed locked in its grid slot (ghost class) — the exact "invisible grid" sensation. Additionally, dead `toolbar.draggable = true` attributes (pre-Sortable native DnD leftover) would have stolen native drags from the tile.
5. **`transition: transform` + `will-change: transform` on every `.dashboard-tile`** — all reflow movement was animated and GPU-promoted, making reflows look like jumping.

Secondary: `mountDashboardTiles()` did `root.innerHTML = ""` + destroy/recreate Sortable on every call.

### Fix (Phase 2D-3)

1. **Ghost-preview resize** — `bindTileResize` rewritten: measure column/gap/offsets ONCE at `pointerdown`; during `pointermove` only a `.tile-resize-ghost` dashed overlay moves (clamped inside the grid content box so it can't force a horizontal scrollbar); span class + `saveLayoutToStorage()` commit exactly ONCE on `pointerup`, only if the span changed. Unified to pointer events (`touch-action: none` on handles).
2. **SortableJS `forceFallback: false`** — the dragged tile participates in grid flow; neighbors shift once with the existing 200ms animation. Instance is now created once and kept (`root._sortableInstance`), not destroyed/recreated per mount.
3. **Removed** `transform` transition + `will-change: transform` from `.dashboard-tile` (hover shadow/border kept).
4. **`mountDashboardTiles()`** — no more `innerHTML = ""` teardown; reorders in place via `appendChild` (preserves listeners/scroll/iframes) + sweeps only stray tiles whose id left the layout.
5. **Removed dead `toolbar.draggable = true`** from all four tile-chrome creators (`createTileChrome`, `bindGroupTileChrome`, `bindCalendarTileChrome`, `bindNotesTileChrome`) — conflicted with native-mode Sortable drag.

### Follow-up (same day): gaps between tiles

Removing `grid-auto-flow: dense` left dead space whenever tile widths/heights didn't fill a row — user: "not at all how tiling works." Two-part fix:

1. **Restored `grid-auto-flow: dense`** so later tiles backfill holes (tight packing). Safe now: resize commits once per gesture (single re-pack) and the other four jumpiness causes are gone. Accepted behavior: with mixed widths, drop placement during drag can be "filled around" by smaller tiles — deterministic for a given (order, widths) set.
2. **Root-caused the thin strips between tiles:** the legacy `.panel` class (reused on feed/tasks/presence tiles) carries `margin: 0 auto 1.25rem` from when it was a full-width top panel. Auto left/right margins on a grid item override stretch — the tile shrank to content width and centered in its grid area, leaving dead strips on both sides plus extra bottom margin. Added `.dashboard-tile.panel { margin: 0; max-width: none; }` so panel tiles stretch to fill their cells.

### Files changed

| File | Role |
|------|------|
| `public/styles.css` | dense flow restored, `position: relative` grid, `.dashboard-tile.panel` margin override, ghost styles, handle `touch-action`, tile transition slimmed |
| `public/app.js` | `bindTileResize` rewrite, `mountDashboardTiles` in-place reorder, Sortable options, 4× draggable removal |
| `public/index.html` | cache-bust `app.js?v=1.95.5`, `styles.css?v=1.87.13` |

### Known follow-up (out of scope, not fixed)

- `renderBoardGroups()` still removes/recreates every group tile `<section>` on each data refresh (app.js:8198), losing scroll/filter DOM state. Should become idempotent (update in place). Deliberately deferred — drag/resize stability was the reported symptom.
- Pin-feature dead code remains (`PINNED_TILE_IDS`, `isTilePinnedToTop`, `setTilePinnedToTop`, `normalizeOrderForPinned`, `bindTilePinButton`, `applyTilePinState`, `isTilePinned`/`setTilePinned`).
- Odysseus-style freeform windows (fixed position + snap zones) evaluated and rejected — incompatible with CSS Grid architecture, high risk. User chose grid fix.

---

## ISSUE-011 — Documents modal sidebar: per-project list loaded at open + infrastructure monitoring

**Status:** ✅ Fixed 2026-07-20  
**Priority:** Medium  
**Area:** Documents search modal, admin console, server health (`public/app.js`, `public/index.html`, `public/styles.css`, `server.py`, `docker-compose.yml`)

### Summary

The Phase 2H Documents modal loaded a per-project list in the sidebar as soon as it opened (`loadDocsProjects()` → `/api/v2/projects/simple` → render up to 5 recent projects). This showed per-project documents before the user had performed any search. Additionally, there was no server-reachability indicator — when the backend went down, users had no way to know until they triggered a failed API call.

### Fixes applied

**Documents modal:**
- Removed the project list (`#documents-project-list`) from the Documents modal sidebar.
- Removed `loadDocsProjects()` / `renderDocsProjectList()` and the call from `openDocumentsModal()`.
- Removed `projects: []` from `docsState` and unused CSS rules.
- Documents toolbar: Delete/Move/Copy buttons consolidated into a single "Actions" `<select>` dropdown.

**Server health indicator:**
- Added `<span id="server-health-indicator">` in header (after admin-console-btn, before sign-out).
- CSS: hidden by default; `.unreachable` class shows amber icon + dot; positioned between admin gear and sign-out.
- Mobile layout reordered: sign-out (rightmost) → admin gear → health indicator → logo (top-left).
- Desktop layout: sign-out (1.5rem) → admin gear (3.5rem) → health indicator (5.5rem).
- `api()` function hooks: network error or 502/503/504 → show indicator; successful response → clear indicator.
- 60s health poller: calls `GET /api/health`; skips when `document.hidden` (no throttle false positives); 3 consecutive successes → hide indicator.

**Admin Infrastructure tab:**
- New tab in admin console (after Logs) with: server status (DB reachability), Docker health (container status, restart count), infrastructure event log (rolling 200 events), restart controls (restart server process or Docker container).
- Server endpoints: `GET /api/v2/admin/infra-log`, `GET /api/v2/admin/docker-health`, `POST /api/v2/admin/restart-server`, `POST /api/v2/admin/restart-container`.

**Docker:**
- `healthcheck` added to `docker-compose.yml` (calls `/api/health` every 30s).
- `restart: on-failure:5` (max 5 automatic restarts, then stop).

**Server:**
- In-memory rolling 200-event `collections.deque` ring buffer tracks errors, restarts, and server events.
- `log_infra_event()` called on Document Server unreachable, restart requests.

---

## ISSUE-006 — WebKit / Apple cache-busting attempt (July 4) — expensive disaster, ignored user feedback

**Status:** ❌ Fully scrapped and removed 2026-07-05

### What was attempted
- Added `detectAppleWebKit()`, `state.isAppleWebKit`, `bustCache()` helper, and conditional `bustCache()` calls on short-TTL proxy paths (filter/history/tag/customfield).
- In `server.py:_handle_api_get`, on cache miss for dynamic paths, emitted `Cache-Control`, `Pragma`, `Vary`, `Expires` headers via `send_header` **before** `send_response`, plus `isDynamicShortTtl` logic.
- Goal: make dashboard more "Apple friendly" by preventing WebKit from caching CRM responses.

### Breakage
- Produced `net::ERR_INVALID_HTTP_RESPONSE` (HTTP 400) on direct http LAN testing (`http://127.0.0.1:8765` and local LAN IPs).
- Affected: opportunity filter, history/filter, batch-opportunity-tags, individual tag calls.
- Login and some proxy calls succeeded; core data loads (groups, history, tags) failed after login.
- Prod was masked by host nginx; local dev (plain http, no nginx) was the canary.

### Ignored user feedback
- User repeatedly stated "local dev used to work great until the edits" and that the breakage started exactly after the WebKit changes.
- Initial agent focus was on auth, reachability, or 502s instead of acting on the reported timeline.
- User had to argue the point multiple times; agent kept trying additional changes while claiming the WebKit edits were not the issue.

### Outcome
- Entire WebKit feature scrapped.
- All `detectAppleWebKit`, `state.isAppleWebKit`, `bustCache`, `isDynamicShortTtl`, and premature header emission blocks removed.
- Pre-existing server TTL cache (`_proxy_cache`, 30s/600s), cache invalidations on mutations, `newId()` (crypto.randomUUID fallback), 502/5xx JSON resilience, friendly tile errors + retry links, LAN printing, batch tags, and all prior proxy/auth/upload logic preserved.
- Documented here and in CHANGELOG as an expensive disaster and waste of tokens.

### Why it happened
- Attempt to solve a perceived platform caching problem without first reproducing the exact reported symptoms on the actual dev path (plain http, no nginx).
- Failure to stop and revert when user timeline evidence was provided.

---

## ISSUE-005 — Agent repeatedly forgets completed work (2026-07 session)

**Status:** Ongoing — documented 2026-07-05

### Symptoms
- Backdate HTML inputs (`<input type="date" id="*-note-created">`) were implemented, tested locally with working backdated POSTs, and visible in a live session.
- Only the supporting JS (reset/read/submit) and CSS (width/hue) were committed and pushed.
- HTML change was never present in the tree; feature appeared broken on fresh loads despite working in cached browser.
- "WebKit catastrophe" / Safari date-input wrapper issues were discussed at length but never written to any committed file.
- Agent would claim "done" and "pushed" when the actual markup was missing.

### Root cause (observed)
- Session state and chat context are not persisted into the source tree or docs.
- Agent trusts prior conversation as truth instead of re-reading committed files + `git show`.
- No mandatory step to run `git status --short && git diff --stat`, confirm exact lines changed, and update CHANGELOG + AGENTS last-session summary before ending work.

### Mitigation (enforced going forward)
- After every feature/fix: run `git status --short && git diff --stat`.
- Explicitly confirm the changed lines are in the working tree and committed.
- Write a one-line summary under "Last session summary" in AGENTS.md.
- Add a CHANGELOG entry (and RELEASE note for tagged versions).
- If the user has to argue "where is the HTML?", the agent has failed the rule.

### Related
- Similar pattern seen with the WebKit date-input wrapper removal rationale and the "backdate button" being only partially shipped.

---

## ISSUE-004 — Stale Deals Tile: attempted, debugged, and scrapped

**Status:** ❌ Abandoned 2026-06-12 — removed in v1.8.0
**Area:** Dashboard tiles (`public/app.js`, `public/index.html`, `public/styles.css`)

### What was tried

We wanted a tile that shows "stale" opportunities (claims with no recent CRM activity or past their due date). Two approaches were attempted:

**Attempt A (activity-based):**
- Used `state.feedRawItems` (CRM notifications feed) to find the last activity event per opportunity.
- Computed days since last event. If no event found in the feed window, showed "no activity in N days."
- Added caching (`localStorage` daily key), midnight refresh, severity colors (amber/orange/red), and a threshold dropdown.
- **Why it failed:** The feed only covers the last 30 days (max 150 events). Most opportunities had no activity found in that window. The fallback to `opp.created` (creation date) was misleading — a deal created 6 months ago might have had activity yesterday.
- **Debug finding:** `state.feedRawItems` contains parsed objects from `parseRelationshipNotifyEvent`. Only the last 30 days of history are available. Opportunity objects from `fetchOpportunitiesForGroup` have `created` but no `updated` field.

**Attempt B (due-date-based):**
- Switched to `expectedCloseDate` on opportunity objects. If the due date was more than N days in the past, the deal was "stale."
- Added threshold dropdown (1 week / 30 days / 90+ days). Bread emoji 🍞 for stale indicator.
- **Why it failed:** During testing, no deals were listed in any time period. The `expectedCloseDate` on open deals was not reliably past-due. Closed deals have no `expectedCloseDate` at all.
- **Debug finding:** `expectedCloseDate` is available on all open opportunities, but it is future-dated or not far enough in the past to trigger "overdue" in the test data.

### Why it was scrapped
- Both approaches produced empty results in real-world testing. The tile was non-functional.
- User explicitly asked to scrap it and document the attempts.
- All stale deals code removed from `app.js`, `index.html`, and `styles.css`.

### Future possibilities
- A proper CRM-side query for "last modified" timestamp (not available in current API).
- A different staleness metric (e.g., "no note added in 30 days" using a dedicated endpoint).
- Revisit if CRM API provides opportunity `updatedAt` or `lastActivityDate` in the future.

---

## ISSUE-003 — Feed notification notify-user search: auto-inject [Notified:] attempted and scrapped

**Status:** ❌ Abandoned 2026-06-12 — reverted in v1.7.6
**Area:** Feed notifications / event creation (`public/app.js`)

### What was tried

We wanted to make "My notifications" (events where the current user was in notifyUserList) searchable via the keyword filter using `@username`. Since the CRM's GET `/api/2.0/crm/history/filter` does not return notifyUserList in its response, we couldn't filter client-side by user.

**Attempt A (v1.7.6-dev, reverted):** Auto-inject a `[Notified: Name1, Name2]` suffix into the event content inside `createOpportunityHistoryEvent`, right before the HTML conversion. Then detect the suffix in `renderFeedNotificationItem` and style it separately. Users would search `@ken` as a keyword and find events where they were notified.

**Why it was reverted:**
- User reported the [Notified:] text appeared "squished against the note" and was the same font size despite CSS rules — possibly a browser-cache issue, but the styling approach was unreliable.
- The user preferred the existing manual `@ken` keyword filter which already worked for events where their name appeared in the text.

**Alternative considered:** A dedicated "My notifications only" checkbox that would try to check `notifyRecipients` — but the API doesn't return this data in GET responses, so it hid everything. Also reverted.

**Current state:** Only the manual keyword filter (comma-separated AND) is used for feed filtering. Mail events removed from feed (noise reduction). FEED_MAX_EVENTS=150.

## ISSUE-001 — New Opportunity: custom user fields do not persist (RESOLVED v1.7.0)

**Status:** ✅ Fixed 2026-06-11 — `CREATE_OPP_USER_FIELDS_ENABLED=true`, see **Root cause** below.  
**Priority:** High  
**Area:** Create opportunity modal (`public/app.js`)

### Root cause (two bugs found)

1. **DOM query selector bug** (primary — fields were never sent): `collectCreateOppCustomFieldValues()` used `wrap.querySelector('[data-custom-field-id="..."]')` which matched the wrapper `<div>` (set in `renderCreateOppCustomFields` via `field.dataset.customFieldId`) instead of the actual `<input>`/`<select>`/`<textarea>`. Accessing `.value` on a `<div>` returned `undefined` → empty string → `if (!raw) continue;` — every field was silently skipped and `customFieldList` was absent from the create body.

2. **JSON payload format** (secondary — caused 400 errors when fields were present): `buildCustomFieldListForApi()` was returning `{Key, Value}` (PascalCase) objects. Combined with flat `customField_{id}` fields, this caused the CRM's `DeserializeXNode` to produce duplicate XML sibling nodes, triggering "Input string was not in a correct format" / "Value does not fall within the expected range".

### Fix (v1.7.0)

- `collectCreateOppCustomFieldValues()`: finds the wrapper div by `[data-custom-field-id]`, then uses `fieldEl.querySelector("input, select, textarea")` to get the actual input with the user's value.
- `buildCustomFieldListForApi()`: returns `{key, value}` (camelCase) only — no duplicate `Key`/`Value` props.
- `buildOpportunityCreateBody()`: includes `customFieldList` with `{key, value}` format; no flat `customField_{id}` loop.
- Per-field POST (`POST .../customfield/{fieldId}?fieldValue=...`) works correctly as fallback.
- Tested end-to-end: dashboard → proxy → CRM. Created opps with text/select/date/checkbox fields; values persist in native CRM.

## ISSUE-004 — Dashboard UI freeze / hang when returning from background tab

**Status:** ✅ Fixed 2026-06-14 — implemented in `public/app.js` (see below)
**Priority:** Medium
**Area:** Performance / IntersectionObserver / browser tab throttling (`public/app.js`)

### Summary

User reports the dashboard becomes unresponsive or "hangs" when the tab has been in the background for a while and the user returns to it. The issue does not occur during active editing.

### Root cause identified

1. **Browser tab throttling**: When a tab is backgrounded, Chrome aggressively throttles `setInterval` and `setTimeout`. All deferred callbacks fire simultaneously when the tab returns to the foreground.

2. **IntersectionObserver burst**: Each group tile has an `IntersectionObserver` on its opportunity cards. When the tab returns to the foreground, the browser recalculates all intersections, which can trigger up to `OPP_CUSTOM_FIELD_ENRICH_CONCURRENCY` (5) concurrent `fetchOpportunityCustomFields()` calls per group. With 5+ groups, this creates 25+ simultaneous API calls plus DOM rebuilds, causing a brief UI freeze.

3. **Not the mutation queue**: The `processMutationQueue()` (5s interval) was suspected, but it does almost nothing when the queue is empty (which is the normal case). The hang occurs even with zero queued mutations.

### Proposed fix

1. Debounce the intersection observer callback across all groups (batch enrich queue instead of per-group).
2. Skip `fetchOpportunityCustomFields()` entirely when `document.visibilityState === 'hidden'`.
3. Add a `document.visibilitychange` listener to pause and resume the intersection observer.
4. Optionally, add a small delay (e.g., 500ms) after tab visibility returns before re-enabling observers, to let the browser settle.

### Fix applied

1. **Batch observer callbacks** — `observerBatch` (Map groupId → Set oppIds) collects all intersecting cards in a 50ms debounce, then flushes once via `flushObserverBatch()` instead of firing per-card.
2. **Skip when hidden** — `enqueueOpportunityCustomFieldEnrich` returns immediately if `document.visibilityState === 'hidden'`; observer callback also bails early.
3. **Pause/resume on visibilitychange** — When the tab is hidden, all group observers are disconnected and `groupCardObservers` is cleared. When the tab returns, a 500ms delay lets the browser settle before re-observing all visible groups.
4. **Result** — Eliminates the burst of 25+ concurrent API calls + DOM rebuilds that caused the freeze.

### Files changed

| File | Role |
|------|------|
| `public/app.js` | `observeOpportunityCardsInGroup`, `enqueueOpportunityCustomFieldEnrich`, `drainOppCustomFieldEnrichQueue`, `flushObserverBatch`, visibilitychange listener |
| `public/styles.css` | (none expected) |

---

## ISSUE-002 — CRM Mail: mark read/unread, account selector, unread badge, and linking

**Status:** ✅ Re-enabled in v1.7.0 — server API calls for mark/unread, delete, and link-to-deal now work. Account selector remains disabled (unified inbox only). See **Root cause** below.  
**Priority:** Medium  
**Area:** Mail inbox modal (`public/app.js`, `public/index.html`), mail history linking

### Summary

The mark read/unread toolbar buttons and delete button were viewer-only (no server push). Linking to deals existed but used a fallback history event. The root cause was using message-level endpoints (`POST /api/2.0/mail/messages/markread`) which returned HTML errors — the native CRM operates on **conversations** (not individual messages). The dashboard list was loaded from `/api/2.0/mail/messages` which returned message objects without `conversationId`, making it impossible to use conversation-level APIs.

The account selector pulldown remains disabled (unified inbox only). The unread badge indicator works but may reflect local state combined with server data.

### Root cause

All mail operations in the native OnlyOffice CRM (mark read/unread, delete, link to CRM) use **conversation-level** endpoints, not message-level:

| Operation | Endpoint | Body |
|-----------|----------|------|
| Mark read/unread | `PUT /api/2.0/mail/conversations/mark.json` | `ids[]=<convId>&status=read\|unread` |
| Delete (move to trash) | `PUT /api/2.0/mail/conversations/move.json` | `ids[]=<convId>&folder=4` |
| Link to CRM | `PUT /api/2.0/mail/conversations/crm/link.json` | `{"id_message":<convId>,"crm_contact_ids":...}` |

The dashboard previously loaded `/api/2.0/mail/messages` which returned individual message objects without `conversationId`, so conversation IDs were never available. Additionally, mark/delete handlers attempted `POST /api/2.0/mail/messages/markread` which returned HTML errors (endpoint does not exist at message level in this Community Server version).

### Fix (v1.7.0)

1. **Switched list endpoint**: `loadMailMessagesForModal` now loads `/api/2.0/mail/conversations.json?folder=1&page_size=...&sort=date&sortorder=descending` instead of `/api/2.0/mail/messages`. Conversation objects have compatible fields (`id`, `subject`, `from`, `date`, `read`) so `renderMailList` works without changes.

2. **Mark read/unread**: Handlers now `PUT /api/2.0/mail/conversations/mark.json` with form-urlencoded `ids[]=<id>&status=read|unread`. Local `mailDashboardReadIds` Set preserved as fallback if the server call fails.

3. **Delete**: Button shown (was `display:none`). Handler now `PUT /api/2.0/mail/conversations/move.json` with `ids[]=<id>&folder=4` (trash). Includes confirmation dialog.

4. **Link to deal**: Sidebar restored (replaces the viewer-warning sidebar). Uses existing `POST /api/2.0/mail/crm/link` endpoint with conversation IDs as `messageIds` + fallback history event. The `resetQuickLinkSidebar()` function clears state on modal open.

5. **Expand (View)**: Changed from `fetchMailMessage(id)` (message-level) to loading conversation detail via `/api/2.0/mail/conversation/{convid}.json?loadAll=false` and extracting the first message for `renderMailEmbedPanel`.

6. **Local persistence**: `mailDashboardReadIds` Set + localStorage preserved for read status resilience. `markMailMessageRead` (auto-mark on expand) now also pushes to server (best-effort, non-blocking).

### Files changed (v1.7.0)

| File | Role |
|------|------|
| `public/index.html` | Restored link sidebar HTML (`.mail-right-sidebar`); removed viewer-warning sidebar; delete button visible |
| `public/app.js` | `loadMailMessagesForModal` → conversations API; mark/unread/delete handlers → server API; expand → conversation detail; `resetQuickLinkSidebar()` added; viewer-warning references removed; `markMailMessageRead` pushes to server |
| `ISSUES.md` | This update |

### What remains

- Account selector pulldown still disabled (unified inbox). Re-enabling would need a working `/api/2.0/mail/accounts` → folder list → filter by `folderId`.
- Badge indicator uses a mix of server folder counts and local overrides; may not perfectly reflect native CRM unread state.
- Link sidebar searches only opportunities (no contacts or other entity types).
- Delete moves to trash (folder 4); does not permanently delete.

### References

- Conversations list: `GET /api/2.0/mail/conversations.json?folder=1&page_size=...&sort=date&sortorder=descending`
- Conversation detail: `GET /api/2.0/mail/conversation/{id}.json?loadAll=false`
- Mark: `PUT /api/2.0/mail/conversations/mark.json` (form-urlencoded `ids[]=<id>&status=read|unread`)
- Delete: `PUT /api/2.0/mail/conversations/move.json` (form-urlencoded `ids[]=<id>&folder=4`)
- Link primary: `POST /api/2.0/mail/crm/link` (messageIds + crmEntityId + crmEntityType:2); fallback history POST
- Accounts/folders for badge: `/api/2.0/mail/accounts`, `/api/2.0/mail/folders?accountId=...`
- History mail parse: `parseHistoryMailPayload`, `isMailLinkedHistoryEvent`, `extractMailMessageIds`, `crmMailReceivedLine`, `renderMailHistoryReceivedSummary`
- See also CHANGELOG / RELEASE notes for v1.7.0 context

---

## ISSUE-005 — Deal caching: stale data after edits and refresh

**Status:** ✅ Fixed 2026-06-14 — v1.8.1
**Priority:** High
**Area:** Dashboard caching (`public/app.js`, `server.py`)

### Summary

After editing a deal (tags, notes, due date) and clicking the tile refresh button or the global Refresh All, the dashboard still showed stale data. The user confirmed the changes were saved in the native CRM, but the dashboard didn't reflect them.

### Root cause

**Client-side (IndexedDB):** `refreshAll()` called `state.filterResultCache.clear()` and then `await hydrateAllCachesFromIndexedDB()`. The `clear()` method triggers `clearIndexedDBStore()` fire-and-forget (not awaited), while hydration reads from IndexedDB before the clear transaction commits. This repopulated the in-memory cache with stale data.

**Server-side (proxy cache):** `server.py` caches GET responses for 30 seconds (`/crm/opportunity/{id}`) and 600 seconds (tags, custom fields). The invalidation logic only handled `PUT`/`POST`, not `DELETE`, and didn't invalidate the single-opp cache line on tag changes.

### Fix

1. `refreshAll()` no longer calls `hydrateAllCachesFromIndexedDB()` — hydration only happens on initial load via `initCaches()`.
2. `enableCachePersistence()` made idempotent (prevents double-wrapping).
3. `refreshGroup({ force: true })` now explicitly clears the filter result cache entry before fetching.
4. Server-side: Added `DELETE` to mutation invalidation, and tag changes now invalidate the single-opp cache line.
5. Preview modal refresh button (`⟳`) appends `?_t=Date.now()` to bypass the proxy cache entirely.

### Files changed

| File | Role |
|------|------|
| `public/app.js` | `initCaches()`, `refreshAll()`, `refreshGroup()`, `enableCachePersistence()` idempotency, `bustCache()` helper |
| `server.py` | Cache invalidation for `DELETE`, single-opp cache invalidation on tag changes |

---

## ISSUE-006 — Amber border lost after deal edit

**Status:** ✅ Fixed 2026-06-14 — v1.8.1
**Priority:** High
**Area:** Card rendering / tag enrichment (`public/app.js`)

### Summary

After editing a deal (e.g., adding a note via quick note or deal edit), the card in the group tile lost its amber border and background. The "High Priority" tag was still present on the deal in native CRM, but the dashboard card no longer showed the styling.

### Root cause

`fetchOpportunityForUpdate()` returns the core opportunity object but does **not** include the `tags` property. The targeted single-opp refresh path in `submitDealEditForm()` and `submitQuickNoteForm()` updated the in-memory opportunity with the untagged object, then called `renderCard()`. Since `oppHasTag(opp, "High Priority")` found no tags, the `.card--high-priority` class was not applied.

### Fix

Added `await enrichOpportunitiesTags([updatedOpp])` immediately after `fetchOpportunityForUpdate()` in both submit paths. This fetches tags for the updated opportunity before rendering the card, preserving the amber border/background styling.

### Files changed

| File | Role |
|------|------|
| `public/app.js` | `submitDealEditForm()`, `submitQuickNoteForm()` — tag enrichment after fetch |

---

## ISSUE-007 — Attachment note with no failure indicator

**Status:** ✅ Fixed 2026-06-14 — v1.8.1
**Priority:** High
**Area:** Note submission / mutation queue (`public/app.js`)

### Summary

A note with a PDF attachment submitted on a deal tile did not appear in the CRM. The user did not see any error indicator or failure message. The note was silently queued for retry but the UI showed "Note saved" as if it succeeded.

### Root cause

`createOpportunityHistoryEvent()` used `withCrmQueueOnTransient()` which queues the mutation on transient errors and returns `{ queued: true }`. The callers (`submitDealEditForm()`, `submitQuickNoteForm()`) did not check the return value, so they always showed the success toast regardless of whether the note was actually sent or queued.

### Fix

1. `createOpportunityHistoryEvent()` now explicitly returns `{ queued: true }` or `{ success: true }`.
2. Callers check the return value and show "Note queued for retry (CRM is temporarily down)" when appropriate.
3. The note queue indicator in the header was made more visible (amber background, larger font).

### Files changed

| File | Role |
|------|------|
| `public/app.js` | `createOpportunityHistoryEvent()`, `submitDealEditForm()`, `submitQuickNoteForm()` |
| `public/styles.css` | Note queue indicator visibility |

---

## ISSUE-008 — Preview modal manual refresh returns stale data

**Status:** ✅ Fixed 2026-06-14 — v1.8.1
**Priority:** Medium
**Area:** Preview modal / server proxy cache (`public/app.js`)

### Summary

Clicking the manual refresh button (`⟳`) in the preview modal still showed stale data even after edits were confirmed in native CRM. The user had to wait ~30 seconds for the server proxy cache to expire.

### Root cause

The preview modal refresh button re-fetched the same API paths without any cache-busting parameter. The server-side proxy cache (`_proxy_cache`) stores GET responses for 30 seconds (opportunity data, history) and 600 seconds (tags, custom fields). So the manual refresh was returning the cached stale response.

### Fix

Added `bustCache(path)` helper that appends `?_t=Date.now()` to any API path. The preview modal refresh button now passes `force=true` through the entire chain: `openOpportunityPreviewModal(…, true)` → `fetchOpportunityPreviewData(…, true)` → each individual fetch function (`fetchOpportunityForUpdate`, `fetchOpportunityCustomFieldValues`, `fetchAllOpportunityHistory`, `loadDealEditTags`, `fetchOpportunityDocuments`).

### Files changed

| File | Role |
|------|------|
| `public/app.js` | `bustCache()`, `fetchOpportunityForUpdate()`, `fetchOpportunityCustomFieldValues()`, `fetchAllOpportunityHistory()`, `loadDealEditTags()`, `fetchOpportunityDocuments()`, `fetchOpportunityPreviewData()`, `openOpportunityPreviewModal()`, preview refresh button |

---

## ISSUE-009 — Changelog popup on first login after update

**Status:** ✅ Implemented 2026-06-14 — v1.8.1
**Priority:** Low
**Area:** UX / onboarding (`public/app.js`, `public/index.html`, `public/styles.css`, `server.py`)

### Summary

New feature: Show a modal with the changelog content when the user logs in after a version update. The modal should only appear once per version — if the user closes it, they don't see it again until the next version update.

### Implementation

1. **Server:** `GET /api/changelog` endpoint reads `CHANGELOG.md` and returns it as `text/markdown`.
2. **Client:** After login (or session restore), the app fetches `/api/config` to get the current version, compares it with `localStorage.getItem("changelog_seen_version")`, and shows the modal if different.
3. **Rendering:** Uses the existing `renderBasicMarkdown()` function (headers, lists, bold, italic, links, code, etc.).
4. **Dismissal:** Close button, Escape key, or backdrop click. Saves the seen version to `localStorage`.
5. **Defer:** 300ms delay so the dashboard shell paints first.

### Files changed

| File | Role |
|------|------|
| `public/index.html` | Modal markup (`#changelog-modal`, `#changelog-body`, `#changelog-close`) |
| `public/app.js` | `showChangelogModal()`, `closeChangelogModal()`, `maybeShowChangelog()`, `bindChangelogModal()` |
| `public/styles.css` | `.modal-card-changelog`, `.changelog-body`, `.changelog-version`, markdown typography |
| `server.py` | `GET /api/changelog` endpoint |
| `CHANGELOG.md` | Updated with v1.8.1 release notes |
| `docs/RELEASE_v1.8.1.md` | Release notes file |

---

## ISSUE-010 — Customer Bot employee mode: CRM mail API quirks and workarounds

**Status:** ✅ Documented / implemented 2026-06-29 — v2.0.5
**Priority:** Medium
**Area:** Customer Bot employee mode (`server.py`, `telegram_bot.py`)

### Summary

When building employee-mode deal detail for the Telegram bot, we need the full text of linked email history events. The CRM `/api/2.0/crm/history/filter` endpoint only returns a truncated mail summary (often just a `mailto:` link), so we had to find an alternate source and handle several OnlyOffice API quirks.

### API quirks discovered

1. **CRM history stores truncated mail content.**
   - Mail events returned by `/api/2.0/crm/history/filter` have `category.title` = "Mail Message" and `content` is a JSON string.
   - The JSON contains `from`, `to`, `subject`, and an `introduction` field, but `introduction` is truncated (e.g. ends with `[ ](mailto:notifications@grasshopper.co…`).
   - The full email body is **not** present in the history response.

2. **`/api/2.0/mail/messages/{id}` does not return the rendered email body.**
   - The history event contains a `messageId`/`mailMessageId` (e.g. `27639`).
   - Calling `/api/2.0/mail/messages/{id}` with that ID returns a mail object, but the body fields (`htmlBody`, `textBody`, `body`) were empty/unusable in production.
   - This endpoint is used by the dashboard preview modal for metadata, but it is **not** the endpoint the native CRM MailViewer uses to render the message body.

3. **The CRM MailViewer uses a legacy ASPX handler for the body.**
   - Native CRM opens emails with `/Products/CRM/MailViewer.aspx?id={some_id}`.
   - That page fetches the body via `/Products/CRM/HttpHandlers/filehandler.ashx?action=mailmessage&message_id={messageId}`.
   - The `message_id` query parameter is the same `messageId` from the history event (e.g. `27639`), **not** the `id` shown in `MailViewer.aspx?id=...` (e.g. `38596`). These are two different IDs.

4. **Bot account needs admin + mail access (historical OnlyOffice sync).**
   - The bot authenticated as a dedicated CRM admin account and called the filehandler with an `Authorization: <token>` header.
   - Initially the bot account was a regular user and the filehandler returned no body / permission errors.
   - Making the bot account an OnlyOffice admin and enabling mail module access for it allowed the filehandler to return full email HTML.

5. **Email addresses in angle brackets break Telegram HTML parse mode.**
   - Forward/reply attributions contain text like `"Grasshopper" <notifications@grasshopper.com>`.
   - Telegram's HTML parser treats `<notifications@grasshopper.com>` as an unsupported start tag and rejects the message with: `Can't parse entities: unsupported start tag "notifications@grasshopper.com"`.
   - Fix: escape `<` and `>` in attribution lines before sending.

6. **CRM mail bodies are raw HTML with tables and wrapped text.**
   - The filehandler returns HTML with `<table>` headers for forwarded messages and hard-wrapped lines inside `<p>` tags.
   - Direct sanitization leaves broken words (`payment of$432 ,851.60`) and leading whitespace.
   - Fix: convert HTML to plain text first, flatten tables to `Header: Value` lines, collapse whitespace, then sanitize for Telegram.

7. **History event author lives in `createBy`/`createdBy`.**
   - The event creator is in `ev.createBy.displayName` (or `createdBy`), not a top-level `author` field.
   - Employee mode now extracts this and shows it for non-mail events. It is intentionally hidden for `Customer Update` events.

### Current implementation

- `server.py` `_extract_mail_message_id()` finds the mail message ID from nested objects, top-level fields, `additionalData`, or regex.
- `server.py` `_fetch_full_mail_body()` calls the ASPX filehandler, parses JSON/HTML, and replaces the truncated `introduction` in the event content.
- `telegram_bot.py` `_html_to_text()` converts CRM email HTML to tight plain text.
- `telegram_bot.py` `_extract_forward_info()` / `_clean_reply_attribution()` handle forwarded/reply headers.
- Mail bodies are capped at 1200 characters and end with `[truncated]`.
- Plain-text fallback (`_strip_html_tags()`) strips tags if Telegram rejects HTML.

### Files changed

| File | Role |
|------|------|
| `server.py` | `_extract_mail_message_id()`, `_fetch_full_mail_body()`, `_extract_event_author()` |
| `telegram_bot.py` | `_html_to_text()`, `_extract_forward_info()`, `_clean_reply_attribution()`, `_truncate_html()`, `_strip_html_tags()`, `_format_mail_event()` |

---

## ISSUE-012 — Server process dies when backgrounded from opencode shell

**Status:** ⚠️ Known issue — workaround in place  
**Priority:** Low (developer experience only — does not affect production)  
**Area:** `start.sh`, `server.py`, opencode bash tool

### Summary

When the server is started via `nohup python3 server.py &` inside the opencode bash tool, the process is killed when the bash tool's shell session ends (timeout or next command). This is because the opencode shell sends SIGHUP to all process groups when the session closes, and `nohup` alone does not prevent this.

### Workaround

Use `setsid` to fully detach the process from the shell's process group:

```bash
cd /home/chapman-server/Projects/new-crm && setsid .venv/bin/python3 server.py > /tmp/server.log 2>&1 &
```

This works because `setsid` creates a new session for the process, so it is not affected by SIGHUP from the parent shell.

### Impact

- **Development only**: Production uses `docker compose up -d` which handles process lifecycle properly.
- Server appears to start (PID exists, port opens) but dies within seconds, causing "connection refused" from LAN clients.
- No crash log — the process simply vanishes.
---

## ISSUE-015 — Outlook SMTP send disabled by Microsoft (`5.7.139`)

**Status:** ✅ Workaround shipped 2026-09-17 (Microsoft Graph sendMail).  
**Priority:** High (blocks sending from any affected Outlook account).  
**Area:** `smtp_client.py`, `oauth_providers.py`, `server.py`, `scanner/mail_scanner.py`.

### Summary

When Microsoft disables SMTP AUTH on a mailbox, `smtp_client.send_email_from_account()` returns the explicit SMTP server response `535 5.7.139 SmtpClientAuthentication is disabled` (or `SMTPAuthenticationError` with the same message). This is a per-mailbox capability disable, not a credential issue — a fresh OAuth token does not help, and Microsoft support refuses to re-enable SMTP AUTH on consumer (outlook.com / hotmail.com / live.com / msn.com) accounts because the disable is intentional and irreversible via support channels.

Affected symptoms:
- Send from Outlook OAuth account returns the `5.7.139` error every time, regardless of token freshness.
- Microsoft support's standard response is "use Outlook web" or "contact your tenant admin" — neither helps personal accounts.
- Previously the only workaround was to switch the account to app-password authentication, which Microsoft also disabled on many consumer accounts.

### Fix (2026-09-17): Microsoft Graph `sendMail`

Use the Microsoft Graph API's `POST /me/sendMail` endpoint with delegated `Mail.Send` scope instead of SMTP AUTH. Graph sendMail is a separate code path from SMTP AUTH; the `5.7.139` disable does not affect it.

Because the v2.0 token endpoint mints one access token with one audience, Graph sendMail needs a separate OAuth flow from the existing Exchange OAuth (IMAP/SMTP). Two-token model:

- **Exchange token** (existing, in `oauth_*` columns): scope `offline_access openid email https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send`. Used for IMAP sync and SMTP send (XOAUTH2).
- **Graph token** (new, in `oauth_graph_*` columns): scope `offline_access https://graph.microsoft.com/Mail.Send`. Used for `POST /me/sendMail`.

**Routing** (in `mail_send.py:try_send_for_account`):
1. Microsoft OAuth + Graph token → Graph first.
2. Graph failure → fall back to SMTP.
3. SMTP `5.7.139` + Graph token available → retry Graph.
4. Google OAuth / app-password / manual SMTP → SMTP only (unchanged).

**Re-consent**: every existing Microsoft OAuth account must reconnect via "Connect Graph" in the account settings modal to grant the `Mail.Send` scope. The Compose Email modal shows an amber banner at the top when the active from-account is MS OAuth without a Graph token.

### Files changed

| File | Role |
|---|---|
| `migrations/019_add_mail_graph_token_columns.sql` (new) | Adds `oauth_graph_*` columns |
| `init.sql` | Mirrors the new columns for fresh installs |
| `mail_graph_client.py` (new) | Stdlib-only `send_email_via_graph()` posting to Graph |
| `mail_send.py` (new) | Shared routing helper used by server + scanner |
| `oauth_providers.py` | Adds `MicrosoftProvider._GRAPH_SCOPE`, `graph_authorize_url`, `graph_exchange_code`, `graph_refresh_token` |
| `server.py` | New Graph OAuth routes (`authorize` / `callback` / `disconnect`), `_refresh_graph_token_if_needed`, `_try_send_for_account`, `graphStatus` field in `_handle_mail_accounts` response |
| `scanner/mail_scanner.py` | Reply to Email custom behavior uses the shared helper; new `_build_graph_token_refresher()` closure |
| `public/index.html`, `public/app.js`, `public/styles.css` | Account Settings "Microsoft Graph Send" block + amber banner in Compose |
| `docs/OAUTH_SETUP.md` | Documents the two-consent flow |

### Trade-offs

| Concern | Impact |
|---|---|
| Attachments >25MB total | Graph send fails; falls back to SMTP if possible |
| Rate limit (20 req/min/user) | Matters only for high-volume senders; falls back to SMTP |
| Tenant admin consent | Required for `Mail.Send` on managed tenants; rare |
| Refresh token revocation | If the Graph token is revoked, falls back to SMTP and surfaces the error |


## ISSUE-016 — Kanban: per-column vertical scroll + click-drag horizontal scroll + card-content wrap (2026-10-08)

**Status:** ✅ Fully Fixed 2026-10-08 (5 attempts over 2 days, plus 2 follow-up commits for auto-hide scrollbars and card content wrap).  
**Priority:** Medium (visual UX only; no data loss)  
**Final area of changes:**
- `public/styles.css` — base `.board` / `.column` / `.column-body` rules at lines 700-825; `.dashboard-tile.local-kanban-tile` rules at 1964-2007; `.dashboard-tile.board-group-tile:not(.personal-notes-tile)` rules at 2009-2023; global subtle scrollbar rules at lines 31-100; `.card` overflow-wrap at lines 810-832
- `public/app.js` — new `enableBoardDragScroll(boardEl)` helper at line 11196; `setupAutoHideScrollbars` IIFE at line 5
- `public/index.html` — cache busters through `styles.css?v=3.7.14` and `app.js?v=3.7.9`

### Summary

User wanted three things across the kanban tiles (both the deal-level "Tagged deals" type tiles AND every main page-wide project group kanban):
1. **Per-column vertical scroll** — each column has its own scrollbar, the column is clipped to the tile border, the column-body's max-height scales with the tile when the user resizes.
2. **Click-and-drag horizontal scroll** on the board for mouse (mirroring the existing mobile touch-pan), with a guard so link clicks inside cards still navigate normally.
3. **Card content stays inside the card border** — long URLs and long single words in card descriptions should wrap, not break out of the card.

Plus, after the kanban was working, the user asked to **auto-hide the scrollbars** so they're only visible on the element being scrolled or hovered (like Slack, Linear, GitHub).

### Commits (chronological, 8 total)

#### Part A — per-column vertical scroll + click-drag (5 attempts)

- **2026-10-06 attempt 1** (`d9dbfae`) — replaced `.column { max-height: calc(100vh - 280px) }` with `max-height: none; min-height: 0`. Column overflowed the tile border; no per-column scroll.
- **2026-10-06 attempt 2** (`512e8f6`) — added `max-height: min(70vh, 800px); overflow: hidden` on `.dashboard-tile.local-kanban-tile`. Capped the deal-level tile. Scoped to deal-level only; main page-wide kanban untouched.
- **2026-10-06 attempt 3** (`357b23e`, reverted `f708cc1`) — added `align-items: stretch` + `overflow-y: hidden` on the board, `min-height: 0` on the column. Scoped to deal-level. User reported still broken — column content still overflowed the tile border.
- **2026-10-07 attempt 4** (`960bed0`, reverted `6c485b3`) — used a **viewport-based** `max-height: calc(100vh - 240px)` on the base `.column-body`. Click-and-drag worked, per-column scroll worked, but the column-body's max-height did NOT scale with the tile — when the user resized a tile taller than viewport-240px, the column-body stayed at viewport-240px, leaving empty space below the last card while the tile was much taller. User: "the column height max isnt growing with the tile height."
- **2026-10-07 attempt 5 (`b9ef269`)** — the correct fix. Replaced the viewport-based `max-height` with `max-height: 100%` on the column-body (caps to the column's available height, which scales with the tile). Set `align-items: stretch` on the board so the column fills the board's cross-axis height. Column stays content-tall when content is shorter than the column (per user: "Its ok if there is empty space underneath the column if there is less cards").

#### Part B — auto-hide scrollbars (3 attempts)

- **2026-10-07 attempt 6** (`2b21c7c`, reverted `4cec083`) — CSS-only: `background: transparent` on `::-webkit-scrollbar-thumb` + `*:hover::-webkit-scrollbar { width: 8px }`. Failed because on WebKit/Blink, `background: transparent` on the thumb doesn't actually hide the scrollbar — Chromium still paints the thumb. User: "scroll bars are thicker and more visible than ever."
- **2026-10-07 attempt 7** (`62e8ee8`, reverted `a8e4770`) — JS scroll listener adding `is-scrolling` class to `body` + CSS rules to show scrollbar when body has the class. Failed because the class was on body, which made ALL scrollbars visible when ANY scroll happened. User: "Arent they supposed to only become visible on the element that the mouse is scrolling on?"
- **2026-10-07 attempt 8 (`9895916`)** — the correct approach. Per-element classes: `is-scrolling` added to the SPECIFIC scrolled element (via `e.target` on the scroll event); `is-hovered` added to the SPECIFIC scrollable ancestor of the cursor (via pointerover + walk-up to find the first scrollable element). CSS uses the standardized `scrollbar-color: transparent` (works in both overlay and classic scrollbar modes) rather than `::-webkit-scrollbar { width: 0 }` (which forces classic mode and still paints the scrollbar). The thumb becomes visible only on the element with the class.

#### Part C — card content wrap

- **2026-10-08 (`98ad9e1`)** — single CSS edit: added `overflow-wrap: break-word;` to the base `.card` rule. The `overflow-wrap` property is inherited, so all child text elements (`.card-title`, `.card-meta`, `.card-details`, `.card-description`, `.card-contact`) get the wrap behavior. Long URLs and long single words now wrap inside the card border instead of breaking out.

### Why each phase of attempts failed

#### Phase A (per-column scroll)

The fundamental tension: per-column scroll needs the column-body to have a known height (so `overflow-y: auto` has something to scroll against), but the user wants the column to:
- Not stretch to fill the board (would make short columns show empty space at the bottom — user said "do not stretch")
- Scale with the tile when the tile is resized (no static cap)

The resolution (attempt 5): **let the column stretch to the board** (via `align-items: stretch`), but **let the column-body stay content-tall inside the column** (via `flex: 0 1 auto` + `max-height: 100%`). The column-body is capped to 100% of the column's height — which scales with the tile. When content is shorter than the column, the column-body is just that tall (no stretch). When content exceeds the column, the column-body scrolls.

Attempts 1-3 failed because the column was content-tall (no flex chain forcing it to a known height), so the column-body's `overflow-y: auto` had nothing to scroll against. Attempt 4 used a viewport-relative max-height which didn't scale with the tile.

#### Phase B (auto-hide scrollbars)

The core mistake across attempts 6-7 was using `::-webkit-scrollbar { width: 0 }` (attempt 6) or `body.is-scrolling` (attempt 7) to hide the scrollbar. Per [Chrome's scrollbar styling docs](https://developer.chrome.com/docs/css-ui/scrollbar-styling): "when you set the `width` or `height` of `::-webkit-scrollbar`, an overlay scrollbar is always displayed, effectively turning it into a classic scrollbar." Once the scrollbar is classic, it's always painted regardless of width: 0.

The correct approach (attempt 8): use the standardized `scrollbar-color: transparent transparent` property, which makes the thumb invisible in BOTH overlay and classic modes without forcing classic. Combined with per-element JS classes (`.is-scrolling` on the scrolled element, `.is-hovered` on the scrollable ancestor of the cursor), only the specific element's thumb becomes visible.

#### Phase C (card content wrap)

The base `.card` rule had no `overflow-wrap` or `word-break` properties. Long URLs (which have no natural break points) overflowed the card's right border. Adding `overflow-wrap: break-word` to `.card` causes the browser to break long words/URLs at any character when they would otherwise overflow. The property is inherited, so all child text elements get the wrap behavior.

### Final fix details

**CSS (`public/styles.css`):**

- **Base `.board` (line 700):** `align-items: stretch` (was `flex-start`); `overflow-x: auto; overflow-y: visible`; `cursor: grab`; `:active { cursor: grabbing }`.
- **Base `.column` (line 711):** `overflow: hidden` backstop; `.column-header` has `flex-shrink: 0` so it stays at natural height.
- **Base `.column-body` (line 770):** `flex: 0 1 auto` (content-tall, not stretched); `max-height: 100%` (caps to column's available height, scales with tile); `min-height: 80px`.
- **`.dashboard-tile.local-kanban-tile` (line 1964):** removed the `max-height: min(70vh, 800px)` tile cap; kept `overflow: hidden`. The board inside has `overflow-x: auto; overflow-y: hidden`.
- **`.dashboard-tile.board-group-tile:not(.personal-notes-tile)` (line 2009):** NEW rules for the main page-wide kanban. Same pattern.
- **Global scrollbar rules (lines 31-100):** standardized `scrollbar-width: thin; scrollbar-color: transparent transparent` on `*` (inherited by all elements); `.is-scrolling, .is-hovered { scrollbar-color: rgba(255, 255, 255, 0.28) transparent }`. WebKit fallback: `::-webkit-scrollbar-thumb { background-color: transparent }` (no width/height manipulation).
- **`.card` (line 810-832):** added `overflow-wrap: break-word` and `min-width: 0`.

**JS (`public/app.js`):**

- **`enableBoardDragScroll(boardEl)` helper at line 11196 (~50 lines).** Mirrors mobile touch-pan for mouse. Wired in two places: `renderLocalKanbanTiles` at line 8970 (deal-level, per-board) and `renderBoardGroups` at line 11550 (main page-wide, querySelectorAll sweep). Idempotent via `_dragScrollBound` guard. `e.target.closest("a")` guard preserves link clicks.
- **`setupAutoHideScrollbars` IIFE at line 5 (~80 lines).** Listens for scroll events on `document` (capture phase, passive) → adds `.is-scrolling` to `e.target` with 1s fade-out timer. Listens for `pointerover` → walks up from `e.target` to find the first scrollable ancestor (via `getComputedStyle` checking `overflowY/X === 'auto' || 'scroll'`) → adds `.is-hovered`. Tracks `currentlyHovered` so moving between elements swaps the class. Page-level scrolls coalesce to `body`.

**`public/index.html`:** cache busters through `styles.css?v=3.7.14` and `app.js?v=3.7.9`.

### Verification

- `node --check public/app.js` passes.
- 15/15 Python + 28/28 Node tests still pass.
- **Browser test (manual):**
  1. Open "Tagged deals" tile and any main page-wide project group kanban.
  2. Each column has its own vertical scrollbar when content exceeds the column's available height.
  3. Resize the tile taller (via the height-resize handle) → columns grow with the tile; the column-body's max-height tracks; no empty space inside the column-body when content is short.
  4. Resize the tile shorter → column-body is clipped at the tile border by `overflow: hidden`; content above the clip is visible, content below scrolls.
  5. Click-and-drag horizontally on any board → board slides left/right.
  6. Click on a card-title link → link navigates, no drag.
  7. Scroll a column → only THAT column's scrollbar appears (during scroll + 1s after). Other columns' scrollbars stay hidden.
  8. Hover over a column → its scrollbar appears. Other columns stay hidden.
  9. Card content (long URLs, long words) wraps inside the card border.

---

## ISSUE-017 — Timesheet audit round 3 + 4 (2026-10-15)

**Status:** ✅ Implemented 2026-10-15 across 4 commits (`b3db7b7`, `e576c3a`, `7196e7d`, plus a closing round-5 follow-up)
**Priority:** High (security + correctness) / Medium (UX) / Low (polish)
**Area:** Timesheet / Activity (`server.py`, `notification_dispatcher.py`, `timesheet_overdue_checker.py`, `migrations/032_*`, `public/app.js`, `public/styles.css`, `public/index.html`)

### Summary

Full audit of the timesheet/activity surface: backend routes, frontend UI, `migrations/009`. Found 9 distinct issues across security, correctness, and UX. All fixed across 4 commits, plus a closing round-5 follow-up (this issue) for documentation + the small modal polish.

### Items closed (4 commits)

#### Round 3 (commit `b3db7b7`) — security + UX hardening

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | 🟠 Medium | `_handle_timesheet_clock_out` silently added in-progress break minutes to `break_minutes` and closed the shift when the user clicked Clock Out while still on break. | Now rejects with `400 {"error": "End your break before clocking out"}`. Symmetric with `_handle_timesheet_break_end`. |
| 2 | 🟠 Medium | `/api/timesheet/{history,daily,export,export-pdf}` silently coerced a non-admin's `?user_id=N` request to the caller's own data and returned 200. | All four endpoints now return `403 {"error": "Admin required to view/export other users' timesheets"}` when `user_id` is supplied and differs from the caller. Default (no `user_id` param) behavior unchanged. Matches the existing pattern from `_handle_timesheet_admin_summary`. |
| 3 | 🟡 Low | `tsEntryContextMenu` allowed deleting the active shift (`clock_out IS NULL`). | The Delete item is now `disabled: true` with `cursor: not-allowed`, muted text, opacity 0.6, and tooltip "Clock out before deleting this entry" when the entry has no `clock_out`. |
| 4 | 🟡 Low | `tsEditEntry` and `tsAssignProject` attached a document-level `click` listener for backdrop dismissal that was only removed on the backdrop path. Save/Cancel/Escape closed the popup but left the listener attached. | Refactored both to use a single `teardown()` that removes the document listener AND the popup. All close paths go through `teardown()`. |

#### Round 4 part 1 (commit `e576c3a`) — admin "Open Shifts" tab (Option C)

Per user-pinned design: soft warning only, with an admin-only bulk-close tab. No automated data change for end users.

- New admin tab in the Activity / Time Sheet modal (visible only when `state.currentUser.isAdmin`).
- `GET /api/timesheet/admin/open-shifts` — admin-only. Returns every `time_entries` row with `clock_out IS NULL`, joined with users and opportunities.
- `POST /api/timesheet/admin/close/{id}` — admin-only. Atomic `UPDATE ... WHERE id = %s AND clock_out IS NULL RETURNING ...`. Sets `auto_closed = TRUE`. Folds any in-progress `break_start` into `break_minutes`. Writes a `bot_activity_log` row. Returns 409 on race-loss, 404 on nonexistent.
- Per-row "Close now" (NOW) and "Set time…" (inline datetime-local picker) buttons.
- 30s auto-refresh while the tab is active. Badge on the tab shows the open-shift count.
- New test file: `tests/test_timesheet_audit_admin_tab.mjs` (7 source-grep tests).

#### Round 4 part 2 (commit `7196e7d`) — "clocked in for 10h+" reminder

Per user-pinned design: 10h + 14h, no per-type opt-out (existing global Telegram opt-out still respected), 60s polling interval, INSERT-IGNORE-style unique guard. Drawer row format: headline = time, snippet = question (no repetition).

- New background thread `timesheet_overdue_checker.py`. Polls every 60s, pulls every open shift past 10h, computes per-row elapsed time, INSERTs a `notifications` row with `ON CONFLICT DO NOTHING`. Exposes `start_checker()` / `stop_checker()` / `scan_once()`.
- Migration `032_timesheet_overdue_unique_index.sql`: `CREATE UNIQUE INDEX uq_notifications_timesheet_overdue ON notifications (user_id, type, (payload->>'entry_id'), (payload->>'threshold_hours')) WHERE type = 'timesheet_overdue'`. Partial WHERE keeps other notification types unconstrained.
- `notification_dispatcher.py`: new `_format_timesheet_overdue()` + `elif ntype == "timesheet_overdue":` branch in `_format_notification()`. Uses `_build_tg()` so the visual style matches every other Telegram notification. 10h = "⏰ Forgotten clock-out?" / 14h = "⚠️ Still clocked in". Email formatter also gets a matching branch.
- `public/app.js`: `_notifTypeIcon()` gets a `timesheet_overdue` branch with the Tabler alarm SVG. `_renderNotificationDrawerItem` click handler branches on `n.type === 'timesheet_overdue'` and calls `openActivityModal()` so the user can immediately clock out.
- Two new test files: `tests/test_timesheet_overdue_notifications.mjs` (5 source-grep tests) + `tests/test_timesheet_overdue_checker.py` (5 Python unit tests for `scan_once()`).

#### Round 5 (closing — pending) — modal polish + this doc entry

Cosmetic improvement: the modal's entries list now shows a "● Live" badge on the still-running row, and the row's duration cell shows `Hh Mm Ss` (seconds) instead of `Hh Mm`. The duration updates every second via the existing timer interval. Refactored the elapsed calculation into a shared `_tsComputeActiveElapsed()` helper so the big modal timer and the row duration stay in sync.

### Pre-existing issues surfaced and fixed

These were masked by the dead tile-version code (round 1, `48e29df`) and only became visible after the dead code was removed:

- **Modal range navigation was broken** (day/week/month buttons updated the label but never refreshed the entries list). The dead `tsRefreshEntries` was the only caller of itself; the modal's `tsShiftRange`/`tsSetRange` called it but the function was about to be deleted. Repointed both to `modalRefreshEntries()`.
- **The timer never started** because the dead `tsUpdateClockUI` was the only path that called `tsStartTimer`. After the deletion, nothing started the interval. The modal's `updateModalClockUI` never called the timer starter — it relied on the dead code's side effect.

### Bugs caught during smoke (both fixed before commit)

- **Query threshold off-by-one:** initial SELECT filter used `max(thresholds)` (14h), so an 11h shift never appeared in the result set. Fix: use `min(thresholds)` (10h) in the SELECT, filter per-row.
- **Naive datetime TZ bug:** `clock_in` from `query_dicts` is a naive `datetime`. Calling `.timestamp()` on a naive datetime interprets it as **local time** — wrong on non-UTC servers (the dev environment is on CDT/UTC-5). The elapsed calc was off by the local TZ offset. Fix: tag naive datetimes as UTC before `.timestamp()`. Prod is on UTC so this wouldn't have bitten prod, but the dev environment was broken without the fix.

### Verification

- Live smoke (cleaned up after each test):
  - Non-admin `GET /api/timesheet/admin/open-shifts` → **403** "Admin required".
  - Non-admin `POST /api/timesheet/admin/close/1` → **403** "Admin required".
  - Admin `GET` → **200** with open shift listed.
  - Admin `POST` first close → **200**, `auto_closed: true`. Second close → **409** "Entry was already closed by another admin". Nonexistent entry → **404**.
  - Break-folding: shift with `break_start` 20 min ago, closed via admin → response shows `break_minutes: 19`, `break_start: null`.
  - `bot_activity_log` row written with admin name + clock_in/clock_out.
  - Overdue test: created test user with 11h24m shift → `scan_once()` returned `{'scanned': 1, 'inserted_10h': 1, 'inserted_14h': 0}`. DB row: `message='Clocked in for 11h 24m'`, `payload={'snippet': 'Did you forget to log out?', 'threshold_hours': 10, 'elapsed': '11h 24m', 'entry_id': '6'}`. Idempotent on second call. With a 15h5m second shift: 3 rows total (10h for 11h shift, 10h + 14h for 15h shift).
  - Drawer API returns the rows with the right structure (`type`, `message`, `payload.snippet`, `payload.threshold_hours`).
- Test counts: 11 JS test files (116 tests) + 4 Python test files (28 tests) = **210 tests, 0 failures**.
- `py_compile` ×3 (`server.py`, `notification_dispatcher.py`, `timesheet_overdue_checker.py`), `node --check public/app.js` clean.
- Server restarted with `ts_overdue=ok` in the READY subsystem status.

### Items still open (deliberately)

- **`time_entries_daily` view's `COALESCE(clock_out, NOW())` is still unbounded.** Open shifts still grow the per-day total indefinitely. The admin tab and the overdue notification make the situation actionable (admins can close forgotten shifts, users get a nudge) but the view itself isn't fixed. Three options exist: (a) add a scheduled auto-close job (the design conversation the user originally deferred); (b) filter `clock_out IS NULL` out of `time_entries_daily` until an admin closes them (changes analytics); (c) leave as-is (the view reflects ground truth, the admin tab is the action path). **Current decision: option (c).** Revisit only if analytics depend on it.
- **`_tsActiveEntry` periodic refresh.** If a future admin path closes the user's shift while the user is on a different page, the header button stays in "Clock Out" state until reload. Theoretical today; the only close paths are the user's own clock-out, the admin close handler, and the auto-close notification — all of which require explicit re-fetch.

### Files changed (across all 4 commits)

| File | Role |
|---|---|
| `server.py` | New handlers: `_handle_timesheet_admin_open_shifts`, `_handle_timesheet_admin_close_entry`. New route registrations. 403 enforcement on 4 cross-user endpoints. New start/stop wiring for the overdue checker. |
| `notification_dispatcher.py` | New `_format_timesheet_overdue()` + email branch. |
| `timesheet_overdue_checker.py` (new) | 60s background thread, idempotent INSERT, shared elapsed-math helper. |
| `migrations/032_timesheet_overdue_unique_index.sql` (new) | Partial unique index for de-dup. |
| `public/app.js` | Admin tab renderer + click actions, drawer clock icon + click-to-open-modal, modal "● Live" dot + active-row seconds counter, shared `_tsComputeActiveElapsed()` helper. |
| `public/index.html` | New `<button>` for the Open Shifts tab (hidden by default). |
| `public/styles.css` | New styles for the admin shifts table, the active row, the "● Live" badge. |
| `tests/test_timesheet_audit_round3.mjs` (new) | 5 source-grep tests for the round 3 fixes. |
| `tests/test_timesheet_audit_admin_tab.mjs` (new) | 7 source-grep tests for the admin tab. |
| `tests/test_timesheet_overdue_notifications.mjs` (new) | 5 source-grep tests for the overdue notification. |
| `tests/test_timesheet_overdue_checker.py` (new) | 5 Python unit tests for `scan_once()`. |

---

## ISSUE-018 — Mail sent from the wrong Outlook account (2026-10-29)

**Severity: critical (business-impacting).** Emails sent from the Aplus
account went out as **"MC MDM Construction"** while the CRM recorded
`aplus.estimates@outlook.com`. The recipient saw MDM; our records and the
CRM's Sent list said Aplus. Operational impact on the business.

### Root cause

Graph derives the sender from the **access token**, not from any From
address. `mail_graph_client.send_email_via_graph` sends no `from` property
("`from_addr` is informational only"), and `saveToSentItems: True` writes
the copy into the token principal's Sent folder. So whichever mailbox the
token belongs to is the mailbox that sends.

The Aplus account (`mail_accounts` id 5) held a Graph token belonging to
the **MDM** mailbox. Two independent defects let that happen:

1. **The identity check silently no-opped.** `server.py` had a check on the
   Graph OAuth callback, but it read:
   ```python
   if ms_email and acct_email and ms_email != acct_email:   # refuse
   ```
   `ms_email` came from the token response. The Graph scope was
   `offline_access https://graph.microsoft.com/Mail.Send` — **no `openid`**,
   so the token response carried **no id_token** and the identity could not
   be read. The fallback probe (Graph `/me`) needs `User.Read`, which was
   not granted, and answered **HTTP 403** (verified live). `ms_email` was
   therefore always `""` and the check **never ran**. Proof in the data:
   `oauth_graph_identity` was empty on both Graph accounts.
2. **The account picker was never shown.** `graph_authorize_url` sent no
   `prompt=select_account` and no `login_hint`, so Microsoft silently
   reused the browser's existing Microsoft session. Connecting "Aplus"
   while signed in as MDM minted an MDM token.

A third defect made every reply go through that row: the inline reply used
`accounts[0]`, and `/api/v2/mail/accounts` is `ORDER BY email`, so
`accounts[0]` was **always** the Aplus account regardless of the tab the
message came from.

A fourth defect hid it: `mail_outgoing.from_addr` / `mail_messages.from_addr`
record `acct["email"]` — what we *believe* — not the true sender.

### Fix

- Graph scope gains `openid email profile` (OIDC scopes need no extra
  consent) → the identity is readable from the token.
- Authorize URL gains `prompt=select_account` + `login_hint`.
- The connect callback **fails closed**: unverifiable → refuse (never
  store); mismatch → refuse + record in `oauth_graph_last_error`.
- Every refresh re-verifies the identity (a refresh token is bound to the
  granting mailbox).
- **Send-time guard** in `mail_send.try_send_for_account` — a token that
  belongs to another mailbox, or an unknown identity, is refused BEFORE any
  network call. An SMTP login that differs from the account email is
  refused too.
- Inline reply sends from the message's own account, with an accented From
  selector; the compose modal defaults From to the replied-to mailbox.
- A send-confirmation dialog names the accented sending account and warns
  when the mailbox is not identity-verified.

### Files

| File | Change |
|---|---|
| `oauth_providers.py` | `_GRAPH_SCOPE` + `openid email profile`; `_GRAPH_SCOPE_LEGACY`; `prompt=select_account` + `login_hint` on both authorize URLs; `graph_refresh_token` returns the identity and falls back to the legacy scope on `AADSTS70000`. |
| `server.py` | Fail-closed Graph callback; identity re-verification on refresh; `_record_graph_error`; `sendVerification` in the accounts payload; `sentAs` in the send response. |
| `mail_send.py` | `_graph_identity_state()` + the send-time Graph and SMTP guards. |
| `scanner/mail_scanner.py` | Identity verification in the scanner's token refresher. |
| `public/app.js` | Inline reply uses the message's account + accented From selector; `confirmMailSend()`; `confirmDialog` accepts `messageHtml`; compose From accent. |
| `public/index.html` | Accented From wrapper; cache-bust 3.7.47. |
| `tests/test_graph_identity_guard.py` (new) | 22 tests (17 fail against the old code). |
| `tests/test_graph_oauth_status.py` | `test_unverifiable_identity_still_connects` → `test_unverifiable_identity_is_refused` (it pinned the policy that caused the incident). |

### Data impact

The Aplus + MDM accounts were deleted (by the user) and re-added with fresh
credentials, so the mis-bound token is gone. `ON DELETE CASCADE` removed
their `mail_messages` (338), `mail_outgoing`, `mail_attachments`,
`mail_deal_links` and `mail_tag_assignments`. The **deal timeline survived**
(`history_events` has no FK to `mail_messages`), as did accounts 2/6/8 and
all their mail. 35 `email_snapshot` events are now orphaned (their mail row
is gone); their bodies are intact inline, 22 had attachments, and 3 still
have their bytes on disk. They can be relinked from the re-synced mailboxes
by `(from_addr, subject, date_received)` — `date_sent` in the snapshot is
the IMAP INTERNALDATE and matches the deleted rows to the microsecond.

---

## ISSUE-019 — Automated backups never ran (2026-10-29)

**Severity: high (silent, total).** Production has had **no automated
backup since the backup service was first written**.

`_load_config_from_db()` returns `(config, configured)` — a tuple — but
three call sites used it as a dict:

- `backup_service.py:870` `_cleanup_old_backups()`
- `backup_service.py:905` the scheduler loop
- `backup_service.py:924` `start_backup_scheduler()`

The startup one raised `'tuple' object has no attribute 'get'`, which
`server.main()` catches and only prints:

```
Backup scheduler not started: 'tuple' object has no attribute 'get'
[READY] ... subsystems=... backup=disabled ...
```

The other two failed inside their own try/except, so the scheduler thread
never ran and the retention cleanup never ran either.

This is why the ISSUE-018 cascade could not be restored from a dump: there
were no dumps on the host.

### Fix

Unpack the tuple at all three call sites. Verified live against the
production DB: `start_backup_scheduler()` and `_cleanup_old_backups()` now
succeed. Tests: `tests/test_backup_scheduler_startup.py` (5, 3 fail against
the old code) — it pins the tuple-unpack at every call site via AST and
exercises the real start path with a stubbed loader.

---

## ISSUE-020 — Status feed notifications invisible to everyone; delivered 1.4M times (2026-10-29)

**Severity: high (silent data waste + a missed feature).**

The user asked: *"Why is there not a single proton notification in my
notifications drawer?"* Three separate defects, all verified on production.

### 1. The rows had no owner

`_insert_feed_notification` (server.py) resolved the recipient list and
looped over it — but the INSERT omitted `user_id`:

```sql
INSERT INTO notifications (type, opportunity_id, actor_user_id, message, payload)
VALUES (%s, NULL, NULL, %s, %s::jsonb)
```

`user_id` took its column default, `NULL`. The drawer query filters
`WHERE n.user_id = %s`, and the unread badge does the same — so **not one**
of the feed notifications was ever visible to anyone:

| type | user_id IS NULL | rows |
|---|---|---|
| service_feed | **true** | **1,438,873** |
| task_assigned | false | 837 |
| note_tagged | false | 137 |
| reply_tagged | false | 57 |
| timesheet_overdue | false | 3 |

Only 1,034 rows were visible to anyone at all.

**Side effect:** `notification_dispatcher.py` queried notifications with NO
`user_id` filter, so it pulled the orphans and marked them
telegram_sent/email_sent — 1.44M pointless UPDATEs over a month, which is
part of why the table was 1019MB on disk for 822MB of live data, and part
of the box's load average of 37. No user-visible spam leaked (only 142
rows in `telegram_notification_log`; a NULL user has no chat id).

### 2. Every entry was re-delivered on every poll

The poller's dedup compared each entry id against **one** remembered id
(`service_feeds.last_item_id`):

```python
if eid == last_id:
    continue
new_entries.append(entry)
```

The Proton feed has 25 entries with **stable** ids (verified by fetching
twice, 75s apart). Only the newest ever equalled `last_item_id`, so the
other 24 were re-delivered on **every** poll — every 5 minutes, to 8
targeted users:

```
24 entries x 8 users = 192 rows per poll
192 x 12 polls/hour = 2,304 rows/hour
~55,000 rows/day
```

1,438,873 rows in ~26 days. The five most-repeated alert titles had
exactly 59,924 copies each.

### 3. Feeds notified everybody about everything

The default `target_mode` was `all` and there was no way to choose which
updates reach you. The Proton feed is mostly maintenance noise:

```
[ 1] Service Maintenance, October 22nd
[ 4] Service Maintenance, October 9th
[ 7] Service Maintenance, September 6th      ... (11 of 25)
[ 0] Partial outage on Proton VPN servers
[11] Global Outage
[24] Server outage on Proton VPN servers in Andorra
```

### Fix

- The INSERT now writes `user_id`; each recipient gets their own row.
- New `service_feed_items (feed_id, item_id)` ledger — `INSERT ... ON
  CONFLICT DO NOTHING RETURNING` decides whether an entry is new, so it is
  delivered exactly once.
- `service_feeds.notify_keywords` — per-feed "only notify about updates
  containing…" filter (case-insensitive substrings; empty = all). The
  Proton feed uses `outage, disruption, incident` → 10 of 25 entries match.
- `service_feeds.target_mode` — `admins` (new default) / `all` /
  `specific` / `none`.
- `service_feed_subscriptions` — per-user opt-out, surfaced in Profile →
  Notification preferences → Status Alerts.
- The dispatcher now skips `user_id IS NULL` rows.
- Migration 040 deletes the 1,438,873 orphans (invisible to every user by
  construction — deleting them removes nothing anyone has ever seen).

**No VACUUM in migration 040:** the migration runner executes each file as
one batch inside an implicit transaction, and `VACUUM cannot run inside a
transaction block` aborted the whole file (caught locally before deploy).

### Files

| File | Change |
|---|---|
| `server.py` | `_insert_feed_notification` writes `user_id`; `_feed_entry_is_notifiable` / `_feed_notify_keywords` / `_normalize_notify_keywords`; `_resolve_feed_target_user_ids` gains `admins` / `none`; `_filter_feed_subscribers`; `_user_is_feed_audience`; `_handle_feed_subscriptions_get/put`; feed create/update accept `notify_keywords`; new default `admins`. |
| `service_feed_poller.py` | Per-entry delivery ledger replaces the single-id comparison. |
| `notification_dispatcher.py` | Skips `user_id IS NULL` rows. |
| `public/index.html` | Admin feed form: admins/all/specific/none + keyword field; profile "Status Alerts" fieldset. |
| `public/app.js` | Feed form wiring + `_profileLoadFeedSubs`. |
| `migrations/040_service_feed_delivery_fixes.sql` (new) | Tables, columns, defaults, orphan cleanup. |
| `tests/test_service_feed_delivery.py` (new) | 29 tests (25 fail against the old code). |

---

## ISSUE-021 — The backup export loaded whole tables into RAM (2026-10-29)

**Severity: critical (took the production site down).**

Starting the backup scheduler for the first time in production (it had
never run — see ISSUE-019) exhausted the box: load average 37, 89MB free,
SSH unresponsive, site 502 until a force restart.

### Cause

The export materialised each table three times over:

```python
rows = db.query_dicts(sql)           # every row as a dict
clean_rows = [{...} for row in rows]  # a second copy
json.dumps(clean_rows)                # a third copy (one huge string)
zf.writestr(name, that_string)
```

Measured on production against the real `notifications` table (1,439,331
rows, 822MB live): **2.9KB of Python overhead per row** → **4,142MB
projected**, against the container's **3,824MB** limit.

The same table streamed through a server-side cursor peaked at **25MB**
(a 165x reduction), streaming all 1.44M rows in 98s under a hard 400MB
address-space cap.

### Two further latent failures found while diagnosing

- **The history tar could not finish.** `data/history-attachments` is 29GB
  across 1204 directories. Measured gzip throughput: 918MB → 838MB in 59s
  (~15.5MB/s of input; gzip shrinks it only ~9% because PDFs are already
  compressed) → the full tree needs ~31 minutes. The code's
  `timeout=900` (15 min) **would have failed the run**.
- **It did not fit on disk.** 29GB against 26GB free on the 77GB root
  volume. Age-based retention (7 days) would have needed 192GB.

### Fix

- `_stream_table_to_zip()` — server-side cursor, rows written straight into
  the zip via `zf.open(name, "w")`. 25MB peak instead of 4,142MB.
- Backup destination is now a dedicated 100GB volume
  (`/mnt/sietch_backup` → `/backup` in compose; `BACKUP_DIR=/backup`).
- Free-space pre-flight: refuses to start rather than fill the disk.
- History-tar timeout configurable, default 5400s.
- Retention keeps the 2 most-recent history tars regardless of age.
- The scheduler waits one full interval before its first run, so a
  container restart can never trigger a heavy run.

### Files

| File | Change |
|---|---|
| `backup_service.py` | `_stream_table_to_zip()`; `_write_history_bundle()`; disk pre-flight; `history_timeout_sec`; `history_keep_count`; wait-before-first-run. |
| `docker-compose.yml` | Mounts `/mnt/sietch_backup:/backup`. |
| `config.example.env` | `BACKUP_DIR=/backup`, `BACKUP_DIR_HOST=/mnt/sietch_backup`. |
| `tests/test_backup_streaming.py` (new) | 12 tests, synthetic data only (10 fail against the old code). |

---

## ISSUE-022 — Orphaned project timeline email snapshots after account deletion (2026-10-29)

**Severity: medium (data pointers dead, bodies intact).**

Deleting the Aplus/MDM mail accounts cascaded their `mail_messages` rows
away (`mail_accounts.id ON DELETE CASCADE` -> `mail_messages.account_id` ->
`mail_attachments` / `mail_deal_links` / `mail_tag_assignments`). The 35
project timeline `email_snapshot` events survived — `history_events` has
no FK to `mail_messages` — so every affected project still shows the full
email inline, but each event's `content.message_id` points at a row that
no longer exists and the `mail_deal_links` rows are gone.

### Fix

New admin action **Relink Orphan Snapshots**
(`POST /api/v2/admin/sync/relink-orphan-email-snapshots`): exact match on
`(from_addr, subject, date_received)` — the snapshot's `date_sent` is the
IMAP INTERNALDATE, which does not change on re-import.

- Dry-run first (default): reports every `event old_id -> new_id` pair
  plus ambiguous/unmatched counts, writes nothing.
- Apply mode rewrites `content.message_id` in place (event id and timeline
  order preserved), recreates the `mail_deal_links` row
  `ON CONFLICT DO NOTHING`, and backfills project attachment copies only
  when the event has none.
- The dual-linked message (old id 3856 -> projects 35 and 46) repoints on
  both events to the same new id; repointing restores the auto-link dedup
  key, so the re-sync creates no duplicate timeline entries.
- Sent-folder messages included (9 of the 35 orphans are sent mail), with
  an `include_sent: false` option to exclude them.

### Files

| File | Change |
|---|---|
| `server.py` | Route + `_handle_sync_relink_orphan_email_snapshots` (+ `_match_relinked_message`, `_apply_snapshot_relink`, `_parse_snapshot_ts` helpers). |
| `public/index.html` | "Relink Orphan Snapshots" admin button. |
| `public/app.js` | Dry-run -> confirm -> apply handler mirroring the history backfill. |
| `tests/test_relink_orphan_snapshots.py` (new) | 13 tests (12 errors + 1 failure against the old code). |
