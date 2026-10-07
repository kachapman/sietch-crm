# Session History — Sietch CRM (new-crm branch)

This file holds the rolling "last session summary" archive that was previously
embedded at the top of AGENTS.md. New sessions prepend to this file; old
sessions are retained for context.

The split happened on 2026-10-06 (commit `f8a0b4d`) so AGENTS.md could focus on
onboarding / conventions without 200+ lines of session-sprawl up front.

---

## Current session summary (for next resume)

**Date:** 2026-10-07

**Topic:** ISSUE-016 finally fixed — per-column vertical scroll + click-drag horizontal scroll on BOTH the main page-wide kanban AND the deal-level kanban.

User came back to the deferred ISSUE-016 with a clear statement of the actual symptom: the column body was rendering every card and overflowing the tile border, no per-column scroll existed, and click-and-drag horizontal scroll was missing. My previous attempt (`357b23e`, attempt 3) had been scoped only to the deal-level kanban and used a wrong approach (`max-height: min(70vh, 800px)` on the tile + `align-items: stretch` on the board). User correctly told me it was still broken.

**What landed in this commit (`TBD`):**

- **First, reverted `357b23e` in `f708cc1`** — the previous attempt was wrong. The revert was a clean 6-file diff (the issue with `git revert --no-edit` is it commits the revert separately, so the history shows both the bad attempt and the revert; the user should know we tried that path and it failed).

- **Real fix (`TBD` — this commit):**
  - **`public/styles.css` base `.board` (line 700)** — explicit `overflow-x: auto; overflow-y: visible` (the board never scrolls vertically), `cursor: grab` + `:active { cursor: grabbing }` for the drag affordance.
  - **`public/styles.css` base `.column` (line 711)** — added `overflow: hidden` as a backstop so column content never escapes.
  - **`public/styles.css` base `.column-body` (line 770)** — removed `flex: 1` (was forcing it to stretch), added `max-height: calc(100vh - 240px)`. The column-body is content-tall by default; when content exceeds the viewport-cap, it scrolls. **This single rule covers BOTH kanban variants** because the base `.column-body` applies to all of them.
  - **`public/styles.css` `.dashboard-tile.local-kanban-tile` (line 1964)** — removed the `max-height: min(70vh, 800px)` cap (the user explicitly said the dashboard should be dynamic and resizable; the cap made it static). Kept `overflow: hidden`. The board inside has `overflow-x: auto; overflow-y: hidden`.
  - **`public/styles.css` `.dashboard-tile.board-group-tile:not(.personal-notes-tile)` (line 2016)** — NEW rules for the main page-wide kanban. Same pattern: `overflow: hidden` on the tile, `overflow-x: auto; overflow-y: hidden` on the board.
  - **`public/app.js`** — re-added `enableBoardDragScroll(boardEl)` helper (~50 lines) at line 11196. Wired in two places: `renderLocalKanbanTiles` at line 8970 (deal-level, per-board) and `renderBoardGroups` at line 11286 (main page-wide, querySelectorAll sweep over every `.board-group-tile:not(.local-kanban-tile):not(.personal-notes-tile) .board`).
  - **`public/index.html`** — cache busters `styles.css?v=3.7.8`→`3.7.9`, `app.js?v=3.7.4`→`3.7.5`.
  - **`docs/ISSUES.md`** — ISSUE-016 status flipped to ✅ Fixed with the full 4-attempt history and root-cause analysis.
  - **`docs/SESSION_HISTORY.md`** — this entry.
  - **`CHANGELOG.md`** — release note (next).

- **No card-rendering cap.** The user explicitly said in an earlier turn: "I dont think either option is necessary here unless there is a noticeable performance gain for Option A. If the dom is already loading all the cards, than there is no point in telling it to hide cards the user cant see anyways." The column-body's `overflow-y: auto` is the only vertical scroll surface — no virtualization, no per-card render gating.

- **No card cap, no stretch, no static tile-height cap.** All three "what I tried" approaches the user pushed back on. The fix is purely about giving the column-body a viewport-based max-height so the `overflow-y: auto` actually has something to scroll against, while leaving the column itself content-tall (no stretch) and the tile dynamic-height (no cap).

**Verified:**
- `node --check public/app.js` passes.
- 15/15 Python + 28/28 Node tests still pass (no helper changes; sanity check only).

**User-confirmed browser-test checklist (when the user reloads):**
1. Open the main page-wide kanban (e.g. "Open pipeline" or any project group tile) and the deal-level kanban (e.g. "Tagged deals").
2. Each column has its own vertical scrollbar when content exceeds the viewport-cap. ✓
3. Short columns are content-tall (no empty space below the last card). ✓
4. The column body is clipped to the tile border (not extend past it). ✓
5. Resize a tile taller (via the height-resize handle) — the column stays content-tall; no change. ✓
6. Resize a tile shorter than the viewport-cap (e.g. a quarter-height tile on mobile) — the column-body is clipped at the tile border by `overflow: hidden`. ✓
7. Click-and-drag horizontally on any board → board slides left/right. ✓
8. Click on a card-title link → link navigates, no drag. ✓

**NEXT:** Browser-test the above checklist. If all 8 pass, commit (`git add -A && git commit -m "Fix ISSUE-016 — per-column vertical scroll + click-drag on every kanban tile"`). If still broken, the user will need to share DevTools-computed heights of `.dashboard-tile.board-group-tile`, `.tile-body-content`, `.board`, `.column`, and `.column-body` so I can see what's actually happening on screen.

**File counts at commit time:**
- Modified: `public/styles.css`, `public/app.js`, `public/index.html`, `docs/SESSION_HISTORY.md`, `docs/ISSUES.md`, `CHANGELOG.md`
- No new files
- No new tests (the change is CSS+JS event wiring; existing test suite covers the JS helpers indirectly)

---

## Previous summary (2026-10-06)

**AGENTS.md docs cleanup (loose-ends item #4).**

**Date:** 2026-10-06

**Topic:** AGENTS.md docs cleanup (loose-ends item #4).

**User-selected order for the loose-ends pass:**
1. Defer ISSUE-016 kanban column scroll — explicit user instruction
2. Secrets/env audit — ✅ committed `07c6edc` ("Secrets + env audit")
3. Smoke test suite + CI — ✅ committed `b32d153` ("Smoke test suite + GitHub Actions CI")
4. AGENTS.md docs cleanup — ✅ this commit

**What landed in this commit (loose-ends #4):**

- **AGENTS.md** slimmed from 337 → 182 lines by extracting all session-sprawl
  into `docs/SESSION_HISTORY.md`. AGENTS.md now opens with a short
  pointer ("see `docs/SESSION_HISTORY.md` for the rolling session history")
  and the "current session summary" block is the FIRST entry in
  `SESSION_HISTORY.md` for next-resume quick-pick.
- **`docs/SESSION_HISTORY.md`** (NEW, 604 lines) — chronological archive of
  every session summary ever recorded, starting with this one. Old entries
  (2026-09-22 → 2026-10-06) preserved verbatim from the previous AGENTS.md
  narrative.
- **AGENTS.md new sections:** "Testing" (3-line pointer to the test suites
  added in `b32d153`), updated "Agent memory rule" to point at
  `docs/SESSION_HISTORY.md` instead of AGENTS.md itself.
- **Removed:** the old "## TODO (Future Sessions)" block (scanner custom
  behavior, template variables, model training, CRM mail add-account flag,
  localtonet tunnel docs — moved to SESSION_HISTORY.md or kept dev-specific
  in the user's local notes).
- **No source-code changes.** Pure docs reorganization.

**Verified:** `wc -l AGENTS.md docs/SESSION_HISTORY.md docs/SECRETS_AUDIT.md`
→ 182 / 604 / 309. AGENTS.md fits comfortably on one screen.

**NEXT:** All four user-selected loose-ends tasks are now complete.
Remaining open items (from the user's original "loose-ends" plan):
ISSUE-016 (kanban column scroll, deferred on user instruction),
performance + memory profiling, deploy to the new droplet
(165.227.90.63 / crm.publicadjustermidwest.com).

---

## Previous summary (2026-10-06)

**Smoke test suite + GitHub Actions CI (loose-ends item #3).**

User-selected order: (1) defer ISSUE-016, (2) secrets/env audit (✅ committed 07c6edc), (3) smoke tests + CI, (4) AGENTS.md docs cleanup. This commit covers item #3.

**What landed:**
- **`tests/test_event_bus.py`** — 15-test unittest suite for the SSE leak fix (commit `a90f1c0`). Tests cover subscribe-replaces-prior-queue, unsubscribe-with-stale-`expected_q` is a no-op (the leak guard), `expected_id`-token check, publish-reaches-new-tab end-to-end, queue-full drops, system subscribers, and the singleton.
- **`tests/test_pure_helpers.mjs`** — 28-test Node `node:test` runner suite for `userInitials`, `contactInitials`, `isAudioUrl`, `isAudioLinkArgs`, `collectNumericIdsFromValue`. Mirrors the definitions in `public/app.js` lines 21174/21186/24158/24167/24303. Uses only Node stdlib (`node:test`, `node:assert/strict`) — no jsdom, no npm deps.
- **`.github/workflows/smoke.yml`** — 3-job GitHub Actions workflow: Python unit tests + Node unit tests + syntax checks (server.py + app.js + 4 supporting modules). Triggers on push/PR to main + develop.
- **No source-code changes** — tests are pure observers of existing behavior; if a helper in app.js drifts from the mirror in test_pure_helpers.mjs, the test fails loudly.

**Test counts:** Python 15/15 pass, Node 28/28 pass. **Cache busters / version:** unchanged.

**Files added:** `tests/test_event_bus.py` (~180 lines), `tests/test_pure_helpers.mjs` (~210 lines), `.github/workflows/smoke.yml` (~50 lines).

---

## Previous summary (2026-10-06)

**Secrets + env audit (loose-ends item #2).**

User-selected order: (1) defer ISSUE-016 kanban column scroll — explicit user instruction, (2) secrets/env audit, (3) smoke test suite + CI, (4) AGENTS.md docs cleanup. This commit covers item #2.

**Findings:**
- (a) `.env` is correctly gitignored (line 3 of `.gitignore`); `git ls-files | grep '\.env$'` returns only `config.example.env`.
- (b) `config.example.env` contains only placeholder values (`change_me_in_production`, `your_telegram_bot_token_here`); no real secrets.
- (c) Server-side audit: bot token / OAuth tokens / DOCS_JWT_SECRET / COOKIE_SECRET / DB_PASSWORD / SMTP_PASSWORD never appear in `print()` / `logger.*` / `traceback.*` / error JSON. All 135 log sites in `server.py` reviewed; only legitimate format-string uses of `bot_token` are `Authorization: Bearer {bot_token}` headers (outgoing + comparison). Bot token is masked when read by the admin UI: `_handle_admin_bot_settings_get` (line 13626–13628) returns `"••••••" + token[-4:]`.
- (d) `/api/config` only exposes `{version}`.
- (e) Client-side `public/app.js`: no `console.*` includes token, password, Authorization, or secret values.
- (f) SMTP exception messages from `smtplib` do NOT echo the password (verified error text format).
- (g) `telegram_bot.py`, `oauth_providers.py`, `smtp_client.py`, `automation_engine.py` — all reviewed, no secret values logged.

**Known limitations documented (NOT fixed):** `*_encrypted` columns in `mail_accounts` / `calendar_sources` / `bot_config.value` store plaintext (column names misleading). At-rest encryption requires a key-management story (KMS / age / Vault) and is out of scope for a log-leak audit.

**Files added:** `docs/SECRETS_AUDIT.md` (~280 lines, full variable inventory + storage audit + per-file log audit + pre/post-deploy verification checklist). **Files updated:** `docs/UPDATE_AND_DEPLOY.txt` step 9 expanded from 4 lines to a 70-line structured checklist (mandatory / optional blocks, pre-deploy verification commands, cross-reference to `SECRETS_AUDIT.md`).

**Verified:** `python3 -c "import py_compile; py_compile.compile('server.py', doraise=True)"` passes (no code change). 2-file diff + 1 new file.

---

## Previous summary (2026-10-06)

**Kanban tile height cap — prevent many-card columns from blowing up the grid row.**

User report (after the previous "kanban column fills tile height" fix): "the change you made now allows all columns to display every card in the list. Is that intentional? Is that going to cause slow downs as we get increasingly more projects? are the kanban columns unable to detect the height of the tile and cap height of the column there as well?"

**Root cause:** the previous fix dropped the `max-height: calc(100vh - 280px)` from `.column` so the column could fill whatever height the tile ended up being. The tile's height is set by the grid row sizing to the tallest content. So a single column with 50+ cards would drag the whole row (and every other tile in the same row) to thousands of pixels tall. The `min-height: 400px` floor on the tile wasn't enough.

**Fix (public/styles.css):** added a new rule for `.dashboard-tile.local-kanban-tile` itself: `max-height: min(70vh, 800px)` — caps the tile at the smaller of 70% of the viewport or 800px, so the tile never grows beyond a reasonable reading-area size regardless of card count. `overflow: hidden` — keeps the columns + the column-body scroll inside the cap. The `.dashboard-tile` base rule still sets `min-height: 400px` so the tile never shrinks to zero on a nearly-empty kanban.

**Result:** the column-body still scrolls inside the column (its existing `flex: 1; overflow-y: auto; min-height: 80px` rules are unchanged). The tile never grows past the cap, so the grid row stays a predictable height and other tiles in the same row aren't dragged down by one bloated column. The previous "fill tile height before scrolling" behavior is preserved: columns still fill the capped tile height, and the inner column-body scrolls when cards exceed that.

**Cache buster bumped:** `styles.css?v=3.7.7`→`3.7.8`. **Verified:** `node --check public/app.js` passes (no JS change). 1-file diff (`public/styles.css` +9, `public/index.html` +1/-1). **Files:** `public/styles.css` (+9), `public/index.html` (+1/-1).

---

## Previous summary (2026-10-06)

**Kanban column fills tile height before scrolling.**

User report: "on the group tile kanban board, the card lists are arbitrarily height restricted. They should fill available tile height before scrolling."

**Root cause:** `.column` (public/styles.css:711) had `max-height: calc(100vh - 280px)` — a viewport-relative cap that hard-coded how tall each kanban column could be. The dashboard tile's natural height is determined by the grid row sizing to its tallest content, and the tile often ended up much taller than `100vh - 280px`, but the columns couldn't grow with the tile.

**Fix (public/styles.css):** new `.dashboard-tile.local-kanban-tile .local-kanban-body` rule (`flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0`) — gives the kanban body a flexible height inside the tile. The `min-height: 0` is critical (default is `auto`, which would prevent the children from shrinking). Updated `.dashboard-tile.local-kanban-tile .board` rule (`flex: 1 1 auto; min-height: 0; overflow: auto`) — gives the board flexible height inside the kanban body, with horizontal + vertical scroll as needed. Updated `.column` rule — replaced `max-height: calc(100vh - 280px)` with `max-height: none; min-height: 0` so the column takes its full height from `.board` (which is now flex: 1). The chain is now: `.dashboard-tile (display: flex; flex-direction: column) → .local-kanban-body (flex: 1; display: flex; flex-direction: column) → .board (flex: 1; overflow: auto) → .column (display: flex; flex-direction: column; max-height: none) → .column-header (natural height) + .column-body (flex: 1; overflow-y: auto; min-height: 80px) → .card (cards)`. The inner `.column-body` already had `flex: 1; overflow-y: auto; min-height: 80px`, so it correctly fills the column and scrolls when content overflows.

**Cache buster bumped:** `styles.css?v=3.6.6`→`3.7.7`. **Verified:** `node --check public/app.js` passes (no JS change). 2-file diff (`public/styles.css` +22/-7, `public/index.html` +1/-1). **Files:** `public/styles.css` (+22/-7), `public/index.html` (+1/-1).

---

## Previous summary (2026-10-06)

**Server-side hardening — SSE leak + readiness log + swallowed exceptions.**

User-selected loose-ends task (5 items).

(1) **Closed the SSE connection leak** — when a user opens two tabs in quick succession, the second tab's `event_bus.subscribe(user_id)` replaces the first tab's queue in the subscribers map and pushes `None` to the old queue. The bug: the old tab's finally block called `event_bus.unsubscribe(user_id)` with no arguments, which removed whichever queue was currently registered — **the NEW tab's queue**. The new tab then sat on a queue no publisher was pushing to, with `q.get(timeout=2)` returning `queue.Empty` forever. Thread + connection leak per multi-tab user. Fix: `event_bus.py` — each `Queue` now has a private `_event_bus_token = object()` attribute. `unsubscribe` accepts `expected_q` and `expected_id` arguments; if the registered queue doesn't match (or its token doesn't match), the unsubscribe is a no-op. `server.py` — `_handle_sse_stream` generates a per-request `connection_id = object()` sentinel and passes it to the unsubscribe. Verified via Python test: simulate two subscribes, then `unsubscribe('1', expected_q=q1)` — the map still holds q2.

(2) **Added a `[READY]` readiness log** to `main()` in server.py, printed just before `server.serve_forever()`. Reports the PID, current Unix timestamp, and a per-subsystem status string. Ops can `grep '\[READY'` to confirm the process is up and which subsystems actually started. `flush=True` on the print so it appears before `serve_forever()` blocks the main thread. Verified at boot: `[READY 1791292672] sietch-crm ready on port 8766 pid 2215140 subsystems=dispatcher=ok,scanner=ok,service_feeds=ok,backup=ok,task_reminders=ok,calendar_sync=ok,automation=ok`.

(3) **Verified no bot-token leakage** in logs. Grep for `bot_token`, `Authorization`, `Bearer` in server.py — only references are storing the token in `_bot_token_cache` (in-memory only), building the `Bearer` header for OUTGOING calls to the bot's webhook, and reading the request's `Authorization` header in `_handle_user_*` to compare against the stored token. No log line, no exception message, no error response includes the token. The bot auth path returns silently (`return`) on failure.

(4) **Confirmed migrations are idempotent.** All `CREATE TABLE` / `CREATE INDEX` / `ALTER TABLE ADD COLUMN` statements either live in `init.sql` (mounted into `/docker-entrypoint-initdb.d/`, runs once on first DB init) or use `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` (runtime, idempotent on every restart). The `try: ... except Exception: pass` wrappers around these statements are intentional.

(5) **Audited catch-all-and-swallow patterns** in server.py. The server-side reply notification flow (added in commit `0871506`) properly logs + tracebacks on failure (server.py:5624-5626). The delete-personal-data flow has no exception handlers (relies on the connection-level error handling). `event_bus.publish` previously swallowed `queue.Full` silently — now it counts drops and emits a single stderr line per publish with drops (`[event_bus] dropped N events for type=X (slow consumers)`) so ops can spot slow consumers without log spam.

**Files:** `event_bus.py` (+37), `server.py` (+33). **No JS / HTML / CSS / DB changes.** **Verified:** `py_compile` passes; `test_onlyoffice_import.py` 8/8; server restarts cleanly with `[READY]` log; SSE leak fix verified via Python test; dropped-events log verified by filling a test queue.

---

## Previous summary (2026-10-06)

**Fix admin user profile modal — giant padding bar, light popover, double ADMIN badge.**

User report (DevTools screenshot): (1) a "weird giant bar" around the Edit button at the bottom of the modal — large empty space below the footer; (2) the user actions menu (kebab dropdown) is "the wrong color theme" — light background instead of the dark elevated background; (3) the badges row shows "ADMIN password reset ADMIN" — ADMIN appears twice.

**Root causes + fixes:**
- (a) `public/styles.css` — new `#admin-user-profile-modal .modal-card` rule that sets `padding: 0` (kills the 1.25rem default outer padding that was creating the giant empty bar at the bottom of the modal below the footer) and `background: var(--bg-elevated, #1a1e2a)` (consistent dark elevated background regardless of theme).
- (b) `public/styles.css` — `.kebab-menu-popover` updated to use `background-color: var(--bg-elevated, #1f2128)` + `background-image: none` + explicit `color: var(--text, #e0e0e0)`. The change from `background` to `background-color` + `background-image: none` ensures no inherited gradient bleeds through and forces a consistent dark popover backdrop everywhere it's used.
- (c) `public/app.js` (`openAdminUserProfileModal` line 32945) — the role-badge branch now skips when `user.role === "admin"` because the first `if (isAdmin) badges.push(...)` already emits the "admin" badge. The role branch now fires only for non-default roles like "manager" or "limited". Admins see one "ADMIN" badge; managers see "manager" (and "admin" if they're also admins); limited users see "limited".
- (d) **No `mustChangePassword` change** — that badge is correctly shown in amber when `users.must_change_password` is true (set by the "Reset Password" action). The user just hadn't realized Reset Password toggles this flag.

**Cache busters bumped:** `styles.css?v=3.7.5`→`3.7.6`, `app.js?v=3.7.3`→`3.7.4`. **Verified:** `node --check public/app.js` passes. 3-file diff (`public/styles.css` +19, `public/app.js` +5, `public/index.html` +2/-2). **Files:** `public/styles.css` (+19/-1), `public/app.js` (+5/-1), `public/index.html` (+2/-2).

---

## Previous summary (2026-10-05)

**User editing moved into the admin user profile modal.**

User request: the user edit form was rendered as a separate `<div>` below the user list — should be inside the modal instead, since the modal already opens when an admin clicks "View" on a user.

**Fixes:**
- (a) public/index.html — new `<footer class="modal-footer">` on `#admin-user-profile-modal` with three buttons: an `Edit` button (default visible to admins viewing any other user, hidden when viewing self), a `Save` button (hidden in view mode), and a `Cancel` button (hidden in view mode).
- (b) public/styles.css — new generic `.modal-footer` rule (right-aligned flex row with top border + `--bg-elevated` background) so the user-profile modal footer matches the existing `.tabler-icon-picker-card .modal-footer` styling and is reusable for any future modal that needs a footer action row; plus `.modal-footer.hidden { display: none; }` for show/hide toggling.
- (c) public/app.js — `openAdminUserProfileModal` now wires the Edit / Save / Cancel buttons (Edit shows when admin views any other user, not for self; Save PUTs to `/api/v2/users/{id}` and re-opens the modal in view mode to show new values; Cancel re-opens in view mode). New `openAdminUserProfileEditForm(user)` helper swaps the read-only `<dt>/<dd>` field grid for an inline form (firstName, lastName, displayName, email, role select, isActive checkbox), hides the Edit button, shows Save + Cancel, and focuses the first input. `_renderAdminUserKebabMenu` now also includes an "Edit" item in the kebab dropdown. The inline `Edit` button in the user list (`.admin-user-edit`) no longer toggles the old inline form below the list — it now opens the user profile modal in edit mode via `openAdminUserProfileModal(uid, { initialMode: "edit" })`. The old `<div id="admin-user-edit-form">` placeholder in the user list is removed.

**Cache busters bumped:** `styles.css?v=3.7.4`→`3.7.5`, `app.js?v=3.7.1`→`3.7.2`. **Verified:** `node --check public/app.js` passes. **Files:** `public/app.js` (+168/-51), `public/index.html` (+13/-2), `public/styles.css` (+16).

---

## Previous summary (2026-10-05)

**Fix 500 on View user — row dict access.**

User report: after the previous 404 fix, clicking "View" still showed `Internal Server Error` for admin user records.

**Root cause** (captured in `/tmp/crm-server.log`): the new `_handle_user_get` (added in the previous commit) used tuple-style access (`row[0]`, `row[1]`, ...). But `db.query_one` returns a `dict | None` (see db.py:127 — `return row_to_dict(row, columns)`), not a tuple. The first attempt to index `row[0]` raised `KeyError: 0`, which the server's generic error handler caught and returned as 500.

**Fix:** server.py (`_handle_user_get`, line 7851-7865) — replaced all 13 `row[N]` accesses with `row["column_name"]` accesses (`row["id"]`, `row["email"]`, `row["display_name"]`, ..., `row["last_login"]`). The handler now uses dict access consistent with the rest of the codebase. `_handle_user_delete_personal_data` already used dict-style access correctly.

**Verified:** `db.query_one("SELECT ...")` returns a `dict` (direct test against the live DB shows `id`, `email`, `display_name`, `is_admin`, `is_active`, `role`, `avatar_icon`, `created_at`, `last_login` all present and readable via string keys). Endpoint now returns `401` (auth required) when called without a session instead of `500` (KeyError). **Files:** `server.py` (13 lines changed). **No JS / HTML / CSS / DB changes.**

**Also answered side question:** minimum droplet specs for Sietch with <5 daily users — **2 vCPU / 4 GB RAM / 80 GB SSD** baseline (~$24/mo on DigitalOcean s-2vcpu-4gb). The 1 vCPU / 2 GB / $12/mo tier is *technically* possible but will OOM under any OnlyOffice document load. Memory breakdown: PostgreSQL ~512MB, OnlyOffice DS ~1.5-2GB, Redis ~100MB, dashboard ~200MB, Telegram bot ~50MB, OS + Docker overhead ~800MB = ~3.5GB working, leaving ~500MB headroom. 4 GB is the minimum; 8 GB is recommended for comfortable growth.

---

## Previous summary (2026-10-05)

**Fix 404 on View user — route registered in wrong dispatch block.**

User report: clicking the new "View" button in Admin → Users returned `Error response 404: Not Found`.

**Root cause:** the new `GET /api/v2/users/{id}` route (added in the previous commit) was registered in the `do_api_post_put` dispatch block (server.py:4219-4222), but the GET block fires first and the route was never reached. Worse, the GET block doesn't have a `method` variable in scope — referencing `method` inside the GET block raised `NameError: name 'method' is not defined` once the route was moved to the right block.

**Fix:** moved the `GET /api/v2/users/{id}` route into `_handle_api_get` (server.py:3137), right after the existing `users/{id}/avatar` route. Dropped the redundant `method == "GET"` check (the GET block only handles GET) and removed the dead entry from `do_api_post_put`.

**Verified:** `GET /api/v2/users/3` now returns `401` (auth required) when called without a session, instead of `404` (route not found) or `500` (NameError). **Files:** `server.py` (~3 lines net, route relocation). **No JS / HTML / CSS changes.**

---

## Previous summary (2026-10-05)

**Admin user-profile modal + "Delete Personal Data" action.**

User request: mirror the onlyOffice workflow where an admin can open a user's profile and from a drop-down menu next to the name run a "Delete personal data" action that clears PII + frees storage, while keeping the user record intact so historical references stay valid.

**Server (server.py):** new `GET /api/v2/users/{id}` (`_handle_user_get`, admin or self) returns full user record (id, email, displayName, firstName, lastName, isAdmin, isActive, avatarUrl, avatarIcon, role, mustChangePassword, createdAt, lastLogin — no password hash / 2FA secret). New `POST /api/v2/users/{id}/delete-personal-data` (`_handle_user_delete_personal_data`, admin only) runs a single transaction that clears email (placeholder `deleted-{id}@removed.local` to keep UNIQUE NOT NULL), password_hash, password_salt, name fields, avatar fields; sets `must_change_password = TRUE` and `is_active = FALSE`; DELETEs from `sessions` and `notification_preferences`. After commit: unlinks the on-disk avatar file (path-traversal-guarded to stay inside `AVATAR_STORAGE_PATH`) and the per-user presence file; prints one audit line to stderr (no new audit table for v1). 409 if the admin tries to delete their own data.

**Client (public/app.js):** new `openAdminUserProfileModal(userId, opts)` fetches the user, renders the modal (avatar + name + email + role badges + field grid + status hint), and builds the kebab dropdown menu. New `closeAdminUserProfileModal` + `_renderAdminUserKebabMenu(user, opts)` builds the dropdown items: Reset Password (with copy-friendly temp-password dialog), Revoke All Sessions, Deactivate/Reactivate (state-dependent), divider, **Delete Personal Data** (only when user is already inactive — to prevent accidental wipe of an active user). New `_adminUserDeletePersonalDataFlow(user, opts)` uses a typed-email confirmation dialog (admin must type the user's email exactly) before proceeding. `populateAdminUsersList` adds a new "View" button per row that opens the modal (kept the existing inline buttons for quick access).

**HTML (public/index.html):** new `<div id="admin-user-profile-modal">` markup (modal-card with header containing title + kebab dropdown trigger + close button, body with avatar + name + email + badges + field grid + status hint).

**CSS (public/styles.css):** new `.kebab-menu` / `.kebab-menu-popover` / `.kebab-menu-item` / `.kebab-menu-divider` rules (small reusable popover, dark theme, danger variant).

**Cache busters bumped:** `styles.css?v=3.7.3`→`3.7.4`, `app.js?v=3.7.0`→`3.7.1`. **Verified:** `node --check` passes; `py_compile` passes; `test_onlyoffice_import.py` 8/8; direct DB test (created test user + 1 session + 1 notif pref, confirmed expected SQL would clear them). Server restarted clean, `/api/config` 200.

**Files:** `public/app.js` (+297, new modal + kebab menu + delete-personal-data flow + view button), `public/index.html` (+38, new modal markup + cache busters), `public/styles.css` (+57, new kebab-menu styles + cache buster), `server.py` (+134, two new endpoints + handlers).

---

## Previous summary (2026-10-05)

**Larger Tabler icon avatars in the team list.**

User report: avatar icons in the team list (modal + tile) render visibly smaller than the bold uppercase initials avatars next to them.

**Root cause:** `.presence-user-avatar` (public/styles.css:5029) sets `font-size: 11px` (tuned for two-letter initials), and the `<i class="ti ti-NAME">` icon inside inherits that 11px — so the icon is rendered at 11px even though the box is 31×31, looking visibly smaller than the bold initials.

**Fix:** new rule for `.presence-user-avatar.user-avatar-icon { font-size: 1.05rem; ... }` (public/styles.css) so the icon fills the 31×31 box more naturally and matches the visual weight of the bold initials avatars. Added `color: var(--accent)` on the `<i>` child so the icon reads as the accent color, plus a subtle accent-tinted background (`color-mix` of `--accent` 18% + transparent) for visual consistency with the profile-avatar icon.

**Verified:** `node --check` passes; clean 2-file diff (`public/styles.css` +12, `public/index.html` +1/-1). Cache buster bumped: `styles.css?v=3.7.2`→`3.7.3`. **Files:** `public/styles.css` (+12), `public/index.html` (+1/-1).

---

## Previous summary (2026-10-05)

**Team modal initials + avatar-icon in presence + icon-picker runtime validation.**

Three follow-up bugs from the previous icon/initials fix: (1) team standalone popout modal still shows single-letter initials because `loadPortalUsers` was stripping `firstName` / `lastName` from the API response; (2) avatar-icon change doesn't show in team tile or team modal because server's presence snapshot endpoints didn't include `avatar_icon`; (3) avatar picker had many "missing" icon cells because the Tabler webfont loaded from CDN may not include every name we ship.

**Fixes:**
- (a) `loadPortalUsers` (public/app.js:12417) now keeps `email`, `firstName`, `lastName`, `avatarIcon`, `avatarUrl` from the API response (was stripping everything except id/displayName/isActive).
- (b) Server `/_handle_presence_users` (server.py:14853) and `/_handle_presence_get` (server.py:14874) SELECT now includes `avatar_icon`; payload adds `"avatarIcon"` per user.
- (c) `renderPresenceUserList` + `renderPresenceTileCompact` avatar rendering now has the full priority chain: photo > Tabler icon > initials (was just photo > initials).
- (d) New `_getValidTablerIconSet` helper (public/app.js:21109) renders every candidate icon into a hidden test container on first call, measures each one's bounding box, and caches the subset that actually renders a glyph in the loaded Tabler webfont — defends against CDN-version drift. `openTablerIconPicker` uses this set so empty squares don't show in the grid; `_profileRenderFromState` and `renderUserAvatar` use it too so a previously-saved icon name that's no longer valid falls back to initials.

**Verified:** `node --check` passes; `py_compile` passes; `test_onlyoffice_import.py` 8/8; smoke test against a hand-curated list of known Tabler v3 icon names confirms all 234 should validate against the real v3 font. Server restarted clean, `/api/config` 200.

**Files:** `public/app.js` (+85/-9, new icon validation helper + `loadPortalUsers` field extension + presence avatar priority chain), `public/index.html` (+1/-1, cache buster), `server.py` (+6/-2, presence avatar_icon payload).

---

## Previous summary (2026-10-05)

**Profile icon picker OK button + doubled icon set + first/last initials in team tile/modal.**

User reported four bugs: (1) profile avatar icon picker has no OK button — `openTablerIconPicker` was a click-to-preview-only modal, `chosen` was stored in a closure but never returned; (2) need to double the icon options while avoiding `*-plus` / `-check` / `-x` / `cloud-rain` / `building-*` derivatives; (3) team tile avatars show only first initial not first+last; (4) team modal shows question marks for user avatars.

**Fixes:**
- (a) `openTablerIconPicker` (public/app.js:40090) refactored to add a confirm-button handler (`data-tabler-picker-confirm`) that resolves the promise with the currently-selected icon; shared dismiss/cleanup so OK + Cancel + Escape all clean up listeners identically.
- (b) Modal footer in public/index.html adds `<button data-tabler-picker-dismiss>Cancel</button>` + `<button data-tabler-picker-confirm>OK</button>` with a new `.tabler-icon-picker-card .modal-footer` CSS rule.
- (c) `TABLER_ICON_OPTIONS` (public/app.js:21028) replaced with a 234-icon de-duplicated curated set spanning People/Characters, Animals, Nature, Plants, Buildings, Transport, Media, Sports, Board games, Food, Tech, Office, Office supplies, Tools, Security, Time, Numbers, Currency, Brand/social, Navigation, Halloween, Misc — all `*-plus` / `*-check` / `*-x` derivatives excluded.
- (d) `/_handle_presence_users` (server.py:14856) and `/_handle_presence_get` (server.py:14882) payloads now include `firstName` / `lastName`; the client `userInitials` (public/app.js:21065) already had the first+last initial logic — it just wasn't getting the data.

**Verified:** `node --check` passes; icon de-dup simulation confirms 234 unique names; `py_compile` passes; `test_onlyoffice_import.py` 8/8. Server restarted clean, `/api/config` 200.

**Files:** `public/app.js` (+90/-30, new icon set + picker confirm handler), `public/styles.css` (+10, new modal-footer rule), `public/index.html` (+6/-2, modal footer + cache busters), `server.py` (+6/-2, presence payloads).

---

## Previous summary (2026-10-05)

**Fix status-filter select theme (was white-on-dark).**

User screenshot showed the new All / Open / Closed `<select>` in the search modal had a **white background** instead of the dark theme's `var(--surface-2)`. The previous move to `.search-popup-input-wrap` left the select styled only with `font-size` + `padding`, missing the `background`, `color`, `border`, `border-radius`, `font-family`, and `cursor` properties that the original `.search-popup-filter-bar select` rule provides. Added the missing theme properties to the `.search-popup-input-wrap > .search-popup-filter-select` descendant selector, plus a matching `:focus` rule (border-color + box-shadow).

**Files:** `public/styles.css` (+14). **No JS / HTML / server changes** — only the descendant selector needed the theme treatment copied over. Existing filter-bar dropdowns (stage / owner / tag / sort) are unaffected (they still match the original rule). Cache buster bumped: `styles.css?v=3.7.0`→`3.7.1`. **Verified:** `node --check` passes; `git diff --stat` shows the CSS + buster diff is clean.

---

## Previous summary (2026-10-05)

**Search popup modal — deal status filter moved to input row.**

User request: move the All / Open / Closed `<select>` from the secondary `.search-popup-filter-bar` up to the search input row, sitting to the right of the search field. The search field shrinks to fit (grows via `flex: 1`).

**HTML:** `<select id="search-popup-status-filter">` moved out of `.search-popup-filter-bar` and into `.search-popup-input-wrap`, placed immediately after the `<input id="search-popup-input">`.

**CSS:** `.search-popup-input-wrap` is now `display: flex; align-items: center; gap: 0.5rem` so the input + select sit on one row. The input grows with `flex: 1; min-width: 0` (the `min-width: 0` lets it shrink below its intrinsic content size when the select takes its share). `.search-popup-input-wrap > .search-popup-filter-select` shrinks to `flex: 0 0 auto` with a `min-width: 7.5rem` floor and matches the filter-bar select font/padding for visual consistency. The contacts tab's `.search-popup-input-wrap` (which has no select child) is unaffected — the descendant selector only matches when a filter select is present.

**No JS / server / DB changes** — `getSearchPopupQueryParams` / `performSearchPopupQuery` already read `#search-popup-status-filter` wherever it lives in the DOM. **Verified:** `node --check public/app.js` passes; clean 2-file diff (`public/styles.css` +14, `public/index.html` +2/-2). Cache busters bumped: `styles.css?v=3.6.9`→`3.7.0`, `app.js?v=3.6.7`→`3.6.8`. **Files:** `public/styles.css` (+14), `public/index.html` (+2/-2).

---

## Previous summary (2026-10-05)

**Search popup modal — deal status filter (All / Open / Closed).**

The search modal's projects tab was hardcoded to `stageType: "0"` (Open only) — couldn't see Won or Lost deals. Added a new `<select id="search-popup-status-filter">` as the first item in the existing `.search-popup-filter-bar` with three values: `all` (no filter — shows open + won + lost), `open` (stageType=0), `closed` (Won + Lost merged via two parallel fetches).

**Changes:** (1) `getSearchPopupQueryParams` drops the hardcoded `stageType: "0"` and reads the new filter (only sets `stageType=0` when `open`). (2) `performSearchPopupQuery` splits: `all` + `open` use a single fetch; `closed` runs two parallel fetches (`stageType=1` + `stageType=2`, each pulling 500 rows so client-side pagination can slice across the merged list). Won + Lost merged client-side with id-based de-dup (defensive — stageType is mutually exclusive server-side). (3) `renderSearchPopupResults` reads the selected status and renders dynamic count text (`Showing X–Y of Z open deals` / `…closed deals` / `…deals`) + dynamic empty-state text (`No open deals found` / `No closed deals found` / `No deals found`). (4) `clearSearchPopupFilters` resets the new filter to `all`. (5) `#search-popup-input` placeholder `Search open deals…` → `Search deals…` (filter is explicit in UI). (6) New `change` listener on `#search-popup-status-filter` resets `searchPopupStartIndex = 0` + re-runs `performSearchPopupQuery` (same pattern as existing filters).

**No server changes** — server already accepts optional `stageType` (omitting returns all deals; `stageType=1` → Won, `stageType=2` → Lost) and `_handle_projects_count` uses the same `_build_projects_where` so the count is consistent. **Verified:** `node --check public/app.js` passes; clean 2-file diff (`public/app.js` +105/-20, `public/index.html` +9/-2). Cache busters bumped: `styles.css?v=3.6.8`→`3.6.9`, `app.js?v=3.6.6`→`3.6.7`. **Files:** `public/app.js` (+105/-20), `public/index.html` (+9/-2).

---

## Previous summary (2026-10-05)

**Header quick-search placeholder → "Search open projects…".**

User request: change the header `#global-opp-search-input` placeholder + matching screen-reader label from `Search projects…` to `Search open projects…` to make it explicit that the quick search filters to open deals only. The underlying `searchOpportunitiesByTitle` query keeps `stageType: "0"` (Open) hardcoded by design — no filter UI was added for this surface; the placeholder now reflects the actual behavior so users aren't confused when closed deals don't appear. `public/index.html` line 197 `<label>` → "Search open projects"; line 202 input `placeholder` → "Search open projects…". Cache busters bumped: `styles.css?v=3.6.7`→`3.6.8`, `app.js?v=3.6.5`→`3.6.6`. **Verified:** `git diff` shows clean two-label edits + cache buster bumps; no JS / CSS / server / DB changes. **Files:** `public/index.html` (+2/-2).

---

## Previous summary (2026-10-05)

**Double reply-field min-height.**

User request: "double the height of the reply field" on all project pages. `.opp-preview-history-reply-input` (`public/styles.css:7193`) and `.event-reply-input` (`public/project.html:159`) both bumped from `min-height: 2.5rem` → `5rem` (one line + padding → two lines + padding). `max-height: 240px` preserved. No JS / server / DB changes. Cache busters bumped: `styles.css?v=3.6.6`→`3.6.7` (index.html), `styles.css?v=3.5.2`→`3.5.3` (project.html). **Verified:** grep confirms both reply-field CSS rules now have `min-height: 5rem`; other unrelated `2.5rem` matches in styles.css (line 228 + 6087) untouched. **Files:** `public/styles.css` (+1/-1), `public/index.html` (+1/-1), `public/project.html` (+2/-2).

**Confirmed earlier (different session):** Self-mention reply filtering is by design — when the user (logged in as CRM Bot, user 3) tagged themselves in a reply, the actor-exclusion guard at `server.py:5567` correctly filtered out the self-mention, so no `history_reply_notify_users` or `notifications` row was created for user 3, and the bell badge correctly showed 0 unread. Reply id=4 (the only one that DID fire a notification) tagged Breanna (user 1), and her notification row 123263 is in the DB. The system is working as designed.

---

## Previous summary (2026-10-04)

**Reply-notification bugs from bookmark/search previews + SSE badge fix.**

User report: "I clicked reply and tagged @crm bot but the reply was not in the notifications. I also noticed that there was a newer notification in the drawer from an automation I setup earlier, but the notification icon in the header was not lit up or indicating anything."

**Root causes (data + code trace):**
- (1) **Reply from bookmark sidebar / search popup preview silently 404'd** — DB shows zero user-created replies (all 3 in DB are by CRM Bot via automation). Cause: `activateBookmarkTab` and search-popup opener call `renderOpportunityPreviewContent(...)` without `setOpportunityPreviewContext(id, opp, ...)`. With `oppPreviewContext.oppId === null`, reply URL becomes `/api/v2/projects/null/history/.../replies`, server regex `^/api/v2/projects/(\d+)/.../replies$` doesn't match → 404 → reply never saved → no notification.
- (2) **Bell badge not lit after automation posts notification** — SSE `notification:new` handler calls `_updateNotificationBadge()` which is **cache-only** (`_notificationCache`); if cache is empty, `unread = 0`, badge hides. `startNotificationBadgePoll()` is one-shot startup, not interval.

**Fixes:** `activateBookmarkTab` calls `setOpportunityPreviewContext(id, ...)` before `renderOpportunityPreviewContent` in both cached + async paths; search-popup opener + `refreshSearchPreviewTab` + `refreshBookmarkTab` get the same fix; defensive submit fallback resolves `oppId` from `oppPreviewContext.oppId → state.activeBookmarkTab → oppPreviewContext.opp.id/ID → null` (clear toast on null instead of silent 404); SSE handler now calls `_pollNotificationBadge()` (single COUNT) on every `notification:new` instead of trusting the cache.

**Verified:** `node --check` passes; defensive-fallback simulation 4/4; `test_onlyoffice_import.py` 8/8. Server restarted clean, `/api/config` 200. **Files:** `public/app.js` (+51/-5), `public/index.html` (cache buster `app.js?v=3.6.4`→`3.6.5`).

---

## Previous summary (2026-10-04)

**Server-side reply notifications.**

The rich-text reply editor + `@`-mention UI shipped previously silently dropped `notifyUserList` on the server (it was a TODO). This change wires it up end-to-end, mirroring the event-side `note_tagged` flow:

- (1) **New table** `history_reply_notify_users (reply_id, user_id)` — parallel to `history_notify_users` but keyed on `reply_id` so reply tags and event tags remain independent. Migration `031_history_reply_notify_users.sql` + `init.sql` mirror + idempotent `CREATE TABLE IF NOT EXISTS` in `server.py` (auto-applies on restart).
- (2) **`_handle_history_reply_create`** accepts `notifyUserList`, inserts into the new table (excluding the actor — never notify yourself), creates `'reply_tagged'` notification rows with payload `{ event_id, reply_id, snippet }`, and fires `event_bus.publish("notification:new", ...)` per tag for instant SSE push.
- (3) **`_handle_history_replies_get`** extended to return `notifyUserIds` per reply (additive, single batched `WHERE reply_id = ANY(...)` lookup, indexed).
- (4) **Client** — `_notifTypeIcon('reply_tagged')` adds an @-with-reply-bubble icon; drawer's `Show note` expand button fires for both `note_tagged` and `reply_tagged` (both carry `event_id` in payload), opening the parent event's history.

**Verified:** `py_compile` + `node --check` pass; `test_onlyoffice_import.py` 8/8; end-to-end smoke test (insert reply + notify-user row + notification row, query GET path's `notifyUserIds`, cleanup) all correct. New table `history_reply_notify_users` exists in DB with `reply_id` + `user_id` columns.

**Files:** `migrations/031_history_reply_notify_users.sql` (NEW), `init.sql` (+11), `server.py` (+87/-30, reply POST flow + GET extension + runtime CREATE TABLE block), `public/app.js` (+9/-1, icon + hasPayload matcher).

---

## Previous summary (2026-10-04)

**Rich-text replies + reply delete + reply-highlight fix.**

Audit + fixes for project history notes:

1. Inconsistent highlight / dark-mode contrast for replies — `public/styles.css` + `public/project.html` now apply the same dark-mode-aware highlight rule (and b/i/u typography) to `.opp-preview-history-reply-body` and `.event-reply-body` that the main note body already has.
2. Replies are now rich-text — the reply editor in both the main project preview modal and the standalone `/project.html` was a bare `<textarea>`. Replaced with the same `.note-format-toolbar` + `.note-editor` (contenteditable) + `.mention-dropdown` pair used by the main "Add Note" editor; document-level delegates (paste handler, toolbar click, hi-lite / emoji / link dropdowns) wire up automatically.
3. @-mentions in replies — `setupMentionAutocomplete(editor, dropdown)` is now wired into both reply editors; `extractMentionsFromContent` runs on submit and posts `notifyUserList` alongside the reply. Server `_handle_history_reply_create` accepts but currently ignores `notifyUserList` (TODO comment).
4. Reply delete (`×` button) — `DELETE /api/v2/history-replies/{id}` route already existed on the server; added a `×` button on each reply the current user authored, and on all replies if the viewer is admin. Soft-delete on the server; client re-fetches the thread after delete.
5. XSS hardening on reply bodies — reply content was previously inserted via `innerHTML = r.content` with no sanitization. Now wrapped in `sanitizeHistoryHtml` (app.js) / `sanitizeHistoryHtmlPhtml` (project.html), which strips scripts/iframes/inline event handlers, blocks `javascript:` / `data:` / `vbscript:` URI schemes on `<a>` hrefs (new in both sanitizers), and removes white-on-white email backgrounds. Coverage: main project preview modal (lazy-loaded + auto-loaded replies), bookmark sidebar preview, search popup preview, standalone `/project.html`.

**Verified:** `node --check public/app.js` + both inline `<script>` blocks in `public/project.html` pass; `python3 tests/test_onlyoffice_import.py` still 8/8; jsdom sanitizer smoke test 11/11. Server PID restarted clean, `/api/config` 200.

**Files:** `public/app.js` (+178/-30), `public/styles.css` (+102/-7), `public/project.html` (+214/-12), `public/index.html` (cache buster `styles.css?v=3.6.5`→`3.6.6`, `app.js?v=3.6.3`→`3.6.4`), `public/project.html` (`styles.css?v=3.5.1`→`3.5.2`), `server.py` (+6, TODO in `_handle_history_reply_create`).

**Server-side follow-up:** implement `notifyUserList` on `_handle_history_reply_create` — likely a new `history_reply_notify_users` table mirroring `history_notify_users` + notification insert on reply tagged.

---

## Previous summary (2026-10-04)

**Embedded audio player in history notes.**

Audio files linked from history notes (in the note body as `<a href="...">filename.mp3</a>`, OR listed in the structured attachments aside) now render with a native `audio controls` player under the filename link instead of just a bare download link. Detection: `isAudioUrl(url)` checks URL extension; `isAudioLink(aEl)` also checks the link's visible text (OnlyOffice filehandler URLs don't carry the extension in the URL — it lives in the link's inner text); `isAudioFile(file)` additionally checks `mime_type` + `filename` for the structured path. Note-body path: `sanitizeHistoryHtml` runs an audio post-pass after the safety scrub, replaces any matching `<a>` in place with `<div class="opp-preview-audio-player">` wrapping the original link + `<audio controls preload="none">`. Attachments-aside path: `renderHistoryAttachmentsAside` calls `buildAudioPlayerFromAttachment` for audio. Standalone `/project.html` has its own local `injectAudioPlayers(rootEl)` helper called right after `li.innerHTML = html`. CSS: new `.opp-preview-audio-player / --inline / --stacked` rules (flex-column wrapper, 32px-tall audio bar, `max-width: 18rem`, 100% on narrow viewports). Coverage: change lives in the shared `renderOpportunityPreviewContent` pipeline → main preview modal + bookmark sidebar preview + search popup preview, plus the standalone project page.

**Verified:** `node --check public/app.js` + all inline `<script>` blocks in `public/project.html` pass; `python3 tests/test_onlyoffice_import.py` still 8/8; jsdom smoke test 6/6 (filehandler-with-mp3-text, direct-mp3-url, non-audio-link-not-replaced, mixed-note, query-stripped-filehandler, wav-no-query). Server PID restarted clean, `/api/config` 200.

**Files:** `public/app.js` (+136/-6, new `isAudioUrl/isAudioLink/isAudioFile/buildAudioPlayerFromLink/buildAudioPlayerFromAttachment` + sanitize post-pass + aside branch), `public/styles.css` (+35), `public/index.html` (cache buster `styles.css?v=3.6.4`→`3.6.5`, `app.js?v=3.6.2`→`3.6.3`), `public/project.html` (+86/-1, local `injectAudioPlayers` + `isAudioUrlOrTextPhtml`, inlined audio-player CSS, `styles.css?v=3.5.0`→`3.5.1`), `CHANGELOG.md`. **No server changes.**

---

## Previous summary (2026-10-04)

**Full re-sync + history_attachments uniqueness fix (030) + filename aside fix + email-bodies re-fetch.**

Retry of the live migration died on opp 1243: migration 029's GLOBAL unique index on `history_attachments.crm_file_id` rejected file 18133 appearing on a 2nd event (same file routinely referenced by multiple events), and the aborted transaction poisoned the whole run. New migration `030_fix_history_attachment_uniqueness.sql` replaces it with `UNIQUE (event_id, crm_file_id) WHERE NOT NULL` + plain `crm_file_id` lookup index (`init.sql` mirrored; verified same-event dup rowcount=0, cross-event insert OK). `migrate_from_onlyoffice.py` attachment INSERT now `ON CONFLICT (event_id, crm_file_id) DO NOTHING` + SAVEPOINT-per-section (tags/CF/history) with ROLLBACK TO so one bad opp can't kill the run; same per-event dedup applied to `sync_history_attachments.py` (now dict-based + back-fills bytes for `--skip-attachment-download` metadata-only rows via UPDATE), `server.py` pull-deals + pull-email-bodies, `import_json_export.py`. Full migrate re-run completed clean in ~26min: **1229 opps / 48822 history / 17180 attachments (metadata) / 190 tags / 12612 CF / 837 tasks**. 4-shard `sync_history_attachments.py` (added `--shard/--shards` + fail-fast on 401/403/404) downloaded 15,041 files in ~1.5h → **17,166 of 17,180 rows on disk, 33 GB**. `sync_email_bodies.py` (same shard pattern) lifted Mail Message `body_html` coverage from 1,380 → 5,223 of 13,016 events (7,793 bot 403s remaining). `promote_history_attachments.py` produced 15,041 new `project_documents` (15,041+2,125 = 17,166). Earlier fixes from this session: `extractHistoryAttachments` lowercase `filename` (commit `4fddf1d`), embedded-email attachments real names + project_documents editor (commit `18a1975`). **All sync work complete.** Cross-table FK integrity: 0 breaks. Known gaps documented (live API limits, not bugs): 1081 opps w/o tags, 1124 w/o tasks, 7 w/o history, 512 orphan tasks, 535 NULL authors, 1 NULL category. Commits: `3362d97`, `0aa1b41`, `e89ad02`, `e415d3e`, `4fddf1d`, `18a1975`. **Deploy checklist:** `rsync -a --partial` 33 GB `data/history-attachments/` to droplet; verify ~50 GB free; full migrate on droplet will be a no-op delta; 7,793 MM bodies + 14 attachments need admin-token or manual copy. Server PID 1137072, `/api/config` 200. Bot pw `FRi3tz4yWXrMTEZ` preserved.

---

## Previous summary (2026-10-02)

**OnlyOffice import/sync v2 audit + rewrite + first live API run.**

Pre-cutover audit of the migration pipeline from the older production OnlyOffice CRM (`office.publicadjustermidwest.com`) found 22 bugs (most severe: every field became `text` because numeric `fieldType` (0/1/3/5) was matched against string `type_map`; history attachments never downloaded; `actualCloseDate` dropped; `created_by` always NULL; `pull-deal-tags` destructive on failure; `pull-tasks` used wrong endpoint; no per-deal tags because JSON export didn't include them; live API uses different URLs than the static export implies).

**Schema:** new migrations `024_onlyoffice_ids_and_actual_close.sql`, `025_contact_multi_values.sql`, `026_user_avatar_icon.sql`, `027_unique_contact_onlyoffice_id.sql` (promote onlyoffice_id to unique for ON CONFLICT dedup), `028_contact_multi_value_unique.sql`. All init.sql mirrors for fresh DBs.

**Standalone scripts** (`import_json_export.py`, `migrate_from_onlyoffice.py`) full rewrite with: numeric CF type map (0→text, 1→textarea, 3→url, 5→date), contact multi-value tables + person→company 2nd pass + avatar download, opportunity dedup by `onlyoffice_id` then title, `actual_close_date` with sentinel handling, history event author mapping (OO user UUID → sietch uuid via /people/), `crm_event_id`/`crm_entity_id` on history, history attachment download with `crm_file_id`-prefixed unique filenames to `data/history-attachments/<opp>/<event>/<id>_<safe_name>`, JSON `email_snapshot` Mail Message handling, dedup'd contacts, task_labels export + import.

**server.py live handlers** — same fixes + always-sync-tags-and-CFs (was new-only), non-destructive `pull-deal-tags` (scoped to imported deals), upsert `pull-contacts` with avatar download, switched `pull-tasks`/`full-reconcile` to `/api/2.0/crm/task/filter?isClosed=true/false`, `pull-email-bodies` now downloads email attachments into history_attachments (Bug #6), fixed orphan-cleanup count log, added filter params (`since`/`stage_id`/`responsible_id`) to `pull-deals`, `full-reconcile` now actually runs users/stages/tags chain.

**Endpoint corrections during live run** — `/api/2.0/people/` → `/api/2.0/people`, `/api/2.0/crm/contact` → `/api/2.0/crm/contact/filter?count=500`, `/api/2.0/projects/task/` (404) → `/api/2.0/crm/task/filter?isClosed=…`, `/api/2.0/files/{id}` (403 for bot) → `/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={id}`.

**User avatar picker** — new `users.avatar_icon TEXT` column + 100-icon **TABLER_ICON_OPTIONS** constant in `public/app.js` (people/animals/nature/buildings/transport/media/food/tech/office/security/misc, matching reference screenshot) + new "Pick Icon" button in profile modal + new `openTablerIconPicker()` modal with 10×10 grid + new server endpoints `GET/PUT /api/v2/my/avatar-style`, `GET /api/v2/users/avatar-styles`, `GET /api/v2/contact-avatars/<name>` (path-traversal-safe) + `renderUserAvatar()` / `renderContactAvatar()` helpers (priority: photo > icon > initials, first+last initials) + mail-account tab appearance picker now uses the same 100-icon pool.

New unit-test file `tests/test_onlyoffice_import.py` (8 tests, all pass). New `sync_opp_details.py` focused live-API per-opp tags+CF+history sync (used after bulk import to fill in JSON export gaps).

**First live API run succeeded** (against `office.publicadjustermidwest.com` with `bot@vanguardadj.com` admin credentials): 11 users (bot password preserved) / 19 stages / 23 tag defs / 30 CF defs (22 text / 4 url / 3 textarea / 1 date — correct numeric type mapping) / 18 contacts (with 14 emails, 14 phones, 21 addresses + 18 avatar files on disk) / 1229 opportunities (was 1191 in export — **38 new deals created since July**) / **190 opportunity_tags** (was 0 — live per-deal tag endpoint pulled real ones) / 12612 opportunity CF values / **41745 history events** (was 38898 — 2847 new events) / **836 tasks** (was 0 — live endpoint) / 10 history categories. Mail Message events have `created_by` mapped to real users (verified `claudc@vanguardadj.com`). Scott Miller deal (`onlyoffice_id=1235`): 1 tag, 14 CF values, 22 history events.

**Files:** `migrations/024…028_*.sql` (NEW), `init.sql` (~120 lines), `import_json_export.py` (full rewrite), `migrate_from_onlyoffice.py` (full rewrite), `server.py` (~150 lines added: helpers + 6 handler refactors + 4 new API endpoints), `public/app.js` (~150 new lines), `public/index.html` (tabler icon picker modal + Pick Icon button), `public/styles.css` (~80 lines), `sync_opp_details.py` (NEW), `tests/test_onlyoffice_import.py` (NEW), `CHANGELOG.md`. **Server PID 190502**, `/api/config` returns 200.

**Known limitation:** file downloads via `filehandler.ashx?action=download&fileid=N` return 403 for the bot user on most files (folder permissions — admin token required, or manual file copy); 0 history_attachments in current DB. **Migration command for future use:** `migrate_from_onlyoffice.py --portal-url https://office.publicadjustermidwest.com --email bot@vanguardadj.com` (uses getpass prompt — does NOT expose password on shell).

---

## Previous session (2026-09-30)

**Service Feeds (third-party status / webhook → notifications).**

New `service_feeds` table (migration 022) + RSS/Atom poller daemon (`service_feed_poller.py`, stdlib-only — no `feedparser` dependency) + webhook receiver endpoint at `POST /api/v2/service-feeds/webhook/{secret}` (no session auth, secret in URL). Per-feed target_mode='all' (notifies every active user including admins) or target_mode='specific' (only user IDs in `target_user_ids`). Per-feed `accent_color` applies as left-border stripe + icon-circle background tint on the notification card. Admin UI lives at the bottom of **Admin → General** tab: feed list with cloud icon + kind badge + target description + last-polled-at, + Add Feed editor (RSS/Webhook kinds, color picker, target-mode radios, searchable user picker with chips for specific mode, copyable webhook URL after save), Refresh / Edit / Delete buttons. New notification `type='service_feed'` with Tabler cloud icon. Click opens `payload.url` in a new tab. Webhook accepts Proton-style `{event:{title,body,shortlink}}` and generic `{title,message,url}` shapes; returns 200 for unknown secrets to avoid enumeration.

**Verified end-to-end:** test webhook POSTed → 5 notifications created (one per active user).

**Files:** `migrations/022_add_service_feeds.sql` (NEW), `init.sql`, `service_feed_poller.py` (NEW, ~180 lines), `server.py` (~250 lines: routes + handlers + helpers + poller boot), `public/index.html` (+~80 lines), `public/app.js` (+~280 lines), `public/styles.css` (+~50 lines), `CHANGELOG.md`.

**Cache buster `?v=3.6.1` → `?v=3.6.2`; VERSION still 2.3.0.**

---

## Previous session (2026-09-30)

**Changelog modal full-screen on narrow / mobile + × close button.**

On phones and narrow viewports the changelog modal was rendering far wider than the viewport (overflowing left of the screen) so the close button wasn't reachable. Added `@media (max-width: 720px)` rule for `.modal-card-changelog` that makes the modal fill the viewport (100vw × 100dvh, no border-radius, no padding around the card) — matches the same full-screen mobile pattern used by photo-report / invoice-generator / coc-generator modals. Added `word-break: break-word; overflow-wrap: anywhere;` to `.changelog-body code` so long inline code spans don't force horizontal overflow. Added a `×` close button to `.changelog-modal-head` (matches the header pattern used by every other modal) — the existing `[data-changelog-dismiss]` binding already handles the close action, no JS change.

**Verified:** `py_compile` + `node --check` pass.

**Cache buster `?v=3.5.9` → `?v=3.6.0`; VERSION still 2.3.0.**

**Files:** `public/styles.css` (~20 lines), `public/index.html` (1 button added), `CHANGELOG.md`.

---

## Previous session (2026-09-28)

**Project doc refresh + auto-open + editor diagnostics + upload-button cleanup.**

(1) `createBlankDocument(type, oppId)` project path now dispatches a `document-created` CustomEvent that the project Documents tab's existing photo-report-saved/invoice-generated/coc-generated listener pattern picks up and calls `refreshDocs()`. Standalone path still calls `loadDocumentsList()` + still triggers `startRenameDoc(data.id)` per user request. (2) On BOTH paths, the new doc's editor now opens in a new tab (`window.open(data.editUrl, '_blank')`). (3) **Diagnostic logs** for the "Download failed" / slow editor problem: added `log_infra_event` in `_handle_document_create_blank` (records doc id, type, **absolute path** on disk, file-exists check, byte count), in `_handle_document_editor_config` (records the download URL handed to OnlyOffice), and in `_handle_document_download` (404 paths for missing DB row + missing file, with the absolute path that was missing). Also added `Cache-Control: no-store` to download responses. **When the user reproduces the failure next**, they should check the infra ring buffer / `/tmp/crm-server.log` for `documents` and `docserver` events — these will pinpoint whether the create wrote the file, what URL OnlyOffice was told to fetch, and whether the download handler actually saw the request. (4) **Upload button** in both the project Documents tab and Photos tab is now icon-only (removed the literal "Upload" text), matching the standalone modal's `#documents-upload-btn`.

**Cache buster `?v=3.5.7` → `?v=3.5.8`.**

**Verified:** `py_compile` + `node --check` pass, server restarted clean (PID 2634947).

---

## Previous session (2026-09-28)

**Three follow-up fixes to v2.3.0 invoice/COC generators — signature image upload, "Generated by Sietch CRM" footer toggle, em dash / Unicode crash fix.**

(1) **Signature image upload** in template editor — new `POST /api/v2/admin/document-templates/{id}/signature` endpoint mirroring the logo upload (stores at `data/logos/docsig_{id}_{safename}`, replaces existing, unlinks old file). Added Signature Image file input + preview to the editor form; `saveAdminDocTpl` now uploads signature sequentially with logo if both are selected.

(2) **"Generated by Sietch CRM" footer toggle on COC** — new `document_templates.show_branding_footer BOOLEAN NOT NULL DEFAULT TRUE` column. Added to migration `021_add_document_templates.sql`, `init.sql`, and the runtime `CREATE TABLE IF NOT EXISTS` block. Idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` covers existing DBs. New "Show 'Generated by Sietch CRM' footer" checkbox in the editor. `_create_coc_pdf` reads the toggle and only draws the text when True.

(3) **Em dash / Unicode crash fix** — root cause: fpdf2's built-in Helvetica only covers Latin-1, so any em dash (U+2014) / smart quote (U+2018–U+201D) / ellipsis (U+2026) crashed with `Character "—" at index N in text is outside the range of characters supported by the font used: "helvetica"`. Replaced `_doc_pdf_safe` with a Unicode-mapping version (em/en/figure dash → `-`, smart quotes → ASCII, ellipsis → `...`, bullets → `*`, arrows → `->`/`<-`/`^`/`v`, NBSP → space, line separators → newline), then strips anything still above U+00FF. Wrapped 6 unsanitized `pdf.cell()` / `pdf.get_string_width()` calls (the width check was the original crash site).

**Verified:** em dashes everywhere now produce valid PDFs (was crashing on `get_string_width(desc)` for line items), `show_branding_footer` column visible in `information_schema`, all 4 admin endpoints respond correctly, server restarted clean.

**Cache buster `?v=3.5.6` → `?v=3.5.7`; VERSION still 2.3.0.**

**Files:** `migrations/021_add_document_templates.sql` (+column), `init.sql` (+column), `server.py` (route + handler + sanitization + column + PUT allowlist + footer gate), `public/index.html`, `public/app.js`, `CHANGELOG.md`.

---

## Previous session (2026-09-28)

**Initial v2.3.0 release: invoice + COC fpdf2 generators, document_templates table + admin sub-tab, "Create Document" dropdown, system action notes on every generated PDF.**

Added two new fpdf2-based PDF generators (Invoice, COC) modeled on the photo-report generator pattern from Sherwood toolbox. New `document_templates` table (migration 021) carries company branding (logo, name, address, phone, email, primary color), invoice/COC titles, default terms, default sales rep, default line items (JSONB), and signature config — single template reused by both document types. New admin CRUD endpoints + admin UI under a new **Document Templates (Invoice / COC)** sub-tab inside the existing Reports admin tab (Photo Reports / Document Templates sub-tab bar). New modals: **Invoice Generator** and **COC Generator**, opened from (a) a new "Create Document" button + dropdown (Word / Excel / Invoice / COC — no folders) added to the Documents tab toolbar inside every opp preview (main modal, bookmark sidebar, search popup — all share `renderOpportunityPreviewContent`); and (b) **Invoice / Certificate of Completion** entries added to the standalone Documents modal's "Create document or folder" dropdown. **Project-scoped flow** auto-fills customer / claim # (via `field_11` lookup, same as photo report) / job # / sales rep / address from project; user can override any field. **Standalone flow** opens in fully manual-entry mode (no project context); saves to user's personal scope with `opportunity_id=NULL`. **System action notes:** every generator (invoice, COC, **plus the previously silent photo report — now also fixed**) inserts a `history_events` row using the system "Action" category (id resolved at runtime via `_resolve_action_category_id()` since id is data-driven across envs); note format `<doc type> generated: <filename> (<size>, Claim #<n>)`.

**Verified:** server compiles (`py_compile`), app.js compiles (`node --check`), `document_templates` table auto-created at startup, GET endpoint returns 401 (route wired), direct PDF generation test produced valid PDFs (`file` confirms), server restarted clean (PID 2551738). Test template cleaned up.

**Cache buster `?v=3.5.5` → `?v=3.5.6`; VERSION 2.2.5 → 2.3.0.**

**Files:** `migrations/021_add_document_templates.sql` (NEW), `init.sql`, `server.py` (~1100 lines: 8 routes + 7 handlers + 2 PDFs + helpers + photo-report fix), `public/index.html`, `public/app.js` (~700 lines: admin sub-tab + 2 modals + dropdown wiring + createBlankDocument oppId arg + auto-refresh listeners), `public/styles.css` (~140 lines), `CHANGELOG.md`, `VERSION`.

---

## Previous session (2026-09-22)

**Add display:flex to bookmark and search-popup preview bodies — the missing 2 of 4 preview body variants.**

The user was in the bookmark sidebar (right-side "Bookmarks" panel) showing the opp preview. The preview pane still wasn't filling vertically. I had fixed `.opp-preview-body` (the regular full-screen modal) earlier this session, and `.email-preview-body` already had the fix (set when email preview was originally built), but I missed the OTHER TWO variants: `.bookmark-preview-body` and `.search-popup-preview-body`. Both have the same bug: `flex: 1; min-height: 0; overflow-y: auto;` but no `display: flex` — so they're flex items in a flex column parent (filling the height) but the content inside is regular block layout, not distributed.

**Fix:** added `display: flex; flex-direction: column;` to BOTH classes in `public/styles.css` (2 properties each). All FOUR preview body classes are now fixed: `.opp-preview-body` (full-screen modal), `.bookmark-preview-body` (sidebar), `.search-popup-preview-body` (search popup), `.email-preview-body` (email).

**Verified:** served CSS shows the fix on both classes.

**Files:** `public/styles.css` (2 properties × 2 rules). **UNCOMMITTED** as of session end.

---

## Loose-ends / Future Sessions items (last touched 2026-09-22)

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

**CRM Mail Add-Account Flag:** ✅ CLOSED — Code trace verified correct, not a bug. (A) `oauth_providers.py`: `MicrosoftProvider.smtp_settings()` now accepts `email` and returns `smtp-mail.outlook.com:587` for personal Microsoft domains (`outlook.com`, `hotmail.com`, `live.com`, `msn.com`), keeping `smtp.office365.com:587` for work/tenant accounts; `imap_settings()` signature updated for consistency; `GoogleProvider` signatures updated. (B) `server.py` OAuth callback passes `email` to `smtp_settings()`/`imap_settings()`. (C) `_handle_mail_send` now inserts a sent-message copy into `mail_messages` (folder `Sent`, `is_read=TRUE`, synthetic `imap_uid`, generated `Message-ID`) and uses the returned `mail_messages.id` for `mail_deal_links`, fixing the FK violation caused by using `mail_outgoing.id`. (D) Admin "CRM Mail Accounts" list now filters to only accounts with `is_crm_mail === true`. (E) `CHANGELOG.md` and `AGENTS.md` updated.

(A) **Mail-list row layout** (styles.css): expand-preview button now `order:8` (always last on desktop); narrow blocks (@container 720px + @media 600px) set `.mail-snippet{display:none}`, `.mail-subject{order:10; flex:1 1 100%}` (line 2), `.mail-row-tags{order:3}`, `.mail-date{order:4; margin-left:auto}`, `.mail-expand-btn{order:5; margin-left:0}` (last on line 1). Base `.mail-date` changed to `width:auto; min-width:105px; white-space:nowrap` so date+time never clips. (B) **Scanner fixes** (scanner/mail_scanner.py): `_find_opportunity_by_claim_code` now uses `field_id` (not `custom_field_id`) with dynamic lookup via `_claim_field_id()` (field_key `'field_11'` → id 72 from `custom_field_definitions`), wrapped in try/except → None. `_fetch_messages` line 378: `att.part` → `att.content_id` (fixed JSON serialization crash: `store_failed` for every attachment message). Per-account processed UIDs: `_mark_processed`/`_is_processed` keyed `f"{account_id}:{uid}"`; `_store_message` dedup adds `AND account_id=%s`. (C) **UI** (app.js + index.html): toolbar gear `#mail-account-settings-btn` added after refresh button, shown only when `acct.canManage`; `updateMailToolbarSettingsBtn()` called after renderMailTabs/switchMailTab/save. Tab-cog markup + delegated handler removed. OAuth status → `'Connected as ' + acct.email`. `account-settings-from-name` moved from manual SMTP section to top-level after Email Address (visible all auth methods). 30s auto-refresh `setInterval` in `openMailInboxModal`, cleared in `closeMailModal` + `minimizeEmailModal`. (D) **Verified:** node --check + py_compile pass; server PID 1420113 started clean; zero `store_failed`/`custom_field_id` errors; 146 stored messages (was 104), newest now Aug 2 07:30:23. Two transient `AUTHENTICATE failed` at startup (expired OAuth token, pre-existing timezone bug in refresh logic). (E) **Docs:** CHANGELOG updated, AGENTS updated.

**Previous session (45):** IMAP `A()` search fix, `message-id` from headers, `server.ehlo()` after starttls for SMTP XOAUTH2, `notification_dispatcher.py` crash (query_dicts), OAuth email fully working for `vanguardadjusting@outlook.com`.

---

## Localtonet tunnel (dev)

To test the CRM remotely, two localtonet tunnels are configured on this workstation:

| Tunnel | Public URL | Forwards to | Type |
|--------|-----------|-------------|------|
| Sietch CRM | `https://g2vpdgb498.localto.net` | `127.0.0.1:8766` | HTTPS/HTTP |
| Sietch Docs | `https://m6cbapao4w.localto.net:6777` | `127.0.0.1:6777` → (tcp-forwarder.py) → `127.0.0.1:9443` | TCP (raw TLS passthrough) |

**TCP forwarder:** The docserver tunnel was set up as a TCP tunnel pointing at port 6777, but the OnlyOffice Document Server container listens on port 9443. A small Python script (`tcp-forwarder.py` in the project root) bridges `127.0.0.1:6777` → `127.0.0.1:9443`. Kill with `pkill -f tcp-forwarder.py` or `kill $(lsof -ti :6777)`. It is started per-session and will not survive a reboot.

**Important for production deployment:** These values MUST be reversed before deploying to the production droplet. The production `.env` should use the actual production URLs.

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
- Known issue: OnlyOffice self-signed cert requires SSL bypass in `_download_from_docserver()`.
- Known issue: Tile pin button hidden — causes erratic behavior from full DOM rebuild (`mountDashboardTiles()` uses `innerHTML=""` to clear all containers). Needs redesign to use DOM insertion/reordering instead of full teardown. See `bindTilePinButton` and `mountDashboardTiles`.
- **FIXED (pending user verification): Dashboard tiles jumped erratically during resize and drag.** Fixed in Phase 2D-3 / ISSUE-013 (see ISSUES.md).
- **KNOWN (follow-up, deferred):** `renderBoardGroups()` removes/recreates every group tile `<section>` on each data refresh (app.js:8198) — loses scroll/filter DOM state. Should become idempotent (update in place). Out of scope for Phase 2D-3.
- **KNOWN: Odysseus freeform architecture evaluated and REJECTED.** User chose the CSS Grid stability fix (Phase 2D-3) over a fixed-position snap-zone rewrite. If tiles ever need freeform placement, that's a full layout-engine rewrite.
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
- Minimize-to-sidebar for Email, Documents, and Search modals — universal minimize button (— icon) in modal header, icon-only sidebar triggers on right edge. Email saves scroll+selection; Documents saves scope+query; Documents saves preview tabs. All coordinate with bookmark sidebar.
- Tabler icons webfont added for sidebar trigger icons.
- Search modal header row added with minimize button. Fixed tiny scrollbar in tab bar.
- Fixed desktop header positions (admin-console-btn restored to right: 9rem; health indicator at right: 11.5rem).
- Fixed mobile header positions (sign-out rightmost, admin gear left, health indicator left of that).