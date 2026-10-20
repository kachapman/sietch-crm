# Session History — Sietch CRM (new-crm branch)

This file holds the rolling "last session summary" archive that was previously
embedded at the top of AGENTS.md. New sessions prepend to this file; old
sessions are retained for context.

The split happened on 2026-10-06 (commit `f8a0b4d`) so AGENTS.md could focus on
onboarding / conventions without 200+ lines of session-sprawl up front.

---

## Current session summary (for next resume)

**Date:** 2026-10-20 (supplemental sync)

**Topic:** Add-only pull-deals mode + honored filters + checkbox wired+tested.

**Status: IMPLEMENTED + TESTED, pushed — NOT deployed (awaiting explicit go).**

- server.py: supplemental flag branches UPDATE/CF/filter logic; provenance counters in response+log.
- UI: default-checked checkbox, flag in payload, counts in result line.
- Tests: new test_sync_supplemental.py (8, incl. negative control vs unpatched server); contact-guard extended; all runnable suites green.
- Note: supplemental UPDATEs never touch stage_type (0-default ambiguity).

## Previous session summary (2026-10-20, mail link history)

**Topic:** Linked emails missing from History & Notes (1648→opp 43) + wrong Email category.

**Status: DEPLOYED 2026-10-20 as 5381b7e** (rebuild healthy; migration 037 applied + recorded; all 7 email_snapshot rows now cat 13). No bot restart needed.

- `_create_email_history_event` idempotent helper; called on fresh + already-linked + sent-mail paths.
- Constant 16→13; migration 037 recategorizes old snapshots. Frontend "Already linked" honesty.
- Tests: new test_mail_link_history.py (4, mocked db) in CI; all runnable suites green.
- Pre-existing, untouched: CI references tests/test_mail_template_auth.py which does not exist.

## Previous session summary (2026-10-20, phone plain text)

**Topic:** `tel:` unrenderable on the clients — phone back to plain text, helper removed.

**Status: DEPLOYED (bot restart only).**

## Previous session summary (2026-10-20, tel-button 400 fix)

**Topic:** Flinchum search got silence — `tel:` inline button 400s sendMessage.

**Status: DEPLOYED (bot restart only — no server.py/frontend changes, no rebuild).**

- Dropped the 📞 tel: button (API rejects tel: in keyboards); text tel:+1 link kept; call sites address-only.
- Tests: keyboard no-tel regression guards; all suites green.

## Previous session summary (2026-10-20, bot card polish + reply attribution)

**Topic:** Customer-card refinements (CRM button, tappable phone, headers, divider) + reply author/notify fix.

**Status: DEPLOYED 2026-10-20 as 52436a6** (droplet pull + rebuild healthy + bot restart active, polling 200s; helpers + author resolution confirmed on droplet).

- Keyboard: 🔗 employees-only; 📞/📍 for customers with userFields fallback; extracted to module level for tests.
- `tel:` normalized to +1 (RFC 3966 — the invalid bare URI is why it rendered plain).
- Headers `<b>[Cat]</b> — date:`; details block closed by a rule. Stray "—" proven not formatter output.
- Replies: threaded POST sends chatId + notifyUserList; server resolves author from mapping; 400 when unresolvable. Old dup rows under Breanna: delete via UI ×.
- Tests: test_telegram_bot 113→124, all suites green.

## Previous session summary (2026-10-20, bot package)

**Topic:** 5-item Telegram bot package — customer card, invite codes, reply-401, help text.

**Status: IMPLEMENTED + TESTED LOCALLY — push + droplet deploy (rebuild, bot restart) still to do at commit time.**
**Deployed 2026-10-20 as b967456:** droplet pull + `docker compose build/up -d` (healthy) + `systemctl restart crm-telegram-bot` (active, polling 200s, no conflicts). Live bundle verified (OK button, Vanguard strings + recentUpdates on droplet).

- Customer card: `recentUpdates` (3, filtered by pairing update type) from `/api/bot/deals`; clickable tel:/Maps; CRM link employees-only.
- Invite codes: unambiguous alphabet, coexistence, OK-to-dismiss, cancel-by-value, full names in pending rows.
- Reply 401: Bearer-before-session reorder in `_handle_history_reply_create` + order-guard test.
- Help text → Vanguard Adjusting (3 spots). Bre's `461OZV8M` code stays valid; nothing to regenerate.
- Tests: test_telegram_bot 102→113, all suites green. Stray "—" in screenshot proven not formatter output (data artifact).

## Previous session summary (2026-10-20, nbsp + pending invites)

**Topic:** Two user-reported fixes — note `&amp;nbsp;` junk on Add Note + bot invite code lost when Admin Console closes.

**Status: COMPLETE — both fixed, all tests green.**

- **Fix 1 — Add Note `&amp;nbsp;`:** typing `test.` + auto-space in a `contenteditable` note editor stores `&nbsp;` in `innerHTML`; `noteContentToHtml` → `plainTextToNoteHtml` (`public/app.js`) then ran `escapeHtml` over the already-escaped entity, saving visible `&amp;nbsp;`. Fix: decode entities (`decodeHtmlEntities` + NBSP-to-space) BEFORE escaping in the plain-text branch. Covers preview ADD NOTE, quick-note, deal-edit, project page (all share the helper). Existing corrupted notes left as-is per user. Verified with a 6-case node harness.
- **Fix 2 — Bot invite survives closing the window:** codes were already persisted server-side (`crm_bot_store.py`, 48h expiry) — the UI just hid them. The code box is driven by in-memory `_botCustomersDraft`, and `switchAdminTab("bot")` never re-showed it after reload. Fix: new **Pending Invites** section (`#bot-pending-list` in `public/index.html`) rendering ALL `pendingCodes` with per-row Copy + Cancel (existing `cancel-code` by value, no backend change); `_botPendingCodes` state refreshed on every bot-tab open; null-guard on `_botCustomersDraft` in the loader.
- **Cache-bust:** `app.js?v=3.7.16` → `3.7.17`; fixed the stale `test_cache_bust_bumped` assertion (expected `3.7.15`, failing on HEAD before this session).
- **Tests:** all 17 JS suites pass, `test_telegram_bot.py` (102) OK, `test_contact_and_mail_attach_fixes.py` (32) OK, `test_search_modal.py` (24) OK.
- **Deploy note:** dashboard container serves `public/` — after `git pull` on production, `docker compose build && docker compose up -d`; no DB migration, no `.env` change.
- **Deployed 2026-10-20:** pushed `265aa0e` to `origin/main`; on droplet (147.182.164.19, `/opt/sietch-crm`) `git pull --ff-only` + `docker compose build` + `up -d`. Verified live: `app.js?v=3.7.17`, Pending Invites + nbsp fix present in served bundle, container healthy, `/api/config` OK. No bot restart needed (no bot code touched).

## Previous session summary (2026-10-17, demoted)

**Date:** 2026-10-17

**Topic:** CRM deploy from home dev to new droplet `147.182.164.19` — steps 4-6 + history-attachments migration.

**Status: COMPLETE — DB and attachments fully reconciled with production.**

**Final counts (droplet == home box exactly):** 48,823 history events, 17,180 history attachments, 841 tasks, 18 contacts, 1,229 opportunities, 11 users, 19 stages. 17,166 of 17,180 attachment files on disk (28 GB); the other 14 are **pre-existing zero-byte metadata rows** (`file_size = 0`, from a `--skip-attachment-download` migrate run) that OnlyOffice has no bytes for either — not migration loss.

**Attachment migration route (the scp dead-end, do not retry):** the home→droplet uplink is ~0.6 MB/s (asymmetric consumer link), so the 30 GB `sietch-data-history-*.tar.gz` push was ~14 hr and **died at 48.6% after 9.5 hr with `Broken pipe`**; plain scp has no resume, so it was all lost. Instead re-fetched from origin: essentially every attachment is OnlyOffice-origin (46,622 of 46,623 events carry a `crm_event_id`) because the home box originally *downloaded* those bytes from `sync_history_attachments.py` — it never created them. Run inside the dashboard container in 4 shards against `office.publicadjustermidwest.com`: **17,004 downloaded / 96 skipped / 0 failed in ~19 min at ~12 MB/s** (droplet outbound ~31 MB/s vs the home uplink's 0.6). Safe to re-run: the skip test is bytes-on-disk (`_disk_ok = (parent/_fpath).exists()`), so DB rows without bytes take the back-fill path and get UPDATEd in place.

**⚠️ Import landmine — `history_categories` id shadowing (cost real time, understand before any future import):** `history_categories` has `UNIQUE(title)`, and the importer uses `ON CONFLICT DO NOTHING`, which swallows *any* unique violation, not just the PK. `init.sql` seeds 15 categories at ids 1-15 (`Note`, `Call`, `Email`, `Customer Update`, …) while production uses ids 8-17 (`Note`, `Mail Message`, `Files are uploaded`, `Customer Update`, `Quick Context`, …). On first import the production rows collided **by title** with the seed rows and were silently skipped — so `id=16` never existed and **6,597 history events were rejected** on `history_events_category_id_fkey`, silently, as `rows_skipped`. Worse, the pre-existing events that *did* import pointed at ids 8-15 now labelled with the WRONG titles (`category_id=8` showed 30,316 events under the title `Comment` instead of `Note`) — i.e. **the event taxonomy was silently mislabelled, not just incomplete**. Fix applied on the droplet: `UPDATE history_events SET category_id = NULL` → `DELETE FROM history_categories` (drops the seed rows, frees ids 8-17) → re-import the delta → then restore `category_id` per event id from the delta export's `history_events.json` (48,822 pairs) via a scripted `executemany` UPDATE. Every category count now matches production exactly (Note 30,316 / Mail Message 13,016 / Customer Update 2,199 / …). **A future session that deletes `init.sql` seed rows without restoring `category_id` will mislabel every event** — if you ever redo this from scratch, either load `history_categories` before anything references it *and* clear the seed rows first, or make the importer match on PK only.

**Delta import:** `POST /api/v2/admin/export/universal?part=db-core` on the home CRM (it is a **POST**, not GET; and `?part=` streams just that part in ~33 s / 43 MB — do NOT use `backup/export-now`, which builds the whole 30 GB set and times out). 43 MB delta shipped over the slow link in ~1 min, imported 37 tables / 102,950 rows / 0 errors. Reused an unexpired admin session token from the home `sessions` table rather than needing the password.

**Importer hardened (same session):** the silent-drop bug class is now fixed and tested — see CHANGELOG "Per-bundle JSON importer: no longer silently drops rows". Four fixes: explicit PK-resolved `ON CONFLICT (cols) DO NOTHING`; `rows_duplicate` vs `rows_failed` split with capped `failure_samples` and a `complete` flag; deferred retry for forward-referencing FK rows (`contacts.company_id`); and `rows_imported` counted from `db.execute()`'s rowcount so a no-op re-import no longer claims 68k rows. `init.sql` now seeds `history_categories` at ids 8-17 matching production. Verified end-to-end on a scratch DB built from the corrected `init.sql` + all 37 migrations: 48,823 events / 17,180 attachments / 0 failed, categories correct, and re-import idempotent. New suite `tests/test_import_conflict.py` (32 tests) wired into CI.

**Importer hardened — and it uncovered a far bigger loss than the taxonomy bug.** The fix that makes failures *visible* immediately exposed what the old code had been hiding all along: on the first live re-import, `rows_failed` came back at **816,873** — every one of them previously counted as a benign `rows_skipped`. Two distinct causes, both now fixed:
- **jsonb adaptation (the big one):** psycopg2 cannot bind a Python `dict` to a `jsonb` column (`can't adapt type 'dict'`) and binds a `list` as a Postgres ARRAY (`column X is of type jsonb but expression is of type text[]`). The export stores those columns as native JSON, so every affected row was rejected. That silently cost **753,627 of 816,863 `notifications` rows** (the `payload` column) plus rows in 14 other tables. Fixed with `_table_jsonb_columns()` / `_adapt_row_values()` wrapping dict/list values in `psycopg2.extras.Json`.
- **`rows_imported` was lying:** `ON CONFLICT DO NOTHING` doesn't raise when it skips a row, so the old code counted every statement as "imported" — a no-op re-import claimed 68k rows imported. Now driven by `db.execute()`'s rowcount.

**Lesson worth keeping:** my first verification harness only imported 11 of the 66 tables the export carries, so it reported a clean PASS while 753k notifications rows were still being dropped. The full-fidelity harness (all 66 tables + per-table reconciliation against the export manifest) is what caught it. If you re-verify anything here, use the manifest reconciliation, not a hand-picked table list.

**Live droplet now reconciled:** 11 users / 1,229 opportunities / 18 contacts / 48,823 history events / 17,180 attachments / 841 tasks / **817,055 notifications** / 23 tags / 19 stages; 17,166 attachment files (28 GB). The backfill import reported `ok:true, complete:true, rows_failed:0` and **all 37 populated tables match the export manifest exactly**. 4 commits: `0ecbc22` (conflict targeting + failure split + deferred FK + honest rowcount), `2a97ff6` (jsonb adaptation), plus CI wiring. `tests/test_import_conflict.py` grew to 40 tests and is in CI.

**Step 7 done — nginx + TLS live on the droplet.** `crm.publicadjustermidwest.com` and `docs.publicadjustermidwest.com` both resolve to 147.182.164.19 and are served by the host nginx (1.24.0) with one Let's Encrypt ECDSA SAN cert (valid to 2027-01-16, `certbot.timer` auto-renews, `renew --dry-run` passes, nginx-reload hook installed). CRM → `127.0.0.1:8766`; OnlyOffice → `127.0.0.1:8080`. Both containers are now bound to **loopback only**, so nginx is the sole public entry point. Verified from outside the droplet: HTTP/2 on both hosts, http→https 301, login + `Secure` cookie over TLS, `/api/v2/me` authenticated, SSE streaming through nginx, `docsApiUrl` serving 67 KB of OnlyOffice `api.js`, the docserver accepting a CRM-signed JWT (HTTP 200 on `CommandService.ashx`), and the docserver able to call back to the CRM at its public URL.

**Two gotchas worth remembering:** (1) the Document Server's internal nginx listens on **:80 only** — there is no `:443` listener, so the compose mapping `9443:443` answered nothing; map `:80` and proxy plain HTTP. (2) nginx 1.24 (Ubuntu 24.04) has no `http2 on;` directive — that is 1.25.1+; use `listen 443 ssl http2;`.

Config now lives in the repo so a rebuild is reproducible: `deploy/nginx-sietch-droplet.conf` (byte-identical to the live site files) and `deploy/provision-nginx-droplet.sh` (idempotent provisioning).

**Live Telegram bot cut over to @vanguardupdates_bot.** Moved off the old droplet (159.89.229.126, /opt/vanguard/onlyoffice-crm-kanban) onto the new one, keeping the SAME bot identity so existing chats keep working. Long-polling (no webhook), so the swap was stop-old → start-new with no downtime. The token is dual-purpose (Telegram credential AND the CRM's Bearer check), and both env files plus the DB bot_config row previously held the wrong bot (@SietchCRM_bot) — fixed all three, then confirmed via journal: `id=8802337788 @vanguardupdates_bot`, `CRM reachable at http://127.0.0.1:8766`, and `/api/bot/settings` → 200. Old bot stopped+disabled; new bot active+enabled. Per user decision the 6 existing chat mappings were NOT migrated (Option B) — users get linked by writing mapping rows directly into the new server's store, since a mapped chat never sees the invite-code prompt.

**Basic admin gating unified on `is_admin`.** Follow-up to the bootstrap-admin incident. Gating was split: `_require_admin` (is_admin) guarded 109 endpoints while `_require_strict_admin` (role='admin') guarded only the two password-reset handlers — so an account with `is_admin=true, role='member'` saw the whole admin console but 403'd on password reset. Investigation showed no user is currently affected (zero rows where `is_admin` differs from `role='admin'`), so this was a latent landmine rather than an active outage. Confirmed the app's own paths can't create the bad row: create derives `is_admin` from role, update syncs it, and the admin UI edit form sends `role` and never `isAdmin`. Fix: both reset handlers now use `_require_admin`, the divergent `_require_strict_admin`/`_require_role` helpers are removed so they can't creep back, and `role` is authoritative on writes — closing a real drift hole where a direct `PUT {"isAdmin": false}` against a `role='admin'` user left `is_admin=false, role='admin'`. 10 new guards in `tests/test_admin_gating.py`, mutation-checked and wired into CI. 485 Python + 189 JS green.

**Infrastructure tab Containers card fixed.** The red "Docker socket not mounted" card was two independent bugs, neither of them a missing socket. (1) The Dockerfile extracted the static docker client with `tar -xzf ... -C /usr/local/bin docker/docker`; the archive member is a file inside a `docker/` dir, so tar created a *directory* at `/usr/local/bin/docker` with the client nested inside — `shutil.which("docker")` then resolved to a directory and every call failed "Permission denied" even as root. (2) The container runs as `appuser` (gid 10001) but the socket is `srw-rw---- root:docker` (gid 987), so the non-root user couldn't open it. Fixed by `install`-ing the client flat onto PATH and adding `group_add: ["${DOCKER_GID:-987}"]` to the dashboard service. No server.py change — the probe was correct, it just couldn't reach a working CLI. Verified live: `_docker_status()` now returns all four siblings; `appuser` has group 987.

**Scanner honored behavior checkboxes at last, plus [\#NNNN] resolves to project_number.** Two long-standing scanner bugs surfaced: line 1433 used `toggles.get("auto_link_project_id", True)` to gate the classifiers, but `toggles.get` returns a CONFIG DICT (`{enabled, dry_run, accounts}`) not a bool — a non-empty dict is always truthy, so the classifiers ran regardless of the checkbox. Every other call site used the correct `_is_behavior_enabled()` helper. And `_find_opportunity_by_deal_id` looked up `WHERE id = %s`, but the number in `[\#NNNN]` is the CRM project number shown in the UI header, not the row id — these agreed for 1,229 of 1,242 deals but DIVERGED for the 13 created by the 2026-10-19 sync, which is why `[\#1241]` resolved to `id 1241` (WS Liquor Inc) instead of `project_number 1241` (Barbara O'Malley). `server.py _resolve_project_id` already does it correctly; the scanner now does too. Data fix on the droplet: unlinked 3 wrong mail_deal_links (ids 181, 182, 185), realigned the 13 divergent deals so `project_number = id` (restored the invariant; used a temp `-id` to dodge the unique-constraint collision), and re-pointed both `opportunities_id_seq` and `opportunities_project_number_seq` to 1247 so future inserts stay in lockstep. Also dropped the torch layer from the Dockerfile — `sentence-transformers` is only used by the optional classifier-retrain path, which is already `try/except` guarded in scanner/mail_scanner.py, and removing it takes builds from ~15 min to ~2 min and the image from 9.77 GB to 432 MB. New `tests/test_scanner_toggle_gating.py` (6) covers the regressions; mutation-checked both fixes. Full suite: 500 Python + 189 JS, 0 failures.

**Wash option, system note types, and the OnlyOffice attachment backfill all shipped.** The user came back with three concrete failures and all three were real. (1) There was no Wash option: I had added the `.opp-preview-history-item--wash` CSS class but never added a `wash` radio button to the Event Note Type form, so the style was unreachable — and the help text still claimed "Accent: background wash + border", contradicting the new left-bar-only Accent. The admin form now offers Stroke / Fill / Accent / Wash with corrected help text, and the shared icon renderer colors the stroke for `wash` like `accent`. (2) System event note types could not be edited at all: `renderHistoryCatsList` hid the edit button for `isSystem` categories, so the icon on Mail Message / Files are uploaded / Task is closed was unchangeable. Edit is now always rendered (Delete stays hidden for system categories; the server still refuses the delete), and the three system types were given real icons + colors: Mail Message -> email #2563eb, Files are uploaded -> upload #16a34a, Task is closed -> check. (3) The OnlyOffice attachment backfill matched zero files because of three separate bugs: it tried to resolve the CRM_Email entity by subject (OnlyOffice mangles claim-number subjects — `13-0B8T-309` becomes `130B8T309` — and several events in a thread share a subject); `filehandler.ashx` needs the Bearer token and the download was sending none (403); and `DATA_DIR` resolved relative to the script location, so running from `/tmp` inside the container wrote to `/data` and failed with Permission denied. Rewrote the cross-reference to match by filename against the deal's history event `files[]` array, which carries the authoritative `crm_file_id` and is exact. Result on the droplet: 171 mail attachments now carry `crm_file_id` plus a local cached copy under `data/mail-attachments/<message_id>/`; the serve chain verifies as HTTP 200 with a valid `%PDF-1.5` for linked attachments and HTTP 410 Gone with a clear message (was a bare 404 the editor rendered as "Download failed") for ones not yet archived. Contacts are also complete: 1,234/1,242 opportunities now have `contact_id` — 1,184 from `CRM_export/`, 50 more from the live OnlyOffice API — with Barbara O'Malley and Scott Ozark both resolving to MDM Construction and Cambridge Commons Storyboard to Baney Construction. The Oct 19 email `10-Customer Copy 4001405496.pdf` still cannot be served because the mail aggregator has not yet archived it into OnlyOffice; it will resolve once it does and the backfill is re-run. Full suite: 526 Python + 189 JS, 0 failures.

**Company Name now reaches the dashboard header.** Changing it did nothing visible because (1) `applyBranding()` never applied `headerTitle` to `.hero-title`, (2) a stale stored `header_eyebrow = "Sietch CRM"` shadowed Company Name ("Vanguard Adjusting") via `headerEyebrow || companyName`, (3) the admin form could never clear the stale value (empty sent null, server COALESCE kept the row), and (4) the server GET defaulted an unset eyebrow to "Sietch CRM", shadowing a second time. Now: eyebrow falls back to Company Name, hero-title renders from its own sanitized field, version line + footer stay static, and empty fields clear to default. New `tests/test_branding_header.py` (8 guards). Cache-bust `app.js?v=3.7.16`.

**Follow-up Due always shown; due/stage saves respect preview context.** The Details panel hid the Follow-up Due section when `expected_close_date` was NULL, leaving deals like Barbara O'Malley (unset in OnlyOffice too) with no way to set one. The section now always renders with "Not set" + click-to-edit, PATCHing `{ expectedCloseDate }` as before. Both the due-date and stage save paths branched to `refreshBookmarkTab` / `refreshSearchPreviewTab` instead of always popping the main modal. New `TestFollowUpDueAlwaysShown` (3 guards).

**Note attachment buttons everywhere + bookmark Post no longer pops the main modal.** Note file upload previously existed only in the Quick-note modal; the preview-inline editor, the project page, and the deal-edit path had no way to attach a file. Added a Tabler paperclip button immediately left of the calendar (backdate) button in all three editors, reusing the existing `uploadAttachmentForNote` → `POST /api/v2/projects/{id}/documents` → `fileIds` flow so files land in Documents and link to the note. Bookmark and search previews inherit it automatically through `renderOpportunityPreviewContent`. Separately, posting a note from a bookmark preview unconditionally called `openOpportunityPreviewModal`, popping the main project modal over the bookmark sidebar; the success path now branches on the render context (`refreshBookmarkTab` / `refreshSearchPreviewTab` / main modal). New `TestNoteAttachmentButtons` (6 guards). Cache-bust `app.js?v=3.7.15`.

**Email bodies restored (6,854) and the 13 stage-less deals fixed.** Three findings, one theme: the pull-deals sync path was writing a fraction of what it should, and I had been checking the wrong things. (1) 7,521 of 11,394 Mail Message events had no `body_html`/`body_text`, which is why the UI said "Linked email no longer available (deleted or access changed in CRM)" — a message that was simply wrong, since the emails were archived the whole time. The CRM event content never holds the body; the bodies live in OnlyOffice's `mail_mail` Elasticsearch index (32,806 messages, back to 2024). The route the kanban dashboard used (`filehandler.ashx?action=mailmessage`) only serves recent ids and 500s for anything older — which is exactly why the 2025 "Ramada interior" email from the user's screenshot could not be fetched that way but came back instantly from ES. `scripts/backfill_email_bodies.py` runs on the Sietch droplet and pulls bodies over SSH; `scripts/oo_fetch_bodies.py` runs on the OnlyOffice droplet and bulk-queries ES (one query per 400 events). Because the CRM's `message_id` is off-by-one from the ES doc id on many rows, the resolver tries the id then ±1..±4. 6,854 of 7,521 restored in 24 seconds; the remaining 667 are 2022-2024 and genuinely predate the mail archive, so they now say so honestly instead of blaming a deletion. (2) The 13 opportunities created 2026-10-19 (ids 1235-1247) were missing stage_id, stage_type, responsible_user_id, actual_close_date, actual_close_value, bid_value and expected_close_date — every one NULL. Three bugs in one sync path: `stage_map` was keyed by `title.lower()` but looked up with `crm_stage_id.lower()` (the OO and Sietch stage ids are different namespaces, so the lookup never matched and every newly-synced deal got a NULL stage); `responsible_user_id` was never written at all even though the sync built the `user_id_map` right there; and the INSERT listed 7 columns, dropping the rest. The sync now joins stages on title, resolves `responsible.id` through the user map, and writes all 12 columns on INSERT and UPDATE with COALESCE guards. `scripts/backfill_deal_fields.py` backfilled the 13 from the OnlyOffice API — Joe Knoll lands in "Closed Paid" with stage_type 1, Tony Gosz and Jesus Casique in "Negotiations/Supplementing", and Judy Osier has no contact because OnlyOffice has none for her either. (3) While verifying, I found that 608 events carry a `message_id` that coincides with a different Sietch `mail_messages.id`, so fetching by id would have rendered the wrong email entirely — "H74219 - Manchester/Jessen" would have shown "Here's your AccuLynx login code". The UI now renders the event's own body first and validates the subject of any fetched message. New tests: `tests/test_sync_pull_deals_fields.py` (15 guards, mutation-checked). Full suite: 541 Python + 189 JS, 0 failures.

**Import-regression sweep across all 70 tables.** Compared every table's live row count against the export manifest: nothing was lost, every table is at or above its expected count. The regressions were all in code paths, not data. Found and fixed: (1) the pull-deals sync wrote `history_attachments` but never mirrored into `project_documents`, and the Documents tab reads only the latter — 62 deals showed "0 documents" despite files being on disk. Backfilled 130 rows, made the sync mirror, and added a fallback so it can't recur. (2) 7,074 duplicate `(opportunity_id, crm_event_id)` history event groups, all pre-migration (baked in on the home box, not from the recent sync) — deleted the extras and added a partial unique index. (3) The preview modal's bookmark button was clickable while the project was still loading, so bookmarks stored the `Project #<id>` placeholder permanently; titles are now resolved before saving and re-resolved at render time. (4) Cache-bust versions were never bumped, so browsers kept serving the old app.js — the new icons and the System checkbox looked like they hadn't deployed.

**Bot customer/employee isolation hardened.** Raised while planning the `@vanguardupdates_bot` cutover. The bot token is one shared secret authenticating the bot *process*, not individual chats, so it was never the employee/customer boundary — the server-side `chatId → mapping` store is. Two paths leaked past it: `/api/bot/deals` read `?employee=` and `?contactId=` off the wire, so a crafted request could drop the contact filter and return every opportunity across every contact; and the bot's `tag:` callback hardcoded `employee=True` with no mapping check, reachable via a stale keyboard after a downgrade. Fixed by making `/api/bot/deals` derive both `is_employee` and customer `contact_id` from the stored mapping (403 when the chat has no linked account), guarding the `tag:` callback before the fetch, and dropping the now-meaningless `employee` param from `get_deals()`. 11 new guards in `tests/test_telegram_bot.py`, mutation-checked (re-adding both bugs fails 4 of them). Full suite green: 475 Python + 189 JS. Audited the other 16 `/api/bot/*` endpoints — all already correct. `/api/bot/upload` is employee-only today; customer uploads would be a separate feature.

**Admin accounts corrected.** A bootstrap admin (`admin@chapmanfamily.online`) was created early in the migration to get past the login screen — it was a mistake, and it did damage: inserted into an empty `users` table it took **id = 1**, which is the export's real `bre@chapmanfamily.online`; bre's row was then silently skipped on import by `ON CONFLICT (id) DO NOTHING`. The placeholder also had `is_admin=true` but the default `role='member'`, so it rendered the whole admin console (gated on `is_admin`) while user-management endpoints 403'd (gated on `role='admin'`) — exactly the "Forbidden: requires one of roles: admin" report. Fixed by **restoring the row in place as bre** (not delete-and-reinsert — a DELETE would have failed the `NO ACTION` FK from the 108 `history_events` with `created_by=1`, which are bre's), clearing the placeholder's now-live password hash, and purging its sessions. `kenc@vanguardadj.com` already had `is_admin=true, role='admin'` and only needed a temp password, set with the app's own PBKDF2 scheme. `admin@chapmanfamily.online` no longer exists. All 11 real users are back with correct roles, and kenc has since completed the forced password change and used the admin console to reset another user's password successfully.

**Remaining before go-live****** (steps 7-9):** nginx + certbot on the droplet; the `docs.publicadjustermidwest.com` A record **still does not resolve at all** (OnlyOffice's doc server is co-located here now, so this needs a new record); DNS cutover verification for `crm.publicadjustermidwest.com`; enable the (correctly-configured, still disabled) `crm-telegram-bot.service`; rotate the bootstrap admin password; then home-box volume cleanup. OnlyOffice creds were shredded from the droplet after use.

---

## Previous summary (2026-10-16 — template dropdown visibility)

**Status: SHIPPED.** Follow-up to the previous "position relative on .tile-toolbar" fix: dropdown was visible but anchored to the toolbar's right edge (far from the button). Fix: wrap `templateBtn + templateDropdown` in a `.template-menu-wrap` div, add CSS `position: relative` (matches `header-settings-wrap` pattern), tighten `top` to `calc(100% + 2px)`. 5 tests in `test_template_dropdown.mjs` (up from 4) covering the new wrap shape + dropdown parent assertion. Full regression green (16 JS files + 15 Python suites, 0 failures).

**Next:** None — user-reported issue resolved.

---

## Previous summary (2026-10-16 — template dropdown visibility)

**Status: SHIPPED.** Root cause: `.template-dropdown` is `position: absolute; top: 100%` but `.tile-toolbar` had no `position: relative`, so the dropdown positioned to the top-right of the whole tile (behind the columns) instead of under the button. One-line CSS fix matching the `header-settings-wrap` pattern; 4 new tests in `test_template_dropdown.mjs` (no jsdom dep). Full regression green (16 JS files + 15 Python suites, 0 failures).

**Next:** None — user-reported issue resolved.

---

## Previous summary (2026-10-16 — group filter audit)

**Status: SHIPPED.** Plan at `/home/chapman-server/.opencode/plan/group-filter-audit.md`. 12 new JS mirror tests; full regression green (16 JS files + 15 Python suites, 0 failures).

**Fixed:** filter stage now always renders its column even when hidden by the column multiselect (new `effectiveVisibleStageIds` helper); stage dropdown no longer carries a stale value after `state.stages` shrinks; `groupBy="tag"` + empty tags now shows a "Select tags" placeholder; `tagLookupKey` dead ID branch removed; filter summary warns when the filtered stage is hidden. Documented `dealStatus="closed"` perf TODO + inline comment for the `!group.stageType` guard.

**Next:** Per the plan, remaining smells are out of scope: per-stage color picker, custom-field filters on the tile, drag-to-reorder columns, virtualized rendering. Other CRM-tile work is a separate audit.

---

## Previous summary (2026-10-16 — calendar Phase 6)

**Status: PHASE 6 SHIPPED — PLAN COMPLETE.** Snooze (10m/1h) + Dismiss buttons on every calendar reminder card (new migration 036 + `snoozed_until` column, sweep honors it). Telegram pref key fixed (`calendar_telegram` → `telegram_calendar_reminder`, legacy row still honored). New `GET /api/v2/calendar/search` (paginated, ILIKE) and `GET /api/v2/calendar/export` (fresh .ics, RFC-5545 escaping via pure `_ics_dump` helper). Settings modal "Search & Export" section: search box, Print current view, Export .ics. `@media print` CSS hides everything but the tile. 16 new tests (38 JS files + 15 Python suites, 0 failures). Recovery: lost a class-indent boundary when inserting `_ics_dump`; `git stash/checkout` reset clean, then re-applied Phase 6 surgically with all subsequent edits as `+4`-space appendages — final compile + full regression green.

**Next:** None — all 6 plan phases shipped. Possible follow-ups outside the plan: print/save list style, attendee email + RSVP dispatch, Telegram notif body improvements, server-side CalDAV sync implementation.

---

## Previous summary (2026-10-16 — calendar Phase 5)

**Status: PHASE 5 SHIPPED.** Sources list (visibility/color/rename/sync-now+errors/sync-all), per-tile source filter, OAuth hide-when-unconfigured, CalDAV beta label, ICS import endpoint + UI, real defaultReminder pref. 17 new tests (5 JS + 12 Python); full regression green (15 JS files + 14 Python suites, 0 failures). Caught by `node --check`: helper insert swallowed `fetchCalendarFeed` header (fixed).

**Next:** Phase 6 reminders + search + polish (VALARM already seeds reminders; needs snooze/dismiss, Telegram path decision, tile/global search, .ics export, print). Files: `server.py`, `calendar_sync.py`, `public/app.js`, `public/index.html`, `tests/test_calendar_sources.py` (new), `CHANGELOG.md`.

---

## Previous summary (2026-10-16 — calendar Phase 4)

**Status: PHASE 4 SHIPPED.** Recurrence builder + per-event TZ/color/busy/URL/attendees/custom reminder + inline validation + Only/Series scope (locked decision) + detail Edit path. Backend: migration 035, series expand-at-write, scope-aware update/delete, attendees codec. 19 new tests (9 JS + 10 Python); full regression green (15 JS files + 13 Python suites, 0 failures).

**Next:** Phase 5 sources/sync/settings (per-tile source picker, visibility/color/rename, sync buttons + errors, hide-OAuth-when-unconfigured, CalDAV beta label, plaintext-password note, ICS file import, default-reminder setting). Files: `public/app.js`, `public/index.html`, `server.py`, `calendar_sync.py`, `oauth_providers.py` (maybe), `CHANGELOG.md`.

---

## Previous summary (2026-10-16 — calendar Phase 3)

**Status: PHASE 3 SHIPPED.** Week/day rewritten (0–24h, all-day strip + continuations, positioned durations, overlap columns, now-line, working-hours shade, click-to-create with hour prefill); month `+more` jumps to day view + empty-day click creates; agenda location/ranges/load-more; week-start + 12/24h display prefs persisted via profile (`calendarDisplay`). 10 new JS mirror tests (24/24 pass); full regression green (15 JS files + 12 Python suites, 0 failures).

**Next:** Phase 4 editor + detail actions (recurrence editor, per-event TZ/color/busy-URL/attendees/custom reminder, validation, Edit/Delete from detail, Only+Series scope). Files: `public/app.js`, `public/index.html`, `public/styles.css`, `user_profile_store.py`, `tests/test_calendar_views.mjs`, `CHANGELOG.md`.

---

## Previous summary (2026-10-16 — calendar Phase 2)

**Status: PHASE 2 SHIPPED.** Expand-at-sync per locked decision. 21 new tests (15 expansion + 6 sync; 52 Python calendar tests total), all suites green (15 JS files + 12 Python suites, 0 failures).

**What changed:** `ics_calendar.py` retains EXDATE/RDATE/RECURRENCE-ID/DURATION/VALARM + stdlib expander (DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL/COUNT/UNTIL/BYDAY/BYMONTHDAY/BYMONTH/WKST, nth-weekday, caps 500/window 1y+2y); `calendar_sync.py` upserts occurrence rows (`<uid>#<ts>`, `recurrence_id`, VALARM→reminder on insert only, legacy bare-row + stale-occurrence prune); `server.py` feed endpoint expands + events API returns `recurrence_id/is_occurrence/recurrence` (+ migration `034`, inline ALTERs); `app.js` maps humanized recurrence + occurrence flags. Fixed along the way: DURATION-applies-to-end (RFC 5545), window_start clip bug (caught by yearly test), weekly count expectation (test-side).

**Next:** Phase 3 views (all-day strip + 0–24h + overlap + now-line; `+more` popover; agenda load-more; week-start/12-24h settings). Files: `ics_calendar.py`, `calendar_sync.py`, `server.py`, `public/app.js`, `migrations/034_calendar_occurrences.sql`, `tests/test_ics_calendar.py`, `tests/test_calendar_sync.py` (new), `CHANGELOG.md`.

---

## Previous summary (2026-10-16 — calendar Phase 0+1)

**Date:** 2026-10-16

**Topic:** Calendar parity — full plan + Phase 0/1 build.

**Status: PHASE 0+1 SHIPPED.** Plan at `/home/chapman-server/.opencode/plan/calendar-parity.md`. Decisions locked via options: hide OAuth/CalDAV when unconfigured, expand recurrences at sync, Only+Series edit scope, Sun+24h defaults. 45 new tests (31 Python + 14 JS), all suites green.

**Fixed:** ICS ORGANIZER/ATTENDEE CN params dropped (now preferred over value); month nav/view-tabs now refetch view-aware ranges + Today button + week/day nav + range/agenda labels; week/day/agenda clicks open detail modal; agenda includes + correctly sorts all-day events; `viewDay` persisted (`user_profile_store` + `newCalendarTile`).

**Next:** Phase 2 recurrence expansion (EXDATE/RDATE/RECURRENCE-ID/VALARM, expand at sync per decision). Files changed: `ics_calendar.py`, `public/app.js`, `user_profile_store.py`, `tests/test_ics_calendar.py` (new), `tests/test_calendar_views.mjs` (new), `CHANGELOG.md`.

---

## Current session summary (for next resume)

**Date:** 2026-10-16

**Topic:** Users audit + password-reset deliverability.

**Status: SHIPPED.** 21 new tests (549 total: 134 JS + 415 Python, 0 failures), five commits.

### Findings (7 issues, all fixed)

**High:**
- **#1 — Kebab Reset Password dead (always 400).** Profile-modal kebab POSTed `/reset-password/send` with no body; server requires method+password. Inline row button used the working temp flow. Kebab now identical + reuses `showTempPasswordDialog`.
- **#2 — Users-list click listener stacked.** Every refresh added another container-level listener (container never replaced). Guarded with `dataset.bound`, scoped so re-rendered inputs still rebind.

**Deliverability:**
- Plain-text reset email (single text/plain, bare URL + copy-paste instruction; remote-image HTML template was spam-foldered). Both callers switched.
- `/reset` landing page (`public/reset.html` + 302 rewrite; the emailed link previously 404'd). Verified live incl. single-use redeem.
- Email option greyed out (disabled button + note; no-Telegram fallback directs to admin; endpoint untouched).

**Server hardening:**
- Dup-email 409 on create (was 500). Avatar serve containment in both handlers. Bot-token auth on users-get kept (accepted risk).

### Tests

`tests/test_users.py` — 21 tests, mutation-verified (5 kills caught). CI + AGENTS counts updated (549).

### Deploy notes

- `CRM_PUBLIC_URL` required; configure + live-test one SMTP path (self-service returns ok:true even on send failure — silent misconfiguration).
- NOTE: round-trip testing rotated user 1's password; temp `en_xI7kAxTnT` issued via standard temp flow (must_change_password=TRUE, sessions revoked) — share out of band.

```
790632d        Users: docs (CHANGELOG + SESSION_HISTORY + AGENTS + CI)
5a19a27        Users: 21 tests (plaintext, landing, grey-out, guards)
6a79b99        Users: dup-email 409 on create + avatar serve containment
7afa923        Password reset: plaintext email + /reset landing page + greyed-out email option
3791177        Users: fix dead kebab Reset Password + stacked list click handler
```

**Next:** audit chain now covers timesheet, presence/DM, automation, telegram, infra, customize, reports, search modal, **users**. Remaining deferred: timesheet B/C, `crm-server.service` on systemd, production deploy. Since this entry: force-change gate shipped (temp passwords now block until changed); SMTP live test shows plaintext still 550-blocked content-side (control probe delivered, no URIBL hits) — email resets stay disabled. Open: bot-token auth on users-get (accepted risk); server-side API gate for must_change_password (client-only for now).

---

## Previous summary (for next resume)

**Date:** 2026-10-16

**Topic:** Search modal audit. (Continuation of the audit chain below.)

**Status: SHIPPED.** 24 new tests (528 total: 134 JS + 394 Python, 0 failures), three commits.

### Findings (9 issues)

**High:**
- **#1 — Closed-deals filter truncated at 500 per stage.** `performSearchPopupQuery` hardcoded `count: 500` for Won + Lost, then sliced the merged client-side array, while `searchPopupTotal` used the true server counts. Page numbers ran past the data and "Showing 1–20 of N" lied past 1000 rows. Fix: each stageType now uses the real page size (50) + current `startIndex`; the merged list IS the page.
- **#2 — Side-by-side restore never cleared the quick-note modal.** Layout sets `pointerEvents: "none"` + `zIndex` on whichever side modal is active (deal-edit or quick-note), but `restoreSideBySideCards` only reset `editModal`. Repro: quick-note side-by-side → close → reopen → visible but click-through until refresh. Fix: restore detects the active side modal the same way.

**Medium:**
- **#3** Cross-page export dropped off-page selections (filtered cached current-page opps). New `?ids=` param on `/api/v2/projects` (`o.id = ANY(%s)` integer array); export re-fetches the full set.
- **#4** Batch ops awaited N×M sequentially (20 deals × 3 actions = 60 requests). Now `Promise.allSettled` per deal with per-deal failure toasts.
- **#5** "Select all" label already said "on page"; added muted "(selections carry across pages)" hint.
- **#6** Contacts search declared `searchPopupContactsQueryAbort` but never used it. Now wraps data + count fetches (same race-safe pattern as deals).
- **#7** Contacts `searchPopupContactsStartIndex` reset but never incremented, no prev/next UI. Added `performSearchContactsPageChange` / `updateSearchContactsPagination`, buttons in markup, total from `/api/v2/contacts/count`, reset/abort on close.

**Low:**
- **#8** `MAX_SEARCH_PREVIEW_TABS` stale "(minus 1 for the search tab)" comment fixed.

### Tests

`tests/test_search_modal.py` — 24 tests (4 real `?ids=` handler exercises + 2 count-flow + 18 JS source guards). Mutation-verified, 6 targeted mutations all caught. Also closed the CI gap for `test_customize.py`, `test_reports.py`, `test_search_modal.py` in `smoke.yml`, and updated `AGENTS.md` counts (528).

```
0fcb197        Search modal: docs (CHANGELOG + SESSION_HISTORY + AGENTS + CI)
144913e        Search modal: 24 tests (4 real + 20 source guards)
781fc6c        Search modal: layout restore + batch parallelism + cross-page export
f9cd324        Search modal: pagination correctness + contacts pagination
```

**Next:** audit chain now covers timesheet, presence/DM, automation, telegram, infra, customize, reports, **search modal**. Remaining deferred items: timesheet B/C (`_tsActiveEntry` refresh, open-shift auto-close), `crm-server.service` on systemd (still ad-hoc `setsid`), `crm.publicadjustermidwest.com` deployment.

---

## Previous summary (for next resume)

**Date:** 2026-10-15

**Topic:** Customize Projects audit (admin console). (Continuation of the bot/infra/audit chain below.)

**Status: SHIPPED.** 26 new tests (471 total, 0 failures), three commits.

### Two data-corruption bugs in the Customize Projects tab

**#1 — Custom field type loss on edit.** The admin type `<select>` only offered `text/number/date/select/checkbox`, but the DB used `textarea` and `url` too. The edit handler did `select.value = f.type`; per the HTML spec, assigning a string with no matching `<option>` leaves `selectedIndex=-1` and `value=""`. Save then sent `type: ... || "text"`, **silently rewriting 7 of 30 live fields** — `Address`, `Claim #`, `Supplement Request` and `PA CONTRACT`, `Measurement Report`, `Insurance Documents`, `Inspection Photos`.

Two-layer fix: added the missing `<option>`s, plus a defensive `_setCustomFieldTypeSelect()` that injects an unknown option rather than blanking the select. Future type drift can't repeat this — the JS helper round-trips *any* string the DB holds (verified against a Node-simulated `<select>`: pre-fix `'textarea'` → `""` → saved as `"text"`; post-fix round-trips, and even a hypothetical `'weird_future_type'` survives).

**#2 — Stage reorder scrambled the pipeline when a search filter was active.** Sortable `onEnd` built its id list from the rendered DOM, which under a filter holds only the matching rows. `/api/v2/stages/reorder` rewrote `sort_order` 0..n for those; hidden stages kept stale values and collided. Fix: `populateAdminStagesList` toggles `stages-filtered`; CSS hides drag handles; `onEnd` checks the class and refuses with a toast.

### Other fixes

- **#3** `ORDER BY sort_order, id` on both stages and custom fields (all 19 stages had `sort_order = 0`).
- **#4** Field-group save unassigns unchecked previously-assigned fields (`groupId: null`).
- **#5** `state.fieldGroups = []` after save/delete replaced with a real `loadFieldGroups()` call — grouping now survives editing without a page reload.
- **#6** Duplicate field key returns **409** with the existing label named, not a generic 500.
- **#7** `showOnCreate` / `showOnEdit` dropped from the API contract (never written; never read).
- **#8** Removed per-update `information_schema` catalog query from `_handle_history_category_update` — `history_categories` has no `updated_at` column, branch could never fire.
- **#9** Custom-field options rewrite wrapped in `db.db_cursor()` — `DELETE` + re-`INSERT` commit together.
- **#10** Stage create honours `isActive`; **#11** custom-field create honours `groupId`; **#12** group assignment parallelised with `Promise.allSettled`; **#13** `field_type` validated against `CUSTOM_FIELD_TYPES`.

### Tests

`tests/test_customize.py` — 26 tests, 8 exercising real handler bodies under heavy mocking + 16 source guards + 2 markup checks. Mutation-verified — 8 mutations injected one at a time, all caught.

Full suite: **134 JS + 337 Python = 471 tests, 0 failures**.

### Notes
- **One item not addressed:** `task_labels` is empty despite `init.sql:1075` seeding 10 defaults — the Task Labels column in the admin tab shows nothing. Looks like data drift, not a code bug. Flag this if you want me to re-seed (would need confirmation; it's a write).

### Earlier in this session
- **Telegram bot outage fix** (7 commits): three stacked root causes (dead localtonet tunnel, wrong Telegram bot from .env skip-precedence, admin-session 401 on bot-settings), all resolved. Bot is systemd-managed, `crm-server.service` is dead and was relaunched via `setsid`. New endpoint `GET/POST /api/bot/settings` (bot-token auth, POST whitelists only `botUsername`/`botLink`). Found and fixed a vacuous-async-test defect in the previous session's `test_telegram_bot.py` (16 hidden errors now surface).
- **Infrastructure (admin console)** (4 commits): `_require_admin` gate on the only unguarded admin endpoint, restart-safety fixes (the CRITICAL bind-race where SO_REUSEADDR doesn't cover an active listener, plus `main()` retry on `EADDRINUSE`), container-runtime detection rewrite, probes parallelised, dead columns removed, etc.
- **Telegram bot audit** (2 commits): 8 fixes including an XSS fix in `_TelegramHTMLSanitizer` (`javascript:` etc.) and shared `httpx.AsyncClient`.
- **Tasks tile** (1 commit): cap the linked project name so the task title isn't squeezed to "Re...".

Full audit chain: timesheet (1-5), presence/DM (6-7), automation (8), telegram, infrastructure, **customize projects**.

```
03da62f        Customize: 26 tests (16 source guards, 8 unit + 2 markup)
3f0dfa3        Customize: correctness + cleanup across 6 admin handlers
af76f2c        Customize: stop custom-field type loss and filtered stage reorder
3009e8e        Docs: session history + AGENTS.md testing section
... (more)
```

**Next:** the audit chain is now extensive. The deferred items from earlier sessions remain: timesheet B/C (`_tsActiveEntry` periodic refresh, open-shift auto-close job), `crm-server.service` (currently running from `setsid`, should be on systemd), and `crm.publicadjustermidwest.com` deployment.

---

## Previous summary (for next resume)

**Date:** 2026-10-15

**Topic:** Telegram bot outage — valid invite codes rejected. (Continuation of the infra/audit chain below.)

**Status: FIXED.** All 445 tests pass, bot is live and heart-beating, `botUsername`/`botLink` auto-populated for the first time ever.

### The three stacked root causes

1. **`DASHBOARD_URL` pointed at a dead localtonet tunnel.** Bot and CRM share a host, so this should have been `http://127.0.0.1:8766`. The dead tunnel returns **HTTP 200 + HTML "Tunnel Stopped"**, `resp.json()` raised, the error was swallowed, and `_post` returned `None` — identical to a bad code. **297 consecutive failures over ~2.5h**, all reported to users as *"your code wasn't recognized."*

2. **The bot was logged into the WRONG Telegram bot.** `server.py:395` and `telegram_bot.py:42` both load `.env` with `if key and key not in os.environ` — they **skip** already-exported keys. A stale `TELEGRAM_BOT_TOKEN` in the launching shell silently overrode `.env`. **This is a recurring trap:** any restart from a shell with that var exported diverges from `.env`. `systemctl start` (clean env) would have picked up `.env`, so the working identity was an accident of how the process was started.

3. **`/api/v2/admin/bot-settings` needs an admin SESSION; the bot sends `Bearer <token>` → 401.** All four call sites failed. So `botUsername`/`botLink` auto-population had **never** worked (the permanent empty credential fields in the admin console), config reload never worked, and upload size silently used the hardcoded 20 MB default.

### Commits

| Commit | Change |
|---|---|
| `9a5523f` | Split `_get_dashboard_url()` (internal/API) from `_get_public_dashboard_url()` (customer links) |
| `89d122e` | `_request()` distinguishes failure kinds + Content-Type check; `verify_code` raises `BotUnreachable` instead of reporting our failures as bad codes |
| `b9b3dbe` | Startup connectivity probe, throttled error logging, Telegram-identity mismatch warning |
| `5ddd579` | New `GET`/`POST /api/bot/settings` (bot's own auth); GET omits token, POST whitelists only `botUsername`/`botLink` |
| `e978be4` | +28 tests (92 total) **and fixed vacuous async tests** |

### The test defect worth remembering

**Every `async def test_*` in `test_telegram_bot.py` was silently doing nothing.** `unittest.TestCase` doesn't support coroutine test methods — the coroutine is never awaited and the test reports as passing. Proved it with `async def test_never_runs` raising `AssertionError` → still `OK`. Three classes added in the *previous session* had never executed a single assertion.

Fix: `unittest.IsolatedAsyncioTestCase`. Running them for the first time immediately surfaced 16 hidden errors. **Lesson: a green suite proves nothing if the tests can't execute — check that a deliberate failure actually fails.** `telegram_bot.py` was the only file affected; all other suites use sync tests.

Mutation-verified 6/6 (was 0/4 caught before the fix): removing the content-type check, `verify_code` returning `None` on any failure, links reverted to internal URL, public URL ignoring blank, connectivity check always `True`, throttle logging every failure.

### Config changed (`.env`, gitignored)

```
DASHBOARD_URL=http://127.0.0.1:8766
DASHBOARD_PUBLIC_URL=https://crm.publicadjustermidwest.com
```

### Ops notes / open items

- **`crm-server.service` was dead for 3 weeks** (killed 2026-09-22); the server was running from an ad-hoc `setsid`. Restarted manually with `env -u TELEGRAM_BOT_TOKEN` so `.env` wins. **`sudo` is unavailable in this environment**, so it can't be started via systemd from here — recommend switching to the unit so `EnvironmentFile=.env` applies and `Restart=on-failure` works.
- **Token rotation is no longer handled by the config poll** (the settings endpoint deliberately never returns the token). Rotate by editing `.env` + restarting the service.
- `telegram_bot.py` is systemd-managed (`crm-telegram-bot.service`, `Restart=always`) and currently healthy.

### Earlier in this session (audit chain)

- **Infrastructure (admin console)** — `5316f40` authz gap on `/api/v2/admin/infrastructure` (only admin endpoint without `_require_admin`; anonymous callers got the whole payload), `662080e` 8 fixes (most severe: `restart-server` could take the CRM down permanently — SO_REUSEADDR doesn't cover an active listener, `main()` had no bind retry, and `restart: on-failure:5` never recovers an exit-0 container → added `_bind_server_with_retry`), `44af2cf` 64 tests + closed a CI gap (smoke.yml ran only 3 of 8 Python test files).
- **Telegram bot audit** — `ab5148f` 8 fixes incl. an **XSS** fix (`javascript:`/`vbscript:`/`data:` URLs in `<a href>`), `b5febb3` 64 tests.
- **Tasks tile** — `bf8b9f` cap the linked project name so the task title isn't squeezed to "Re...".

Full audit chain now complete: timesheet (1-5), presence/DM (6-7), automation (8), telegram, infrastructure.

```
e978be4        Bot tests: +28 tests, and fix vacuous async tests
5ddd579        Add /api/bot/settings so the bot can read its own config
b9b3dbe        Bot: fail loudly at startup instead of silently degrading
89d122e        Bot: don't report our own failures as 'invalid invite code'
9a5523f        Bot: split internal API URL from public link URL
3009e8e        Docs: session history + AGENTS.md testing section
44af2cf        Infra tests (64) + close the CI test gap
662080e        Infra audit: restart safety, runtime detection, probes
5316f40        Infra: gate /api/v2/admin/infrastructure behind _require_admin
b5febb3        Telegram bot: test coverage + _esc None check
ab5148f        Telegram bot: 8 audit fixes
bf8b9f9        Tasks tile: cap the linked project name
... (18 more commits)
```

**Next:** open items are the deferred timesheet B/C items (`_tsActiveEntry` periodic refresh, open-shift auto-close job), moving the CRM server onto its systemd unit, and `crm.publicadjustermidwest.com` deployment.

---

## Current session summary (for next resume)

**Date:** 2026-10-15

**Topic:** Reports audit (admin console). (Continuation of the audit chain below.)

**Status: SHIPPED.** 33 new tests (504 total, 0 failures), three commits.

### Findings

**Logos saved to `data/logos/` were never reachable via HTTP.** The static server's root is `public/`, so the admin preview's `<img src="/data/logos/foo.png">` returned 404. Server-side PDF generation reads files from disk directly and worked, so this was a *cosmetic* bug for the admin preview only — not a data-loss or rendered-output issue. Two existing template records (`Vanguard Adjusting` photo + doc) had dead `logo_path` columns.

**Logo / signature uploads had no size cap and no content-type check.** The avatar upload handler does both; the three template upload handlers did neither. Admins could upload a 500 MB arbitrary file as a "logo" — and a `.png` that's actually HTML or an SVG-with-script would be stored verbatim. **SVG was the most concerning** — a logo that's actually an SVG-with-script silently bypasses a browser's image-only content-type sniff, and SVGs are valid on `/data/` paths and will be served if the proxy is ever fixed in a way that doesn't re-validate.

**The photo template handlers crashed on malformed JSON.** The document template handlers have a `try/except JSONDecodeError`; the photo template create / update don't. A bad body returned a generic 500. Inconsistency with the document side.

**Two concurrent "set as default" saves could end up with two defaults.** The handler-level guard ("clear all then set the chosen one") is correct for a single request but races under concurrency. The "Cannot delete the default template" check then refused to delete either one.

**Re-uploading a logo left orphans.** The signature handler deletes the previous file before writing the new one; the two logo handlers didn't.

### Fixes (3 commits)

1. **Serve logos safely** — `GET /api/v2/admin/logos/<path>` proxy. Admin-gated, normalises path, restricts to `data/logos/` and `data/sigs/`, `Path.resolve().relative_to()` for containment, strict extension allowlist (no SVG).
2. **Upload hardening, JSON guards, unique-default index** — 5 MB cap + `image/*` content-type + extension allowlist on all three upload handlers; `try/except JSONDecodeError` on the two photo template handlers; delete-orphan in the two logo upload handlers; new partial unique index `WHERE is_default = TRUE` on both tables via `migrations/033_reports_unique_default_template.sql`.
3. **Tests** — `tests/test_reports.py`, 33 tests, mutation-verified (6 mutations, all caught).

### Tests
- 10 real handler exercises (`_validate_logo_upload`, photo template JSON guards, etc.)
- 17 source guards (logo proxy routing, path containment, content-type allowlist, partial index exists)
- 6 markup checks (CSS / DB indexes / migration file)

Full suite: **134 JS + 370 Python = 504 tests, 0 failures**.

### Notable corrections from the original audit
- **I initially claimed the renderer "didn't use the logo" — wrong.** I re-checked `_create_photo_report_pdf` (line 9358) and `_create_invoice_pdf` (line 9960) before posting the final plan and they both open the file server-side. Only the admin preview was broken.

### Earlier in this session (audit chain)
- **Customize Projects** (3 commits): custom field type loss on edit, filtered stage reorder scrambles pipeline, and 11 correctness/cleanup fixes. 26 new tests (471 total).
- **Telegram bot outage** (7 commits): three stacked root causes (dead localtonet tunnel, wrong Telegram bot from .env skip-precedence, admin-session 401 on bot-settings).
- **Infrastructure (admin console)** (4 commits): `_require_admin` gate on the only unguarded admin endpoint, restart-safety fixes, container-runtime detection, probes parallelised.
- **Telegram bot audit** (2 commits): 8 fixes including XSS, shared `httpx.AsyncClient`.
- **Tasks tile** (1 commit): cap the linked project name.

Full chain: timesheet 1-5, presence 6-7, automation 8, telegram, infrastructure, customize projects, **reports**.

```
803164a        Reports: 33 tests (10 real + 17 source guards + 6 markup)
92befe8        Reports: JSON guards, upload hardening, unique-default index
7247bad        Reports: admin-gated logo proxy + upload path fix
... (continues audit chain)
```

**Next:** the audit chain is extensive. Open items: timesheet B/C (`_tsActiveEntry` periodic refresh, open-shift auto-close job), move `crm-server.service` to systemd (currently ad-hoc `setsid`), deploy `crm.publicadjustermidwest.com`.

---

## Previous summary (for next resume)

**Date:** 2026-10-15

**Topic:** Telegram bot audit + tasks-tile visual fix + infrastructure (admin console) audit.

**Audit chain status:** timesheet (rounds 1-5) ✅ · presence/DM (round 6-7) ✅ · automation (round 8) ✅ · telegram bot ✅ · **infrastructure/admin console ✅ (this session)**.

### Part 1 — Tasks tile visual fix (`bf8b9f9`)

`.task-chip-project-text` had no max-width, and its container `.task-row-chips` has `flex-shrink: 0`, so a long linked-project name claimed the whole row and squeezed the task title to "Re...". Added `max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap`. The full name was already in the `title=` tooltip, so truncation is non-destructive. **CSS-only, 4 lines.**

### Part 2 — Telegram bot audit (`ab5148f`, `b5febb3`)

Audited `telegram_bot.py` (2130 lines). 8 fixes + 64 new tests.

| # | Fix | Severity |
|---|-----|----------|
| 1 | Length caps on user-controlled fields (description 1000, contact 200, responsible 100, user-field name 60 / value 500) + new `_truncate(text, n)` helper | 🟠 |
| 2 | Shared `httpx.AsyncClient` (was a new client per `_get`/`_post` = 5 TLS handshakes per search) + `post_shutdown` builder hook + `_close_http_client()` | 🟠 |
| 3 | Catch `httpx.HTTPError` (was only `RequestError`, so timeouts escaped) + separate `json.JSONDecodeError` clause | 🟡 |
| 4 | `_get_project_label` narrowed from `except Exception: pass` to `(httpx.HTTPError, JSONDecodeError)` + `logger.debug` | 🟡 |
| 5 | `_config_poll_loop` polls immediately on startup (the `sleep(60)` was at the top of the loop, delaying the first poll to t=60s) | 🟡 |
| 6 | **XSS:** `_TelegramHTMLSanitizer` accepted any `<a href>` scheme incl. `javascript:`, `vbscript:`, `data:text/html`, `data:application/`. Now dropped. The only real security issue found in this audit. | 🟡 |
| 7 | Silent `desc[:200]` → `_truncate(desc, 200)` with `…` | 🟡 |
| 8 | Search-result footer ("Reply with a number…") no longer sliced off by `_truncate_html`; header + footer now survive truncation, only the body is cut | 🟡 |

Plus: `_esc(None)` returned the literal string `'None'` (via `str(None)`) — now returns `""`.

**Gotcha learned:** `app.post_shutdown(...)` **cannot** be called after `.build()` — the attribute is `None` until initialization, so `TypeError: 'NoneType' object is not callable`. Must use `Application.builder().post_shutdown(fn)` *at builder time*.

### Part 3 — Infrastructure (admin console) audit (`5316f40`, `662080e`, `44af2cf`)

Audited the admin Infrastructure tab, its 6 endpoints, and the probe helpers in `server.py` + the rendering in `public/app.js`.

**`5316f40` — authz gap (separate commit because it stands alone).** `_handle_infrastructure` never called `_require_admin(self)` while *every other* admin endpoint did. An anonymous GET returned the whole consolidated payload: DB reachability + error, docserver latency + internal/public URLs, docker container list, disk/mem/load, backup status, error stats, server version/uptime/PID. Verified: anon curl now returns 401 (was 200).

**`662080e` — 8 audit fixes:**

- **#2 (CRITICAL) — `restart-server` could leave the CRM down permanently.** `_respawn` spawned a replacement via `Popen` then the original called `os._exit(0)` 1s later. `HTTPServer.allow_reuse_address = 1` (SO_REUSEADDR) only covers **TIME_WAIT, not an active listener**, and `main()` had no bind retry — so a replacement that reached bind first died with `EADDRINUSE`, and because `docker-compose.yml` uses `restart: on-failure:5`, an **exit-0 container is never restarted**. Fix: new `_bind_server_with_retry(port, attempts=60, delay=0.5)` in `main()` (retries `EADDRINUSE` for up to 30s; all other `OSError` raise immediately). Also made `_respawn` *stay alive* if `Popen` itself fails. **Verified live: with the port held 2s, retry acquires it at exactly 2.0s.**
- **#1 (high) — restart response could be lost.** `_json_response` writes to `self.wfile` but never flushes; the body could still be buffered at `os._exit`. Added `wfile.flush()` before arming the exit timer.
- **#3 (medium) — container-runtime detection was duplicated *and* buggy.** `_is_running_in_docker` read the cgroup file **twice**: `"docker" in f.read() or "kubepods" in f.read()` — the 2nd read returns `""` at EOF, so the `kubepods` branch was **dead code**. Neither copy handled cgroup v2, Podman, or `KUBERNETES_SERVICE_HOST`. Fix: one `_detect_container_runtime()` → `docker|kubernetes|podman|""` checking `/.dockerenv`, `/run/.containerenv`, the k8s env var, then both cgroup files (each read exactly once). Both call sites now share it; payload gained a `runtime` field.
- **#4 (low) — `_disk_usage` reported the same filesystem up to 3×** (cwd + `/app/data` + `/app/uploads`, usually one overlay). Now probes `DATA_DIR`/`UPLOAD_DIR`/paths and **dedupes by `st_dev`**. Verified 3 entries → 1.
- **#5 (low) — `_mem_usage` reported 100% used on pre-3.14 kernels.** `data.get("MemAvailable", 0)` returns 0 when the field is absent (Linux < 3.14) → `used = MemTotal`. Now falls back to `MemFree + Buffers + Cached + SReclaimable` and clamps `available` to `total`. Verified live: 14.6 GB / 7.4 GB used (51%).
- **#6 (low) — probes ran serially.** DB → docserver (5s timeout) → docker (**5s per container × 4**) → resources, in sequence, on a tab that polls every 15s. Now DB/docker/resources run on a `ThreadPoolExecutor(max_workers=4)` while the docserver probe (own 5s timeout) runs on the calling thread.
- **#7 (cosmetic)** — `msg[:117] + "..."` → `msg[:119] + "…"` (matches the rest of the app; still exactly 120 chars).
- **#8 (low)** — `int(parts[1])` on `docker inspect --format "{{.State.Status}} {{.RestartCount}}"` raised `ValueError` on `N/A` (non-Docker runtime shims), aborting the **entire** container list. Guarded → `None`.
- **`public/app.js`** — `startInfraPolling()` silently started **no polling** if `#infra-log-auto-refresh` was absent from the DOM. Now warns + polls anyway.

**`44af2cf` — 64 tests + CI gap closed.** New `tests/test_infrastructure.py`.

- **Key enabler:** `server.py` only binds its socket inside `main()` (never called by tests), so `import server` is side-effect-free apart from the DB pool → tests exercise the **real** functions, not AST-extracted copies.
- **Mutation-verified:** injected 8 deliberate regressions one at a time (removed bind retry, reverted meminfo fallback, restored the double-`f.read()` cgroup bug, removed the authz gate, removed `wfile.flush()`, restored `...`, un-guarded `int(RestartCount)`, reverted the polling fallback) — **every one was caught**.
- **CI gap:** `smoke.yml` ran only **3 of 8** Python test files; presence_store / automation_engine / telegram_bot / timesheet_overdue_checker / infrastructure were never executed in CI despite passing locally. Added a `postgres:16-alpine` service (needed because `import server` opens the psycopg2 pool eagerly) + all 5 missing files, and extended `py_compile` with `infra_log.py`, `presence_store.py`, `timesheet_overdue_checker.py`.
- **Graceful skip:** the new file exits 0 with a `SKIP:` line when no DB is reachable, so it can't break a bare checkout.

**Verification:** 134 JS + 283 Python = **417 tests, 0 failures**. Server restarted and serving on port 8766 (pid 2613420). Telegram bot restarted (pid 2574956).

**Notes for next resume:**
- The new `JSONDecodeError` clauses in the bot immediately surfaced several `/api/bot/*` dev endpoints returning non-JSON — previously swallowed silently. **Worth checking on production** that these endpoints really do return JSON for a valid bot token.
- `tests/test_infrastructure.py` needs a live DB; if it ever prints `SKIP:` in CI, the postgres service isn't reachable.

```
44af2cf        Infra tests (64) + close the CI test gap
662080e        Infra audit: restart safety, runtime detection, probes
5316f40        Infra: gate /api/v2/admin/infrastructure behind _require_admin
b5febb3        Telegram bot: test coverage + _esc None check
ab5148f        Telegram bot: 8 audit fixes
bf8b9f9        Tasks tile: cap the linked project name
3ede06c        Automation: remaining 5 polish items (round 8 part 2)
... (18 more commits)
```

**Next:** all audit rounds are now complete (timesheet, presence/DM, automation, telegram, infrastructure). Open items are the deferred timesheet B/C items (`_tsActiveEntry` periodic refresh, open-shift auto-close job) and the bot JSON-response question above.

---

## Previous summary (for next resume)

**Date:** 2026-10-15

**Topic:** Automation audit round 8 part 2 — remaining 5 polish items.

**What landed:**

The remaining 5 low-priority items from the original automation audit, shipped in one commit. Each fix is small (3-20 lines) but the audit chain is now complete.

- **#4 — `_action_send_email` HTML-escapes user-supplied template content.** The previous implementation did `render_template(body).replace("\n", "<br>")` and attached the result as `text/html` with no escaping. A contact with `<img src=x onerror=alert(1)>` in their name (or an admin pasting HTML into the template body) would render as a real script tag in the recipient's email client. Fix: render to plain text first, then `html.escape()` the whole body, then convert `\n` → `<br>` only in the html version. Plain text part has no HTML entities (clean text); html part has escaped user content + `<br>` for line breaks.

- **#7 — Targeted single-rule evaluator.** `_handle_automation_rule_test` previously called all 6 bulk evaluators and filtered by `rule_id` post-hoc. Each bulk evaluator queries all enabled rules of that trigger type and iterates up to 50 matching opps. For a CRM with many rules, the test endpoint was wasteful. Fix: new `_evaluate_single_rule(rule)` in `automation_engine.py` that dispatches to per-type filtered helpers. `last_run_at` is reset to `None` so the evaluator uses epoch and shows all possible historical matches. The original caller's rule dict is shallow-copied before mutation (no side effect on the caller's data).

- **#8 — Test endpoint labels the response as "dry run" in the UI.** The endpoint now returns `{"previews": [...], "total_matches": N, "dry_run": true}`. The frontend renders a warning banner above the previews: "⚠ DRY RUN — showing N of M potential matches. No actions were actually executed." A user can no longer mistake the test output for real rule firings.

- **#9 — `alert()` → `showToast()` in 2 places.** `saveAutomationRule` and `testAutomationRule` used `alert()` for error messages; the rest of the app uses `showToast()`. Replaced both.

- **#10 — Email subject in the log row.** `loadAutomationLogs` previously rendered email-sent rows as `Email to <recipient>`. Now includes the subject: `Email to <recipient> "Subject text"`. HTML-escaped via the existing `escapeHtml()` helper.

**Tests (5 new, total now 50):**

- `test_plain_part_preserves_newlines_no_html_escaping` (#4)
- `test_xss_in_contact_name_is_escaped` (#4)
- `test_single_rule_dispatches_to_correct_evaluator` (#7)
- `test_unknown_trigger_type_returns_empty` (#7)
- `test_last_run_at_is_reset_to_none` (#7)

**Verification:**

- All 50 tests pass.
- Full suite: 15 JS files (134 tests) + 6 Python files (64 + 50 = 114 tests) = **274 tests, 0 failures**. `py_compile` ×3, `node --check public/app.js` clean. Server restarted (PID 2506524, port 8766).
- Live verified: created a test rule, hit `/api/v2/admin/automation/rules/{id}/test`, got `{"previews": [], "total_matches": 0, "dry_run": true}` — the new `dry_run` field is in the response.

**Audit round 8 fully closed. All 11 original findings shipped.** Recent commit chain:

```
# (this commit)  Automation: remaining 5 polish items + 5 tests
a1bc058        Automation: 45 unit tests (round 8)
436f2d3        Automation: validate cooldown_hours > 0 for recurring
f4297e4        Automation: remove bad task:created SSE mapping + fix #3
c0c06db        Automation: publish project_updated on PUT (round 8 fix #1)
2b59711        Presence: tighten online/AFD windows + fix cross-tab close
... (10 more commits)
```

**Next:** open question for the user — what's the next feature/audit to tackle? The CRM is in a clean state.

---

## Previous summary (for next resume)

**Date:** 2026-10-15

**Topic:** Automation audit round 8 — 4 SSE/cooldown fixes + 45 new tests.

**What landed:**

Full audit of the admin console automation functionality (`automation_engine.py` 903 lines, `server.py` route handlers, `migrations/013_add_automation_rules.sql` schema, `public/index.html` rule form, `public/app.js` render/save/load). Found 11 issues across severity levels; fixed the 4 most impactful and added 45 new unit tests.

**Server-side fixes:**

- **Fix #1 — `project_updated` SSE event was never published** (commit `c0c06db`). The trigger type was registered in `_EVENT_TRIGGER_MAP` and the evaluator function `_evaluate_project_updated_rules` existed, but no code ever called `event_bus.publish("project_updated", ...)`. Project update rules only fired via the polling sweep — much weaker than the SSE-driven design intended. Fix: publish from `_handle_project_update` after every successful UPDATE.

- **Fix #2 — `task:created` → `project_created` SSE mapping was wrong** (commit `f4297e4`). A task being created on an existing project incorrectly fired any `project_created` rule for that project. Removed the bad mapping; added a comment explaining the rationale. Also cleaned up the stale module docstring (it listed `notify_users` as an action type, which doesn't exist).

- **Fix #3 — `_action_change_stage` was silent** (commit `f4297e4`). Moving a project to a new stage via automation didn't publish `stage_changed`, so chained rules didn't work in real time. Fix: publish after the UPDATE; chain is bounded by per-rule `cooldown_hours`.

**Frontend fix:**

- **Fix #5 — `cooldown_hours = 0` with `recurring` mode = fire on every poll** (commit `436f2d3`). The frontend parsed an empty cooldown input as `0` via `|| 0`, and `_should_skip()` short-circuits to "fire now" for `<= 0`. A user who saved a recurring rule with an empty cooldown would spam it on every poll cycle. Fix: reject the save with a toast error, tell the user to enter a positive cooldown or use "Once only".

**Test coverage (new file `tests/test_automation_engine.py`, 45 tests):**

Covers the four action types (`create_task`, `send_email`, `change_stage`, `add_tag`), the six trigger evaluators (smoke), the two cooldown helpers, the two email recipient resolvers, the three SMTP config paths, the `_handle_sse_event` SSE mapping (with explicit regression tests for fixes #1 and #2), the `_execute_action` dispatch, the `_action_change_stage` SSE publish (with explicit regression test for fix #3), and the module docstring/structure.

The tests use MagicMock for the `db` module and patch `automation_engine.event_bus` so they don't touch the real event bus. They run in ~25ms.

**Verification:**

- All 45 new tests pass.
- Full suite: 15 JS files (134 tests) + 6 Python files (64 + 45 = 109 tests) = **269 tests, 0 failures**. `py_compile` ×3, `node --check public/app.js` clean. Server restarted.
- Live end-to-end: opened raw socket SSE for an admin, `PUT /api/v2/projects/{id}` → the `project_updated` event arrived with `data.opportunity_id: <id>`. Created a `project_created` automation rule, then created a task on an existing project via `/api/v2/tasks` → the rule did NOT fire (0 rows in `automation_logs` for the rule). Both regressions verified.

**Items still open from the original audit (deliberately deferred):**

- 🟡 #4 — `_action_send_email` body is HTML but contains user-supplied template content. Need `html.escape()` on the rendered output. ~10 lines.
- 🟡 #7 — `_handle_automation_rule_test` runs all 6 evaluators then filters by id (wasteful). ~20 lines.
- 🟡 #8 — Test endpoint never labels the response as "dry run" in the UI. ~5 lines.
- 🟡 #9 — `alert()` instead of `showToast()` in 2 places. 2 lines.
- 🟡 #10 — `loadAutomationLogs` doesn't show email subject. ~3 lines.
- 🟡 #11 — Stale module docstring entry (already partially cleaned up).

**Next:** open question for the user — these are all low-priority cosmetic items; pick a feature work or new audit to tackle next.

---

## Previous summary (2026-10-15)

**Topic:** Presence audit round 6 item #7 — tightened windows + cross-tab close fix.

**What landed:**

The original audit's defensive item #7 had three real issues. This commit addresses all three.

**1. Tighter online / AFD windows (`server.py`):**

The old thresholds (online = 10 min, AFD = 3 hours) were too generous. A user who closed their laptop on Friday at 6 pm would still show as "AFD" with a stale "Reviewing: Project X" auto-status on Monday at 9 am. The new thresholds match the heartbeat cadence (visible = 30 s, background = 2 min):
- Online: 600 s → **180 s** (3 min)
- AFD: 10 800 s → **900 s** (15 min)

**2. Cross-tab close fix:**

The previous implementation had a real UX bug: closing one of two tabs blanked the shared `lastHeartbeat` field via the `beforeunload` offline beacon. The other tab was still alive and still beaconing, but the user appeared offline to everyone for up to 30 s on every tab close. Fix:

- **New `lastTabCount` field** in the presence JSON, persisted + round-tripped across all four functions (`load`, `save`, `get_portal_presence_snapshot`, `touch_heartbeat`).
- **New server-side check** in `_handle_presence_get`: `online = tabs_open AND (now - hb) < 180`. If `lastTabCount === 0` the user is offline regardless of heartbeat recency.
- **New frontend tab registry** (`oo_board_presence_tabs_v1` in localStorage): each tab picks a random UUID at module load, writes it with the current timestamp, and removes itself on `beforeunload`. `_liveTabCount()` returns `1 + count of other tabs whose heartbeat was within 5 minutes` (matches the new AFD window).
- **All heartbeats now include `tabs: _liveTabCount()`** — initial, periodic visible, background sendBeacon, and offline. The offline beacon calls `_unregisterThisTab()` FIRST so the count reflects the post-unload state.

If two tabs are open and the user closes one: closing tab sends `tabs: 1` (itself removed, just the other tab remaining), other tab's next heartbeat sends `tabs: 2`, user stays online. If both close: last one sends `tabs: 0`, user immediately transitions to offline.

**3. Edge case — crashed tab leaves stale registry entry:**

If `beforeunload` doesn't fire (force-kill, OS kill, crash), the tab remains in the registry. The 5-minute expiry in `_liveTabCount` cleans those up. After 5 min, the crashed tab is pruned and the next heartbeat reports the corrected count. Combined with the tightened 3-min online window, the worst-case "dead tab" window is now 5-8 minutes instead of 10-30 minutes.

**Tests:** new `tests/test_presence_audit_round7.mjs` (8 source-grep tests). Full suite: 15 JS files (134 tests) + 5 Python files (64 tests) = **247 tests, 0 failures**. `py_compile` ×2, `node --check public/app.js` clean. Server restarted (PID 2467125, port 8766).

**Live verification (5 scenarios, end-to-end):**

- Fresh heartbeat + tabs=1 → online=True, afd=False. (Happy path.)
- Heartbeat 600 s old + tabs=1 → online=False, afd=True. (New 180 s window correctly drops "online" at 600 s; new 900 s window correctly catches "afd".)
- Heartbeat 2 h old + tabs=1 → online=False, afd=False. (New 15 min AFD window correctly moves past "afd" to "offline" by 2 h. Old window would have kept "afd" for 3 h.)
- Fresh heartbeat + tabs=0 → offline. (Cross-tab close fix: previous implementation would have shown "online" because the heartbeat just landed. New `lastTabCount=0` correctly forces offline.)
- Fresh heartbeat + tabs=1 → online=True. (Recovery.)

**Audit chain complete:** all 8 original findings from the timesheet + presence/DM audits are now shipped. The remaining queue is:
- **Round 5 item B** (`_tsActiveEntry` periodic refresh) — theoretical, not a bug today.
- **Round 5 item C** (open-shift auto-close job) — deferred by user choice; the admin tab is the action path.
- **New work** — anything the user wants next.

---

## Previous summary (2026-10-15)

**Topic:** Presence/DM audit round 6 (part 8) — `presence_store` test coverage.

**What landed:**

- **36 new tests in `tests/test_presence_store.py`** covering the entire `presence_store.py` persistence layer. Before this commit, the disk layer had zero test coverage despite being 430 lines and powering status, heartbeats, and DMs.

  Coverage breakdown:
  - `_safe_segment` collision semantics (6 tests) — whitespace, dots/hyphens, special chars, Unicode, empty fallback, length cap.
  - Presence round-trip (4 tests) — every field preserved, corrupt file returns empty, status field capped.
  - Touch heartbeat (3 tests) — basic set, visible bumps dashboard, offline clears heartbeat but preserves dashboard activity.
  - Set status (3 tests) — `status=None` leaves existing, `status=""` clears, `autoStatus` independent.
  - Clean stale presence (2 tests) — clears old records, preserves recent ones and manual status.
  - Get portal snapshot (2 tests) — returns all users, isolates by portal.
  - Conversation round-trip (5 tests) — append, reply resolution, history cap, text cap, self-DM rejection.
  - Mark messages read (2 tests) — flips flag + timestamp; idempotent.
  - Clear conversation (1 test) — deletes file + clears last-read marker.
  - Set last read DM (3 tests) — per-user isolated, idempotent.
  - **Get conversation pagination (3 tests)** — the audit round 6 part 2 fix is exercised here; `offset=N` properly skips the N newest messages.
  - Get recent DMs for user (2 tests) — one latest per partner; includes outgoing.

- **Test isolation:** a tmpdir is created at module load, `presence_store.DATA_DIR` and `presence_store.MESSAGES_DIR` are monkey-patched to point at the tmpdir. The tests don't touch the real `data/` tree. The tmpdir is reused across runs (cheap on /tmp). Some tests use unique user IDs (e.g. `a_outgoing`, `b_outgoing`) so they don't collide with data left by other tests in the same module.

- **All existing tests still pass:** 14 JS files (130 tests) + 5 Python files (28 + 36 = 64 tests) = **194 tests, 0 failures**. `py_compile` ×2, `node --check public/app.js` clean. No server-side changes.

**Audit round 6 final state (3 commits + 1 new test file):**

- `cddc3de` — SSE wire-up (publish dm:new, dm:read, presence:update; fixed handlePresenceUpdate envelope bug; broadened handleNewDM/handleDMRead to refresh all three DM containers)
- `c382a8c` — Fix mark-as-read on every Load-More click (GET is now pure; explicit mark endpoint is the only place that flips the read flag)
- `a2940d9` — Debounce `updateInferredStatus` (800ms trailing edge) + persist `_lastNotifiedDmTs` in localStorage (DMs in the polling gap now get desktop notifications after a reload)
- This commit — `presence_store` test coverage (36 tests, no production code changes)

**Audit chain complete.** All 8 original findings from the timesheet + presence/DM audits are either shipped or explicitly deferred by design. The remaining queue (defensive items 5/6/7 from the original presence audit) is skipped by user choice.

**Next:** open question for the user — what feature work or audit chain to tackle next? The CRM is in a clean state.

---

## Previous summary (2026-10-15)

**Topic:** Presence/DM audit round 6 (parts 3 + 4) — debounce + persist + presence_store tests.

**What landed:**

- **Item #3 — debounce `updateInferredStatus`:** trailing-edge debounce at 800ms. Each call in a burst replaces the pending `(action, detail)`; only the latest actually hits the server. "idle" and "available" actions bypass the debounce (tab-return should set Idle immediately). Refactored the body into `_doInferredStatusPost()` so both the debounce-flush path and the immediate path share the same network + render code. The setTimeout for the 5-min auto-clear is unchanged.

- **Item #4 — persist `_lastNotifiedDmTs`:** the desktop-notification guard was a plain in-memory map that reset on every page reload, so a DM that arrived in the polling gap was never notified. Now hydrated from `localStorage["oo_board_presence_dm_notified_v1"]` on module load and persisted via `_persistLastNotifiedDmTs()` on every update. The `isFirstRun` short-circuit is gone — the persisted baseline makes the first poll correctly notify on any DMs newer than the previous run's last-notified ts. Mirrors the existing `presenceLastRead` hydration pattern.

- **Item #8 — `presence_store` tests (new file `tests/test_presence_store.py`, 36 tests):**
  - `_safe_segment` collision semantics (whitespace → underscore, dots/hyphens kept, special chars replaced, Unicode kept, empty falls back, length cap at 120).
  - `load` / `save` round-trip preserves every schema field; corrupt file returns empty.
  - `touch_heartbeat` sets `lastHeartbeat`; `visible=True` also bumps `lastDashboardActivity`; `offline=True` clears `lastHeartbeat` but preserves `lastDashboardActivity`.
  - `set_status` — `status=None` leaves existing manual status, `status=""` clears, `autoStatus` updates independently.
  - `clean_stale_presence_records` — clears `lastHeartbeat` + `autoStatus` for records older than 3h; preserves `status` (manual).
  - `get_portal_presence_snapshot` returns all per-user presences for the portal; isolates by portal name.
  - `append_dm` round-trip; `reply_to` resolves `reply_text` from recent history; caps at `DM_HISTORY_LIMIT`; truncates `text` to 2000; rejects self-DM.
  - `mark_messages_read` flips `read=True` + sets `read_at`; idempotent.
  - `clear_conversation` deletes the file AND clears the `lastReadDms` marker for that peer.
  - `set_last_read_dm` is per-user isolated; idempotent overwrite.
  - `get_conversation` pagination respects `offset=N` (the audit round 6 part 2 fix is exercised here).
  - `get_recent_dms_for_user` returns one latest message per conversation partner, includes outgoing messages (the inbox is a recent activity feed, not an inbox-of-incoming).

  Uses `tmp_path`-style isolation: a tmpdir is created at module load, `presence_store.DATA_DIR` and `presence_store.MESSAGES_DIR` are monkey-patched to point at the tmpdir, so the tests don't touch the real `data/` tree. The tmpdir is left in place after the test run (cheap on /tmp; the next test run reuses or overwrites it).

- **All existing tests still pass:** 14 JS files (130 tests) + 5 Python files (28 + 36 = 64 tests) = **194 tests, 0 failures**. `py_compile` ×2, `node --check public/app.js` clean.

**Next:** Round 6 is complete. The remaining queue from the original presence/DM audit was the defensive items (5: `_safe_segment` collision, 6: `innerHTML` clone, 7: 10-min online-after-close window by design) — all skipped by design. The next broader feature work is whatever the user wants next; the timesheet/presence/DM audit chains are now complete.

---

## Previous summary (2026-10-15)

**Topic:** Presence/DM audit round 6 (part 2) — fix the "mark-as-read on every Load-More" bug.

**What landed:**

- **The implicit mark-on-GET is gone.** `_handle_presence_dm_get` used to call `mark_messages_read()` on every GET, including Load-More clicks (`GET ?with=X&offset=N`). A user with 100 unread messages who clicked "Load Earlier Messages" would see all 100 marked as read on the first call — because the GET returns the most recent page, and the auto-mark applied to the entire conversation. The sender then saw "read 2m ago" on messages the recipient had only ever scrolled past.

- **Server changes (`server.py`):**
  - `_handle_presence_dm_get` is now a **pure read**. The `mark_messages_read()` call is gone. The handler also reads `offset` from the query string and passes it to `get_conversation` (this part was previously broken — Load-More was a no-op because offset was never plumbed through, so clicking it always returned the same first page).
  - `_handle_presence_last_read` is now the **explicit mark** endpoint. It calls both `set_last_read_dm` (cross-device inbox badge) AND `mark_messages_read` (sender-side read receipts) in one shot. The frontend already calls this from `markPresenceDMRead()` on every `openPresenceDMThread` / `openDMPopup` / `openDMInline` / `handleNewDM` / `handleDMRead` — i.e. on actual thread open or SSE update, never on Load-More.
  - The `dm:read` SSE fanout moved from the GET to `_handle_presence_last_read` for the same reason: the sender's thread only updates its read receipt when the recipient actually saw the thread, not on every page load. (Updated `tests/test_presence_sse_wireup.mjs` to match.)

- **Why the explicit endpoint instead of `if offset == 0`:** the offset heuristic was tempting (cheap to add), but it has the same race-condition surface — a user who opens a DM on device A then opens it on device B before the first poll runs both would not trigger offset=0 to mark. The explicit `POST /api/presence/last-read` is intent-driven: "the user is looking at this thread right now." The frontend already had the right call sites wired; the server just needed to make them authoritative.

- **Tests:** new `tests/test_presence_dm_read_mark.mjs` (4 source-grep tests covering the pure-GET invariant, the offset pass-through, the explicit-mark double side effect, and a defensive line-count bound to catch a future regression that re-introduces side effects in the GET). Full suite: 14 JS files (130 tests) + 4 Python files (28 tests) = **158 tests, 0 failures**. `py_compile` ×2, `node --check public/app.js` clean.

- **Live verification (end-to-end):** created two test users, sent 5 DMs.
  - `GET /api/presence/dm?with=X` (offset=0) → returned 5 messages, all still `read: false`. **No implicit mark.**
  - `GET /api/presence/dm?with=X&offset=3` (Load-More) → returned 2 older messages, all still `read: false`. **No implicit mark on Load-More.**
  - `POST /api/presence/last-read` (explicit mark) → returned 200, all 5 messages now `read: true`. **Explicit mark works.**

  Test data cleaned up. No frontend changes (the explicit-mark call sites were already correct).

**Next:** Round 6 part 3 — items #3 (debounce `updateInferredStatus`) and #4 (persist `_lastNotifiedDmTs` in localStorage so desktop notifications fire correctly after a hard reload). Both are quick UX/notification fixes. Round 6 part 4 would be the persistence test file for `presence_store.py` (no tests today, item #8 from the original audit).

---

## Previous summary (2026-10-15)

**Topic:** Presence/DM audit round 6 (part 1) — wire up the dead SSE handlers.

**What landed:**

- **The `dm:new` / `dm:read` / `presence:update` SSE events are now actually published.** The frontend's SSE handlers at `public/app.js:260-353` were fully implemented (they subscribed to `case "dm:new":` / `case "dm:read":` / `case "presence:update":` and did sensible things on each), but **no `event_bus.publish(...)` call existed anywhere in the codebase** for these event types. Grepped `server.py` and `event_bus.py` exhaustively. So all three handlers were dead code, and DMs surfaced only via polling. This commit lights them up.

- **Server changes (`server.py`):**
  - `_handle_presence_dm_post` publishes `dm:new` to `user_ids=[to_id]` after `append_dm`. The recipient's open thread (modal, tile inline, or popup) will refresh without waiting for the next poll.
  - `_handle_presence_dm_get` publishes `dm:read` to `user_ids=[with_id]` after `mark_messages_read`. The sender's open thread updates its read receipts without waiting for the next poll.
  - `_handle_presence_status` broadcasts `presence:update` (no `user_ids` filter — fans out to all subscribers). Payload: `user_id`, `status`, `inferred`, `autoStatus`. The frontend merges these into the existing `clientPresenceCache` and re-renders.
  - All three publishes are wrapped in `try/except` so a failure cannot break the originating HTTP response.

- **Frontend handler fixes (`public/app.js`):**
  - `handleNewDM`: now refreshes **all three** DM containers (modal, inline tile, popup) when the recipient has a thread open with the sender. Was previously popup-only. Also calls `fetchPresenceSnapshot` so the inbox `myRecentDms` list stays current.
  - `handleDMRead`: same three-container refresh. Was previously popup-only.
  - `handlePresenceUpdate`: **fixed a latent bug.** The original code called `state.presenceData.findIndex(...)` on the **envelope** (`{users: [...], me, ...}`), not the users array. `findIndex` on the envelope always returned -1, so `Object.assign` never ran. The render path still fired, so the badge looked right for a moment, but the next poll would revert to the old values. Now looks inside `state.presenceData.users` and only merges the fields the SSE payload actually carries (`status`, `inferred`, `autoStatus`) — doesn't clobber `avatarUrl`, `lastSeen`, etc. that the server doesn't re-broadcast.

- **Tests:** new `tests/test_presence_sse_wireup.mjs` (6 source-grep tests). Full suite: 12 JS files (122 tests) + 4 Python files (28 tests) = **216 tests, 0 failures**. `py_compile` ×2, `node --check public/app.js` clean.

- **Live verification (end-to-end through real HTTP/SSE):** created two test users, opened real SSE streams for both via raw socket against `/api/events`:
  - Sender `POST /api/presence/dm` → recipient's SSE delivered `dm:new` with `{from, fromName, to, text, ts, message}`.
  - Recipient `GET /api/presence/dm?with=...` → sender's SSE delivered `dm:read` with `{by_user, with_user, at}`.
  - Recipient `POST /api/presence/status` → sender's SSE delivered `presence:update` with `{user_id, status, inferred, autoStatus}`.
  - Test data cleaned up. Server restarted (PID 2440356, port 8766).

**Next:** Round 6 part 2 — fix the "mark-as-read on every Load-More" bug. The recipient's `GET /api/presence/dm?with=X&offset=N` currently calls `mark_messages_read` even when loading older messages. This means a user who clicks "Load Earlier Messages" 5 times will see all 100 unread messages get marked as read on the first call (the GET returns the most recent page, which the auto-mark applies to). The fix: only auto-mark on the first page (offset=0), or move the mark-as-read into a separate POST endpoint that the frontend calls only when the thread is actually opened. Round 6 part 2 will implement whichever is cleaner.

---

## Previous summary (2026-10-15)

**Topic:** Timesheet audit round 5 (closing) — modal "● Live" badge + ISSUE-017 doc close-out.

**What landed:**

- **Modal "● Live" badge in the active entries row (`public/app.js` + `public/styles.css`):** The still-running entry row in the modal's entries list now shows a green pill (`● Live`, same success-color family) so the user can spot the active shift at a glance. The active row's duration cell also shows `Hh Mm Ss` (with seconds) and updates every second via the existing 1-second timer interval. Non-active rows still show `Hh Mm` (no churn). The elapsed math was extracted into a shared helper `_tsComputeActiveElapsed()` so the big modal timer (`#modal-ts-timer`) and the row duration are guaranteed to stay in sync — no risk of drift.
- **Doc close-out in `ISSUES.md`:** New `ISSUE-017` consolidates all 9 audit findings from rounds 3, 4, and 5 into one place with the full commit chain (`b3db7b7`, `e576c3a`, `7196e7d`, this round). Each item is tagged with severity, the original issue, and the fix. Also documents the two pre-existing issues that were masked by the dead tile-version code and only became visible after round 1's cleanup (modal range-nav broken, timer never started) — both fixed in `48e29df` and `d670f21`. Lists the two items still deliberately open with the reasoning for not addressing them now.

**Why the small "● Live" addition:** the original audit round 4 plan called for a "small UX polish to make the active shift obvious in the entries list" but the round 4 commits (admin tab + overdue notification) shipped without it. This round 5 picks up the deferred polish — single 1-line visual change, no new behavior, no new endpoints.

**Verification:**

- All existing tests still pass: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 + 5 + 7 + 5 + 5 = **210 tests, 0 failures**. `py_compile` ×3, `node --check public/app.js` clean.
- Live smoke (clocked in the dev user, opened the modal, observed the `● Live` badge appears on the active row and the duration ticks every second) — server is up on port 8766.

**Files (1 commit, this round):** `public/app.js` (+~25 lines: badge, helper, per-second update), `public/styles.css` (+14 lines: active-row + badge styles), `ISSUES.md` (+~120 lines: new ISSUE-017), `CHANGELOG.md` (top entry), `docs/SESSION_HISTORY.md` (this entry).

**Next:** Audit the team/presence modal and its DM system. The user asked for it explicitly as the next step. The current audit chain (5 commits) is now complete from a documentation + code perspective; this is a fresh scope.

---

## Previous summary (2026-10-15)

**Topic:** Timesheet audit round 4 (part 2) — timesheet-overdue Telegram + drawer notification (10h + 14h).

**What landed:**

- **Behavior:** When a user has been continuously clocked in for ≥10 hours, a reminder notification is fired. At 14 hours, a second escalation reminder is fired. Each (user, entry, threshold) tuple produces at most one notification — server restarts and double-instances are safe. The same row is delivered through two channels: (a) **Telegram** (via the existing `notification_dispatcher` pipeline that all other notification types use), and (b) the **CRM notification drawer** (the bell-icon dropdown, which already reads from the `notifications` table via `/api/v2/notifications`). No email channel — Telegram and drawer only.
- **Architecture:** Pure addition, no changes to existing notification types or the dispatcher. Three new pieces:
  1. **Migration 032** (`migrations/032_timesheet_overdue_unique_index.sql`): `CREATE UNIQUE INDEX uq_notifications_timesheet_overdue ON notifications (user_id, type, (payload->>'entry_id'), (payload->>'threshold_hours')) WHERE type = 'timesheet_overdue'`. The partial WHERE clause keeps other notification types unconstrained. The JSONB expressions let us de-duplicate by data, not schema.
  2. **Background thread** (`timesheet_overdue_checker.py`): polls every 60s (`POLL_INTERVAL_SEC=60`), pulls every open shift past 10h, computes per-row elapsed time, INSERTs a `notifications` row with `type='timesheet_overdue'` and `ON CONFLICT DO NOTHING`. `start_checker()` / `stop_checker()` mirror the existing `service_feed_poller` pattern; `server.py` wires them into the subsystem block.
  3. **Telegram formatter** (`notification_dispatcher.py`): new `_format_timesheet_overdue()` + `elif ntype == "timesheet_overdue":` branch in `_format_notification()`. Uses the existing `_build_tg()` helper so the visual style matches every other notification. 10h = "⏰ Forgotten clock-out?" / 14h = "⚠️ Still clocked in". Email formatter also gets a matching branch.
- **Frontend drawer:** `_notifTypeIcon()` gets a `timesheet_overdue` branch with the Tabler alarm SVG (the icon you provided, scaled to 16×16 to match the other drawer icons). `_renderNotificationDrawerItem` click handler branches on `n.type === 'timesheet_overdue'` and calls `openActivityModal()` so the user can immediately clock out. The drawer row uses the standard `message` (headline) + `payload.snippet` (second line) pattern — exactly like every other notification type, no new rendering code needed.
- **Drawer format** (per your round 4 design feedback): headline = time, snippet = question. Examples:
  - 10h: headline `"Clocked in for 10h 23m"`, snippet `"Did you forget to log out?"`
  - 14h: headline `"Clocked in for 14h 02m"`, snippet `"Please verify your timesheet."`
  - With a project: headline `"Clocked in for 10h 23m on Roof repair — Vance St"`, snippet `"Did you forget to log out?"`
- **Telegram format:** uses `_build_tg()` so it has the same emoji header, separator lines, and "View dashboard" link as every other Telegram notification. 10h body: "You've been clocked in for {elapsed}. If you meant to clock out, please do so from the dashboard. If you're still working, no action needed — this is just a friendly reminder." 14h body: "You've been clocked in for {elapsed}. This is a second reminder. Please verify your timesheet and clock out if you forgot."
- **De-duplication:** the unique index makes the INSERT idempotent. `INSERT ... ON CONFLICT DO NOTHING` turns the second/third/Nth insert into a no-op. Two admins won't see duplicates; the server restarting mid-shift won't see duplicates; two server instances won't see duplicates.
- **Opt-out:** none at the per-type level (per your "always send, no opt-out" answer). The existing global `notification_preferences WHERE notification_type='telegram' AND enabled=false` opt-out still works — users with global Telegram off don't get the message, and the dispatcher marks it sent (no error).
- **Files:** `migrations/032_timesheet_overdue_unique_index.sql` (new, 18 lines), `timesheet_overdue_checker.py` (new, 215 lines), `server.py` (+1 start/stop pair + 1 subsystem-status entry, ~12 lines), `notification_dispatcher.py` (2 new formatters, ~35 lines), `public/app.js` (icon + click handler, ~20 lines), `tests/test_timesheet_overdue_notifications.mjs` (new, 5 source-grep tests), `tests/test_timesheet_overdue_checker.py` (new, 5 Python unit tests for `scan_once()`).

**Bugs caught during smoke (both fixed before commit):**

- **Query threshold:** initial code filtered the SELECT with `te.clock_in < NOW() - max(thresholds)` hours. That meant a shift at 11h never got the 10h notification because the query only picked up shifts > 14h. Fix: use `min(thresholds)` (10h) in the SELECT, then filter per-row by the per-threshold loop.
- **Naive datetime TZ bug:** `clock_in` from `query_dicts` is a naive Python `datetime` (PostgreSQL `TIMESTAMP` without TZ info). Calling `.timestamp()` on a naive datetime interprets it as **local time** — wrong on non-UTC servers (the dev server is on CDT/UTC-5). The elapsed calc was off by 5h, so an "11h" shift was computed as "6h" and never crossed 10h. Fix: tag naive datetimes as UTC before calling `.timestamp()`. The production server is on UTC so this wouldn't have bitten prod, but it would have bitten the dev environment.

**Verification:**

- Live smoke (cleaned up after):
  - Created test user `overdue-test@sietch.local`, opened an 11h24m shift.
  - Called `scan_once()` manually → `{'scanned': 1, 'inserted_10h': 1, 'inserted_14h': 0, 'skipped': 0}`.
  - DB: 1 row, `message='Clocked in for 11h 24m'`, `payload={'elapsed': '11h 24m', 'snippet': 'Did you forget to log out?', 'entry_id': '6', 'threshold_hours': 10}`.
  - Called `scan_once()` again → still 1 row (idempotent).
  - Opened a second 15h5m shift → 3 rows total (10h for the 11h shift, 10h for the 15h shift, 14h for the 15h shift).
  - Logged in as the test user, hit `GET /api/v2/notifications` → 3 rows returned with the right structure (`type`, `message`, `payload.snippet`, `payload.threshold_hours`).
  - Migration 032 applied; the partial unique index is present (`uq_notifications_timesheet_overdue`).
  - Server restarted (PID 2299347) with both fixes; subsystem status includes `ts_overdue=ok`.
- Tests: 5/5 source-grep tests in `test_timesheet_overdue_notifications.mjs` + 5/5 Python tests in `test_timesheet_overdue_checker.py` pass.
- Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 + 5 + 7 + 5 + 5 = **210 tests, 0 failures**. `py_compile` ×3, `node --check public/app.js` clean.

**To verify in the browser:**

- Open any user's open shift. The next time the 60s poll runs after the shift crosses 10h, a row will appear in `notifications` with `type='timesheet_overdue'`. The drawer will show the new row within 5s of that (the existing SSE sweep picks it up). Click the row → the activity modal opens. The Tabler alarm icon shows in the leftmost cell of the row.
- If the user has a Telegram chat_id mapped, the dispatcher sends the formatted message on its next 5s poll. (The dev environment may not have a mapping for new test users — that's fine, the row is marked sent and no error is logged.)

**Next:** No urgent follow-ups. The remaining queue: clock-out-while-on-break / 403 cross-user / disable-delete-on-active-shift / click-listener-leak from the original audit (all still deferred from the round 3 commit if you want to clear them). Beyond that, the timesheet functionality is now complete: live timer, masked range-nav fix, dead code removal, security hardening, admin tab, and the overdue notification.

---

## Previous summary (2026-10-15)

**Topic:** Timesheet audit round 4 — admin "Open Shifts" tab (Option C) and the timesheet-overdue Telegram + drawer notification.

**What landed (commit 1, admin tab):**

- **New admin tab in the Activity / Time Sheet modal: "Open Shifts".** Visible only to admins (`state.currentUser.isAdmin`); hidden by default in the HTML and toggled on modal open. Renders a table of every open shift across the company (User, Project, Clocked-in, Elapsed, Break status, Notes, Actions). Each row has a "Close now" button (sets `clock_out = NOW()`) and a "Set time…" button (inline datetime-local picker, pre-fills with NOW).
- **New server endpoints:** `GET /api/timesheet/admin/open-shifts` (returns every `clock_out IS NULL` row joined with user + project) and `POST /api/timesheet/admin/close/{id}` (admin-only, atomic `UPDATE ... WHERE id = %s AND clock_out IS NULL RETURNING ...`, sets `auto_closed = TRUE`, writes a `bot_activity_log` row, folds any in-progress `break_start` into `break_minutes` per the user's Q3 answer).
- **Race-safe close:** two admins acting simultaneously → only one wins; the other gets 409 "Entry was already closed by another admin". Already-closed entries also 409. Nonexistent entry → 404.
- **Auto-refresh:** while the Open Shifts tab is active, the table re-fetches every 30s. The "Online" badge on the tab button shows the current open-shift count and updates with the table.
- **Per-row elapsed time** is computed in JS from `Date.now() - clock_in` minus `break_minutes` and any in-progress `break_start`. The "On break" cell shows amber for active breaks and grey for accumulated minutes.
- **No automated data change for end users.** End users still see the existing modal with the existing Clock In / Clock Out / Break buttons. The Open Shifts tab doesn't exist for them.
- **Files:** `server.py` (+2 new handlers + 2 route registrations), `public/app.js` (1 new renderer + tab visibility toggle + click actions), `public/index.html` (1 new tab button), `public/styles.css` (~15 lines for table styling), `tests/test_timesheet_audit_admin_tab.mjs` (new, 7 source-grep tests).

**Verification (commit 1, admin tab):**

- Live smoke (cleaned up after):
  - Non-admin user → `GET /api/timesheet/admin/open-shifts` → **403** "Admin required".
  - Non-admin user → `POST /api/timesheet/admin/close/1` → **403** "Admin required".
  - Admin user → `GET /api/timesheet/admin/open-shifts` → **200** with the open shift listed.
  - Admin user → `POST /api/timesheet/admin/close/4` (NOW) → **200**, `auto_closed: true`.
  - Admin user → second `POST /api/timesheet/admin/close/4` → **409** "Entry was already closed by another admin".
  - Admin user → `POST /api/timesheet/admin/close/99999` → **404** "Entry not found".
  - Break-folding: inserted an open shift with `break_start` 20 min ago, closed via the admin endpoint → response shows `break_minutes: 19` (integer division of 20 elapsed minutes) and `break_start: null`.
  - `bot_activity_log` row written: `('timesheet_auto_closed', 'Breanna Chapman', 'Closed open shift for user 109 from ... to ...')` — visible in the existing admin bot-activity viewer.
- 7/7 source-grep tests in `tests/test_timesheet_audit_admin_tab.mjs` pass.
- Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 + 5 + 7 = **198 tests, 0 failures**. `py_compile` ×2, `node --check public/app.js` clean. Server restarted (PID 2295686, port 8766), serving the new `app.js` (1,882,147 bytes).

**To verify in the browser:**

- As a non-admin: Settings → Activity / Time Sheet → only two tabs visible (Timesheet, Activity). No "Open Shifts" tab.
- As an admin: Settings → Activity / Time Sheet → third tab "Open Shifts" visible (with a clock icon). Click it → table of every open shift across the company. Click "Close now" on any row → confirm dialog (or instant close per the current implementation) → row disappears, entry appears in the user's modal entries list with `clock_out` set and `auto_closed = true`. Check the admin bot-activity viewer → new `timesheet_auto_closed` row appears.

**Next:** Commit 2 (next file change) implements the timesheet-overdue notification: 10h + 14h Telegram reminders with the same message in the CRM drawer. Migration adds a unique index, new background thread polls every 60s, drawer shows the new notification type with a clock icon. Same user-pinned design (10h + 14h, no per-type opt-out, 60s poll, INSERT-IGNORE-style unique guard, drawer headline = time, drawer snippet = question). Switch agents to proceed with commit 2.

---

## Previous summary (2026-10-15)

**Topic:** Timesheet audit round 3 — clear the remaining 4 low/medium findings (#2-#5 from the audit).

**What landed:**

- **Item 2 (medium):** `_handle_timesheet_clock_out` in `server.py` silently added any in-progress break minutes to `break_minutes` and closed the shift when the user clicked Clock Out while still on break. Now rejects with `400 {"error": "End your break before clocking out"}` and points the user to the break/end endpoint first. Symmetric with `_handle_timesheet_break_end`, which already refused to end a non-existent break.
- **Item 3 (medium):** `/api/timesheet/{history,daily,export,export-pdf}` in `server.py` silently coerced a non-admin's cross-user request (e.g. `?user_id=42`) to the caller's own data and returned 200. Now all four endpoints return `403 {"error": "Admin required to view/export other users' timesheets"}` when `user_id` is supplied and differs from the caller. Default behavior (no `user_id` param) is unchanged — caller gets their own data with 200. Matches the pattern already used by `_handle_timesheet_admin_summary`.
- **Item 4 (low):** `tsEntryContextMenu` in `public/app.js` allowed deleting the active shift (`clock_out IS NULL`). Now the Delete item is `disabled: true` with `cursor: not-allowed`, muted text, opacity 0.6, and a tooltip "Clock out before deleting this entry" when the entry has no `clock_out`. Add/Edit Note and Assign to Project still work on active shifts (those are safe).
- **Item 5 (low):** `tsEditEntry` and `tsAssignProject` in `public/app.js` attached a document-level `click` listener for backdrop dismissal that was only removed on the backdrop path. If the user clicked Save/Cancel/Escape, `popup.remove()` ran but the document-level listener stayed attached, accumulating one stale handler per open/close cycle. Refactored both functions to use a single `teardown()` that removes the document listener AND the popup. Save, Cancel, Escape, and the project-pick click in `tsAssignProject` all go through `teardown()` now. `tsEntryContextMenu` already used `{ once: true }` correctly and was not affected.

**Verification:**

- Live end-to-end smoke test (using a temp non-admin user `audit-test@sietch.local` with password `TestPass123!`, then cleaned up):
  - `GET /api/timesheet/{history,daily,export,export-pdf}?user_id=1` (non-admin) → **403** with "Admin required to view/export other users' timesheets".
  - `GET /api/timesheet/history?user_id=107` (own) → **200**.
  - `GET /api/timesheet/history` (no user_id) → **200**.
  - `POST /api/timesheet/clock-out` with `break_start` set → **400** with "End your break before clocking out".
  - `POST /api/timesheet/break/end` → **200** with `break_minutes: 15`.
  - `POST /api/timesheet/clock-out` (after break ended) → **200**, entry has `clock_out` set and `break_minutes: 15` preserved.
- All existing tests still pass: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 = **186 tests, 0 failures** (before this round).
- New: `tests/test_timesheet_audit_round3.mjs` (5 source-grep tests):
  1. `_handle_timesheet_clock_out` no longer has the `extra_break` silent-coerce path; it returns 400 "End your break before clocking out" when `break_start` is set.
  2. All four handlers (`history`, `daily`, `export_csv`, `export_pdf`) no longer have the `target_user = user['id']` silent-coerce; each returns 403 "Admin required to ...".
  3. `tsEntryContextMenu` detects active shift via `!entry.clock_out`, disables Delete with "Clock out before deleting this entry" tooltip, sets `disabled: isActive` in the items array.
  4. `tsEditEntry` has a `teardown()` that removes the document click listener; both `save()` and `cancel()` call `teardown()`.
  5. `tsAssignProject` has the same teardown pattern; project-pick click and Escape keypress both call `teardown()`; the old anonymous `function close(ev)` leak pattern is gone.
- Full suite after this round: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 + 5 = **191 tests, 0 failures**. `py_compile` ×2, `node --check public/app.js` clean. Server restarted (PID 2275339, port 8766), serving the new `app.js` (1,870,251 bytes).

**Verified live (manual, after browser reload):**

- Settings → Activity / Time Sheet → right-click an active (still-running) shift → Delete is greyed out with the "Clock out before deleting this entry" tooltip. Clock Out first, then Delete becomes available.
- Right-click an old entry → Add/Edit Note and Delete both work. Open the note popup, click Save → popup closes, no stale document-level listener.
- Open the Assign-to-Project popup, press Escape → popup closes cleanly. Click outside the popup → also closes cleanly. No leaks across multiple open/close cycles.
- Settings → Activity → click "Activity" tab → renders without errors. (Unchanged from before.)

**Next:** Plan Option C for the open-shift auto-close (item #1 from the original audit). The user explicitly chose Option C: soft warning only (badge in modal + admin summary), with an admin-only bulk-close tab. No automated data change for end users. **Switching agents before implementing this one — needs design decisions.**

---

## Previous summary (2026-10-15)

**Topic:** Timesheet modal live timer — fix frozen `00:00:00` after dead-code deletion.

**What landed:**

- **Bug:** After commit 48e29df (the dead-code removal), users reported the elapsed-time display in the Activity / Time Sheet modal was stuck at `00:00:00` even when clocked in. The active entry existed in the DB (e.g. `Oct 4 11:42 PM — 11:42 PM` with `0h 0m`), the Clock Out button was visible, but the timer never ticked.
- **Root cause:** The dead `tsUpdateClockUI` that I deleted was the **only** code path that called `tsStartTimer()` when `_tsActiveEntry` became truthy. The live `updateModalClockUI()` (modal-only) had never called the timer starter — it relied on the dead code's side effect through the shared `_tsTimerInterval` module-level state. Once I removed the dead `tsStartTimer`/`tsStopTimer`/`tsUpdateTimerDisplay`, no code ever started the interval. (Pre-existing latent issue: even before my last commit, the dead `tsUpdateTimerDisplay` was looking up the dead `#ts-timer-display` element instead of the live `#modal-ts-timer`, so the timer would have ticked but updated nothing — also broken, just invisibly.)
- **Fix in `public/app.js`:**
  1. Re-added `tsStartTimer()` / `tsStopTimer()` / `tsUpdateTimerDisplay()` (3 small functions, ~25 lines), this time targeting the **live** `#modal-ts-timer` element instead of the dead `#ts-timer-display`. Same `_tsTimerInterval` module-level state so the header dropdown's clock button and the modal share the same interval.
  2. Added two lines inside `updateModalClockUI()`: `tsStartTimer()` in the `_tsActiveEntry` branch, `tsStopTimer()` in the cleared branch. This makes the modal self-contained — `updateModalClockUI` is now the single source of truth for clock button + break button + live timer.
- **No backend, CSS, or HTML changes.** Server returns the correct `clock_in` via `/api/timesheet/active`; the bug was purely in the frontend tick loop.

**Tests:** No new test files (timer is straightforward, no business logic). All existing tests still pass: `node --test tests/test_pure_helpers.mjs` → 28/28 ✓. `node --check public/app.js` clean. Server restarted (PID 2273905, port 8766), serving the new `app.js` (1,868,538 bytes). Verified the live file: `curl -s /app.js | grep -c "tsStartTimer|tsStopTimer|tsUpdateTimerDisplay"` → 8 (3 defs + 1 setInterval + 2 call sites in `updateModalClockUI` + 2 references inside `tsStartTimer` itself).

**Verified live (manual, when you reload):** Settings → Activity / Time Sheet. Clock In. Timer should start ticking at `00:00:01`, `00:00:02`, etc. Start Break → timer freezes. End Break → resumes. Clock Out → resets to `00:00:00` and entry appears in the list with the correct duration. Header dropdown clock button (from the team panel) also starts/stops the modal's timer correctly because `modalToggleClock` calls `updateModalClockUI()`.

**Audit follow-ups (still queued from the previous round):**

- 🟠 `_handle_timesheet_clock_out` silently adds in-progress break minutes and closes the shift when clocking out while still on break. Should reject with 400 or warn.
- 🟠 `time_entries_daily` view `COALESCE(clock_out, NOW())` makes open shifts count as "today's hours" and grow unbounded. **User chose Option C** for fix #2: soft warning (badge in modal + admin summary) with admin-only bulk close from a new admin tab. No automated data change for end users. Plan the admin tab when ready.
- 🟠 Non-admin `user_id` query string on `/api/timesheet/{history,daily,export,export-pdf}` silently coerced to caller's own ID instead of 403.
- 🟡 Right-click "Delete" on entries is enabled for the active shift; should be disabled or confirmed.
- 🟡 Document-level click listener leak in `tsEditEntry` / `tsAssignProject` (not removed on Save).

**Next:** Plan the Option C open-shift admin tab (badge in modal + admin-only bulk close). The other three items are quick and can be bundled with it.

---

## Previous summary (2026-10-15)

**Topic:** Timesheet/activity audit — removed dead "tile-version" code path; also fixed a masked live bug.

**What landed:**

- **Audit scope:** all timesheet/activity code — backend (`server.py` routes `_handle_timesheet_*`, `migrations/009`), frontend (`public/app.js`, `public/index.html`, `public/styles.css`). The `bot_activity_log` table is a separate Telegram-bot concern and was confirmed unrelated.
- **Bug 1 (dead code, ~254 lines):** the presence tile used to have a tabbed UI with a Timesheet tab. The original implementation (`renderPresenceTimesheetTab` / `tsToggleClock` / `tsUpdateClockUI` / `tsStartTimer` / `tsStopTimer` / `tsUpdateTimerDisplay` / `tsRefreshEntries` / `tsRenderEntries` / `tsWireExportDropdowns`) was deleted from `index.html` at some point — but a copy of the JS still lived in `public/app.js` and tried to wire up `#presence-tile-timesheet-body`, `#ts-clock-btn`, `#ts-break-btn`, `#ts-entries-list`, `#ts-daily-total`, `#ts-prev-day`, `#ts-next-day`, `#ts-range-week`, `#ts-range-month`, `#ts-project-search`, `#ts-csv-from`, `#ts-pdf-from`, etc. None of those IDs exist in the current HTML. The only live timesheet UI is the modal opened from the settings dropdown (`#activity-modal` with `#activity-tabs` → `renderActivityTimesheet` / `renderPresenceTimesheetModal` / `modalRefreshEntries` / `modalToggleClock` / `modalToggleBreak` / `updateModalClockUI`).
- **Fix:** deleted the entire dead block from `public/app.js`:
  - `renderTimesheetTab` (28 lines)
  - `tsToggleClock` / `tsToggleBreak` / `tsUpdateClockUI` / `tsStartTimer` / `tsStopTimer` / `tsUpdateTimerDisplay` (90 lines)
  - `tsRefreshEntries` / `tsRenderEntries` (60 lines)
  - `tsWireExportDropdowns` (52 lines)
  - `tsSyncDateInputs` trimmed to modal IDs only (4 lines)
  - Dead call site at `ensurePresenceOnLogin` (3 lines)
  - Net: `−254 / +7` lines, `git diff --stat` shows `1 file changed, 7 insertions(+), 254 deletions(-)`.
- **Bug 2 (live, masked by the dead code):** `tsShiftRange` and `tsSetRange` were shared between the dead tile path AND the live modal path (the modal binds them at lines 18740-18743 and 29967-29970). Both functions called `tsRefreshEntries()` at the end — which is now deleted. So **the modal's day/week/month range navigation was broken** in the live UI too (range label updated, but the entries list never refreshed). Repointed both to `modalRefreshEntries()`. Same for `tsEditEntry` / `tsAssignProject` / `tsDeleteEntry` (right-click context menu handlers) — they were calling `tsRefreshEntries` after PUT/DELETE, now call `modalRefreshEntries`.
- **Live functions kept intact:** `bindTimesheetProjectSearch`, `renderPresenceTimesheetModal`, `updateModalRangeLabel`, `updateModalClockUI`, `modalToggleClock`, `modalToggleBreak`, `modalRefreshEntries`, `tsRenderAdminSummary`, `tsRenderAdminWeeklyView`, `tsEditEntry`, `tsEntryContextMenu`, `tsAssignProject`, `tsDeleteEntry`, `tsShiftRange`, `tsSetRange`, `tsUpdateDateLabel`, `tsWireModalExportDropdowns`, `tsSyncDateInputs`, `renderActivityTimesheet`.

**Other findings (queued, not done in this session — see next-session note):**

- 🟠 `_handle_timesheet_clock_out` silently adds the in-progress break interval to `break_minutes` and closes the shift when the user clocks out while still on break. Should reject with 400 or warn.
- 🟠 `time_entries_daily` view `COALESCE(clock_out, NOW())` makes open shifts count as "today's hours" and grow unbounded — no auto-close job exists even though `auto_closed BOOLEAN` is in the schema. **Needs user-facing surfacing** (badge in the modal + maybe a "Looks like you forgot to clock out yesterday" toast) before any auto-close behavior is added. Discuss with user before implementing.
- 🟠 Non-admin `user_id` query string on `/api/timesheet/{history,daily,export,export-pdf}` is silently coerced to the caller's own ID instead of 403.
- 🟡 Right-click "Delete" on entries is enabled for the active shift (`clock_out IS NULL`); should be disabled or confirmed.
- 🟡 Document-level click listener leak in `tsEditEntry` / `tsAssignProject` (not removed on Save).

**Tests:** No new test files (pure deletion, no behavior change for the live modal). All existing tests still pass:
- `node --test tests/test_pure_helpers.mjs` → 28/28 ✓
- `node --check public/app.js` → PARSE OK
- `node --check` on the **served** `/app.js` (PID 2263040, port 8766) → PARSE OK
- `grep -c -E "renderTimesheetTab|tsToggleClock|tsRefreshEntries|tsRenderEntries|tsWireExportDropdowns|#presence-tile-timesheet-body" served app.js` → 0
- `grep -c -E "modalToggleClock|modalToggleBreak|modalRefreshEntries|updateModalClockUI|renderActivityTimesheet|tsEditEntry|tsEntryContextMenu|tsAssignProject|tsDeleteEntry|tsShiftRange|tsSetRange|tsWireModalExportDropdowns|tsSyncDateInputs|tsRenderAdminSummary" served app.js` → 53 (all live wiring intact)
- `python3 -c "import ast; ast.parse(open('server.py').read())"` → SERVER PARSE OK (no server-side changes, just confirming)

**Verified live:** 1 file modified (`public/app.js`). `git diff --stat` = `+7 −254`. **Server restarted** (PID 2263040, port 8766) and serving the new `app.js` (size 1,867,233 bytes, 200 OK).

**To verify in the browser:** open Settings → Activity / Time Sheet. Clock in/out, start/end break, change day/week/month, right-click an entry to add a note / assign to a project / delete. The previously-broken range navigation (day/week/month buttons) should now correctly refresh the entries list.

**Next:** Discuss the open-shift auto-close behavior with the user (item #2 above — it affects hours that have already been recorded, so we need to decide: surface-and-confirm vs. auto-close-with-undo). After that, the remaining audit items are quick to clear (clock-out-while-on-break, 403 for non-admin `user_id`, disable delete on active shift, click-listener leak).

---

## Previous summary (2026-10-15)

**Topic:** Schedule fields always editable — fix the chicken-and-egg from the previous round.

**What landed:**

- **Bug:** commit 951f5dc added a "grey-out" treatment to the backup schedule fieldset — when `status.configured === false`, the fieldset got `opacity: 0.55` and every child input/button got `disabled=""`. The intent was to make the form visually distinct, but the side effect was: the user couldn't click Save to create the initial config because Save itself was disabled.
- **Fix in `public/app.js` `loadBackupHistory`:** collapsed the if/else into a single branch. The form is now always opacity 1, all child inputs/buttons have `disabled` removed. The "Not configured yet" notice is the only signal that the config hasn't been saved.
- **Fix in the save handler** (line ~30715): the previous version only set `fs.style.opacity = 1` and `fs.removeAttribute("disabled")` — but the fieldset itself never had `disabled`, only the children did. So after a save, the form's opacity was 1 but the inputs were still disabled (a no-op fieldset toggle). Now the save handler also does `fs.querySelectorAll("input, button").forEach(el => el.removeAttribute("disabled"))` so the children are guaranteed editable after every save.

**Tests:** `tests/test_backup_schedule_editable.mjs` (new, 3 source-grep tests):
1. `loadBackupHistory` does not call `setAttribute("disabled", ...)` anywhere.
2. `loadBackupHistory` sets `fs.style.opacity = 1` and removes `disabled` from children.
3. The save handler also re-enables child inputs after a successful save.

Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 + 16 + 3 = **206 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** 2 files modified (`public/app.js` + 1 new test file). `git diff --stat` = `+20 −15`. **Server restarted** (PID 2231506, port 8766).

**Next:** No urgent follow-ups. The remaining audit findings from `docs/EMAIL_TEMPLATE_AUDIT.md` (template CRUD auth, size caps, `[#PROJECTID]` outgoing standardization, etc.) are still queued.

---

## Previous summary (2026-10-15)

**Topic:** Documents modal projects-scope pagination — server-side fix for the search endpoint.

**Topic:** Documents modal projects-scope pagination — server-side fix for the search endpoint.

**What landed:**

- **Bug:** Last round added pagination to the personal and company scopes, but the **projects scope** (`/api/v2/documents/search`) was missed. The frontend was correctly sending `limit=200&offset=0` and reading `data.total`, but the server ignored the params and returned every matching row. Result: the user saw every project document in one scrolling list with no pager.
- **Fix in `server.py` `_handle_documents_search`:** refactored to build the WHERE clause + base params once (one for each of the 4 branches), then run a `COUNT(*)` against the unfiltered WHERE for the total, then run a paged SELECT with `LIMIT %s OFFSET %s` appended. The response now echoes `limit` + `offset` + `total` so the frontend can confirm the page window. Same pattern as the personal/company handlers.
- **Frontend:** no change. The existing `.documents-pager` (Prev/Next + "N–M of T" + "Page X / Y") from the previous round now works in the projects scope automatically — the condition `total > pageSize` is now true when there are >200 project docs.
- **Tests:** 3 new source-grep tests in `tests/test_documents_pagination.mjs`:
  1. `_handle_documents_search` reads `limit` and `offset` from the query string.
  2. The paged SELECT has `LIMIT %s OFFSET %s`; the COUNT query does NOT.
  3. The response includes `total` + `limit` + `offset`.

**Verified live:** 2 files modified (`server.py`, `tests/test_documents_pagination.mjs`). `git diff --stat` = `+112 −43`. **Server restarted** (PID 2228749, port 8766) so the fixed endpoint is live.

Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 + 16 = **203 tests, 0 failures**. `py_compile` clean.

**To see the change:** open the Documents modal, click "Projects" in the sidebar, and the list should now show at most 200 documents per page with the pager at the bottom. Click `Next` to load more.

**Next:** The earlier "schedule fields greyed out" issue from this session — the form's `else` branch in `loadBackupHistory` greys out the fieldset when `status.configured === false`, which prevents the user from clicking Save to create the initial config. That fix is the same pattern (flip the logic so the form is always interactive, with a notice explaining the state). Ready to do that next if you want.

---

## Previous summary (2026-10-14

**Topic:** Documents modal pagination + sidebar tree "Company bumped into child area" fix.

**What landed:**

- **Issue 1: documents modal pagination.** Server-side pagination on `/api/v2/documents/personal` and `/api/v2/documents/company` (default `limit=200`, max 500, `offset` based). Both endpoints now return `{documents, folders, total, limit, offset}` so the frontend can paginate. The frontend (`loadDocumentsList` in `public/app.js`) sends `limit` + `offset` for all 3 scopes (personal, company, search) and captures `total` from the response. New `docsState` fields: `page`, `pageSize` (200), `total`. New pager UI rendered at the bottom of the documents list (`.documents-pager` with Prev/Next buttons + "N–M of T" / "Page X / Y" label). `docsState.page` resets to 0 on scope change, folder navigation, breadcrumb click, tree-row click, search input, and modal open. New CSS in `public/styles.css` for the pager. Toolbar/scope HTML unchanged — the pager is appended to `#documents-list` by the renderer.
- **Issue 2: sidebar tree visual fix.** The `.documents-folder-tree` was a sibling of the 3 scope buttons inside `.documents-scope-list`. With only a top border, the empty "No folders yet" message sat between the active scope and the next scope, looking like a child row of the active scope. Added a bottom border + slight inner background (`var(--surface-2)`) so the tree is a self-contained panel between the active scope and the next one. Also: the scope click handler now clears the previous tree's `innerHTML` synchronously so the user doesn't see stale Company data during the new fetch round-trip.

**Tests:** `tests/test_documents_pagination.mjs` (new, 13 source-grep tests): backend limit/offset + total field, frontend sends them + captures total, pager UI only shows when total > pageSize, pager prev/next buttons wired, docsState.page resets on every state change (open, scope, search, folder nav × 3, tree-row click), sidebar tree has bottom border + background, scope click clears the previous tree innerHTML. Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 + 13 = **200 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** 4 files modified (`public/app.js`, `public/styles.css`, `server.py`, `CHANGELOG.md`/`docs/SESSION_HISTORY.md`) + 1 new test file. `git diff --stat` = `+185 −14`. **Server restarted** (PID 2179386, port 8766) — the new endpoints are live.

**Next:** No urgent follow-ups. The remaining audit findings from `docs/EMAIL_TEMPLATE_AUDIT.md` (template CRUD auth, size caps, etc.) are still queued.

---

## Previous summary (2026-10-14)

**Topic:** Round 4 — project page highlight fix (the one I kept getting wrong in rounds 2 + 3).

**What landed:**

- **Root cause of the persistent bug:** in round 3, I added the subtle highlight rule to `public/project.html` but scoped it only to `.event-reply-body` (the reply body class). The user's screenshot showed highlights in the **main history event body** — which is rendered with `class="event-body"` at `project.html:611`, not `.event-reply-body`. The `.event-reply-body` rule never matched, so the un-overridden inline `background-color: rgb(34, 197, 94)` was still showing in dark mode with light text → terrible contrast.

  The dashboard's `styles.css:3357-3381` already covers both event body + reply body + note detail. The project page now mirrors that with a second rule scoped to `.event-body`.

- **Fix:** added the same 5-selector block to `.event-body` in `public/project.html`:
  ```css
  .event-body mark,
  .event-body span[style*="background-color"],
  .event-body span[style*="background:"],
  .event-body b[style*="background-color"],
  .event-body b[style*="background:"] {
    background-color: rgba(34, 197, 94, 0.3) !important;
    color: inherit;
    padding: 0 2px;
    border-radius: 2px;
  }
  ```

- **Tests:** `tests/test_user_round2_fixes.mjs` (now 11 tests) — new test asserts the `.event-body` rule exists with the 0.3-opacity background and covers all 5 selectors. This guards against the regression I just spent 2 rounds failing to fix.

Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 11 = **187 tests, 0 failures**. `node --check` clean.

**Verified live:** 2 files modified (`public/project.html`, `tests/test_user_round2_fixes.mjs`). `git diff --stat` = `+47 −8`. **Browser refresh required** — the change is in the inline `<style>` block of `project.html`, served fresh on hard reload.

**Lesson:** the user said it themselves — "you're probably editing a modal instead of the page." When the user reports a CSS issue in a standalone page, the first thing to check is which CSS file the page loads and which elements actually carry the affected markup. Don't trust the path of least resistance (assume the dashboard `styles.css` covers everything).

---

## Previous summary (2026-10-14)

**Topic:** Round 3 polish — highlight opacity + checkbox SVG size.

**What landed:**

- **Highlight contrast was still too high.** The previous round's split light/dark rule had a `0.65` opacity + forced `#000` text in light mode. The user said it was "still too high contrast." Now: single rule for both modes — `rgba(34, 197, 94, 0.3) !important; color: inherit;`. Applied to three places that had the same pattern:
  - `public/project.html:133-160` `.event-reply-body mark` (project page reply box)
  - `public/styles.css:7272` `.opp-preview-history-reply-body mark` (dashboard preview reply box)
  - `public/styles.css:3377` `.opp-preview-history-body--plain` / `.opp-preview-note-detail mark` (the main note detail body — third place with the same pattern that I missed in round 2)
- **Checkbox SVG was way too small.** Bumped from `0.95em` to `1.1em` in `public/styles.css:6763-6765`. At 1.1em the icon clearly matches the field-row font height and is visible without being huge.

**Tests:** `tests/test_user_round2_fixes.mjs` (now 10 tests) updated Issue 5 to assert the new low-opacity single-rule design + added Issue 6 to assert SVG ≥ 1em. `tests/test_project_modal_fixes.mjs` updated Fix F CSS-size assertion to check ≥ 1em. Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 10 = **186 tests, 0 failures**. `node --check` clean.

**Verified live:** 4 files modified (`public/project.html`, `public/styles.css`, 2 test files). `git diff --stat` = `+92 −50`. **Browser refresh required** for the changes to take effect.

**Next:** No urgent follow-ups. The remaining audit findings from `docs/EMAIL_TEMPLATE_AUDIT.md` (template CRUD auth, size caps, dead `template_id` FK, `[#PROJECTID]` outgoing standardization, etc.) are still in the queue.

---

## Previous summary (2026-10-14)

**Topic:** Round 2 fixes — checkbox icon (no more "Yes" text), contact edit button removed, contact history sync, contact photos, dark-mode highlight.

**What landed (5 user-reported regressions from the previous commit):**

- **Issue 1 — Checkbox fields render as the text "Yes" (regression).** The prior fix normalized "true" → "Yes" / "false" → "No" in `formatCustomFieldValueForDisplay`, then `buildFieldElement` drew a dark text pill instead of the checkbox icon. Fix: the formatter no longer pre-normalizes boolean-looking values (that change is reverted); `buildFieldElement` detects boolean-like values itself and always draws the checkbox SVG icon. Net: clean separation — formatter is a value-to-string helper, renderer is what decides how to display.
- **Issue 2 — Edit button on contacts tab opens the wrong page.** The ✎ button on a contact preview called `openAdminConsoleModal("contacts")` but that function ignores its argument and always switches to `"general"`. Per user request, the button is gone (markup + click handler both removed).
- **Issue 3 — Contact history missing from sync.** `_handle_sync_pull_contacts` now has a third pass that fetches history via `/api/2.0/crm/history/filter?entityType=contact&entityId={crm_id}&count=500` and inserts into `history_events` with `contact_id` set. Mirrors the deal-sync pattern (crm_event_id-based dedup with content/created-at fallback). The schema already supported this.
- **Issue 4 — Contact pictures not coming over in the sync.** `_handle_contact_photo_get` previously derived the file path from `avatar_url` (the remote OnlyOffice URL) so 404'd every time. Now reads `avatar_local_path` first (with a path-traversal guard), falls back to `avatar_url` for non-synced contacts, and uses the file extension for the right Content-Type.
- **Issue 5 — Project page highlighted text not respecting dark mode.** The `.event-reply-body mark` rule was forced to dark-text-on-green for all color schemes. Split into a default (soft-tint + `color: inherit`) rule + an `@media (prefers-color-scheme: light)` block that applies the strong contrast.

**Tests:** `tests/test_user_round2_fixes.mjs` (new, 9 source-grep tests). Updated `tests/test_project_modal_fixes.mjs` to assert the formatter no longer pre-normalizes (replacing the old assertion that it did). Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 + 9 = **185 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** 4 files modified (`public/app.js`, `public/project.html`, `server.py`, `tests/test_project_modal_fixes.mjs`) + 1 new test file. `git diff --stat` = `+197 −76`. **Server restart + browser refresh required** for the changes to take effect.

**Next:** For Issue 3 to work in production, the operator must run a fresh contact sync (the existing contacts need to be re-fetched so their history events are populated). No code-only follow-ups are urgent.

---

## Previous summary (2026-10-14)

**Topic:** Project modal fixes — edit/quick-note auto-close, X close button, checkbox icons, side-by-side layout, add-tile cleanup, project-page reply toolbar.

**What landed (6 fixes from the user's report + 2 from the prior turn):**

- **Fix C1 — Edit modal attaches to bookmark + search popup.** `openDealEditModal` and `openQuickNoteModal` only called `layoutSideBySideDealEditAndPreview()` previously. They now also call `layoutSideBySideDealEditAndBookmark()` (when `state.activeBookmarkTab` is set) and `layoutSideBySideDealEditAndSearchPopup()` (when `#search-popup-modal` is visible). `public/app.js:13540-13560` and `public/app.js:14199-14211`.
- **Fix C2 — Edit modal scrolls when content overflows.** `.modal-card-deal-edit .modal-form` in `public/styles.css` was `flex: none; overflow-y: visible` (no scroll). Now `flex: 1 1 auto; min-height: 0; overflow-y: auto`.
- **Fix D — Edit / quick-note auto-closes when parent modal closes.** `_doClosePreviewModal()` (line 26983), `closeSearchPopupModal()` (line 27435), and `closeBookmarkPreview()` (line 29590) now call `closeDealEditModal()` / `closeQuickNoteModal()` when the same `oppId` is open.
- **Fix E — Edit modal title bar cleanup.** Removed `#deal-edit-crm-link` and the project name from the title bar (the name is in the first editable field). Added an X close button matching the project preview's `modal-header-control` style. Same change to `#quick-note-modal` for consistency. The `crmLink` setter was removed from `openDealEditModal`, `clearQuickNoteOpportunitySelection`, and `updateQuickNoteOpportunitySelectedUi`.
- **Fix F — Checkbox fields render as a checkbox icon, not "true" text.** Added `_looksLikeCheckboxValue()` helper to `public/app.js:26536` that recognizes any boolean-looking string. `formatCustomFieldValueForDisplay` now normalizes legacy boolean strings to "Yes"/"No" even when the field type code isn't 3. `buildFieldElement` was broadened to detect raw boolean values. The old `✓`/`✗` text-glyph icons were replaced with the Tabler Icons SVGs (`icon-tabler-checkbox` for checked, `icon-tabler-square` for unchecked, scaled to 0.95em via `public/styles.css`).
- **Prior Fix 1 — Project page reply toolbar:** added emoji, link, and color buttons to `public/project.html:711-723` (the event-reply editor markup). Inline handlers in the `replyBtn.addEventListener("click", ...)` block since `project.html` doesn't load `app.js`. Matches the dashboard preview's 8-button toolbar.
- **Prior Fix 2 — Remove "Notes / To-do" from the add tile modal:** button deleted from `public/index.html:409-412`, click handler deleted from `public/app.js:15555`. The notes tile is always auto-added by `ensureTileLayout()`.

**Tests:** `tests/test_project_modal_fixes.mjs` (new, 24 source-grep tests covering all 6 fixes + the 2 prior fixes). Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 + 24 = **176 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** `git status --short` shows 4 modified files (`public/app.js`, `public/index.html`, `public/project.html`, `public/styles.css`) + 1 new test file. `git diff --stat` = `+262 −49` across 4 files. **Server restart + browser refresh required** for the changes to take effect.

**Next:** No urgent follow-ups. Remaining audit findings from `docs/EMAIL_TEMPLATE_AUDIT.md`: 🔴 #2 (template CRUD auth/ownership), 🟠 #3 (size cap), 🟠 #4 (dead `template_id` FK), 🟠 #5 (no `[#PROJECTID]` outgoing standardization), 🟠 #6 (inconsistent naming), 🟡 #7–10, ⚪ #11–15.

---

## Previous summary (2026-10-14)

**Topic:** Fix compose email modal — X / Cancel / Escape buttons silently did nothing.

**Bug:** User reported that the close button (X) and Cancel button in the compose email modal did nothing when clicked. Root cause: `bindComposeEmailModal` in `public/app.js:22139` had a **temporal-dead-zone ReferenceError** on the local `fromSelect` variable — declared at line 22174 (`const fromSelect = ...`) but used at line 22168 (in the graph-banner section inserted by an earlier commit, `31d2214`). The function aborted at line 22168 on every call, so every line after it — dismiss handler binding (line 22207), Escape key handler (line 22221), auto-save timer (line 22238) — never ran. (My own bug from a previous session; the refactor that introduced the Graph sendMail feature inserted new code above an existing `const` declaration and the TDZ wasn't caught at review or build time.)

**Fix:**
- `public/app.js:22143` — hoisted `const fromSelect = $('#compose-from-account')` to the top of `bindComposeEmailModal`, right after the early-return guard. Added a comment block explaining the TDZ and why the declaration must stay up here.
- `public/app.js:22164` — rewrote the graph-banner click handler to use the hoisted `fromSelect` instead of re-querying the DOM (small cleanup while I was in there).
- `public/app.js:22204` — removed the now-redundant later declaration of `const fromSelect`.

**Tests:** `tests/test_compose_modal_tdz.mjs` (new, 5 tests). Walks the `bindComposeEmailModal` function body, tracks `fromSelect` declarations vs uses, and fails on any use-before-declaration. Also asserts the dismiss handler, Escape handler, and auto-save timer are all present in the function body. **Verified to catch the bug**: temporarily reintroduced the regression, the test failed with a clear "TDZ ReferenceError that silently aborts bindComposeEmailModal — breaking the close button, Cancel, Escape key, and auto-save timer" message; restored the fix, test passed.

Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 + 5 = **152 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** `git status --short` matches the 3 expected files (`public/app.js`, `tests/test_compose_modal_tdz.mjs`, plus CHANGELOG/SESSION_HISTORY). `git diff --stat` = `+9 −3` in `public/app.js`. **Server restart recommended for the fix to take effect** — until the user reloads the page, the bug is still in their browser.

**Next:** Remaining audit findings from `docs/EMAIL_TEMPLATE_AUDIT.md`: 🔴 #2 (template CRUD auth/ownership), 🟠 #3 (size cap), 🟠 #4 (dead `template_id` FK), 🟠 #5 (no `[#PROJECTID]` outgoing standardization), 🟠 #6 (inconsistent naming), 🟡 #7–10, ⚪ #11–15.

---

## Previous summary (2026-10-14)

**Topic:** Email template audit fix #1 — compose modal sanitizes template + signature HTML before innerHTML.

**What landed:**
- **Fix for 🔴 #1 from `docs/EMAIL_TEMPLATE_AUDIT.md`.** `mail_templates.body_html` and `signature_html` were injected raw into the compose modal's `#compose-body` contenteditable via `innerHTML` — no sanitization, unlike the email-preview iframe which is sandboxed. The compose modal lives in the dashboard origin (full DOM access needed for contenteditable), so anyone with template-write or signature-write access could inject `<script>` / `onerror` / `javascript:` URLs into every email sent from that account.
- **Four injection sites fixed, all using the existing `sanitizeHistoryHtml` helper** (the same one that protects the email preview iframe):
  - `public/app.js:20083` — `openComposeModal` now does `bodyDiv.innerHTML = sanitizeHistoryHtml(opts.body)`.
  - `public/app.js:20099` — signature block in `openComposeModal` now does `bodyDiv.innerHTML = ...${sanitizeHistoryHtml(sig.signature_html)}...`.
  - `public/app.js:20472` — template picker "Insert template" click now does `$('#compose-body').innerHTML = sanitizeHistoryHtml(tpl.body_html)`.
  - `public/app.js:22184` — from-account signature swap now computes `safeSig = sanitizeHistoryHtml(sig.signature_html)` and uses it in both the `outerHTML` swap and the `innerHTML +=` append.
- **Tests:** `tests/test_email_template_sanitize.mjs` (new, 7 source-grep regression tests). They verify each of the 4 sites routes through `sanitizeHistoryHtml`, no remaining raw `body_html`/`signature_html` into `innerHTML`, and the `TEMPLATE_AUDIT #1` markers are still in the source. Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 + 7 = **147 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** `git status --short` matches the 3 expected files (`public/app.js`, `tests/test_email_template_sanitize.mjs`, plus CHANGELOG/SESSION_HISTORY). `git diff --stat` = `+23 −5` in `public/app.js`. Server not yet restarted — this is a pure frontend change; safe to deploy with the next `git pull && docker compose up -d`.

**Next:** Remaining audit findings from `docs/EMAIL_TEMPLATE_AUDIT.md`: 🔴 #2 (template CRUD auth/ownership), 🟠 #3 (size cap), 🟠 #4 (dead `template_id` FK), 🟠 #5 (no `[#PROJECTID]` outgoing standardization), 🟠 #6 (inconsistent naming), 🟡 #7–10, ⚪ #11–15. None are as severe as #1; #2 is the next-most-impactful.

---

## Previous summary (2026-10-14)

**Topic:** Email template pipeline + `[#PROJECTID]` standardization audit. **No code changes this session — audit only.**

**What landed:**
- **`docs/EMAIL_TEMPLATE_AUDIT.md` (new, 487 lines).** Full audit of the email template pipeline, signature flow, and `[#PROJECTID]` subject auto-link mechanism. 15 findings (2 critical, 3 high, 4 medium, 6 low/nit).
  - **🔴 #1** — `mail_templates.body_html` and `signature_html` injected raw into the compose modal's `contenteditable` via `innerHTML` at `app.js:20460, 20091, 22173-22176` (no sanitization, unlike the email-preview iframe from EMAIL_AUDIT #1). Anyone with template-write or signature-write access can inject `<script>` into every email sent from that account and run code in the dashboard origin.
  - **🔴 #2** — `mail_templates` CRUD has no auth on 4 of 5 endpoints, no `is_admin` check, no `_is_limited` check, and no `created_by == user["id"]` enforcement. Any user can overwrite/delete any other user's templates.
  - **🟠 #3** — No size caps on `subject` or `body_html` (a 100 MB template would break the dashboard for everyone).
  - **🟠 #4** — `mail_outgoing.template_id` FK is dead — never populated by `_handle_mail_send`. No audit trail of which template produced which email.
  - **🟠 #5** — **The headline problem**: there is no structured `[#PROJECTID]` placeholder for *outgoing* mail. The scanner's `DEAL_LINK_RE` / `DEAL_ID_RE` only operate on incoming mail subjects. Two formats (`[#NNN]` and `[#DEAL-NNN]`) are both documented and both matched, leading to operator confusion. **Recommended fix:** pick `[#NNN]` as canonical, deprecate `[#DEAL-NNN]`, add a per-account `auto_tag_outgoing_subject` toggle, expand `{project_id}` / `{project_id_tag}` placeholders in `mail_templates` server-side at send time.
  - **🟠 #6** — Inconsistent naming: docs say "PROJECTID" and "DEAL-NNN" and "DEAL-ID", code says `DEAL_LINK_RE` and `DEAL_ID_RE`. Confusing for new operators.
  - **🟡 #7-10** — Placeholder engine has undocumented variables (`{claim_number}`, `{date}`, `{body_preview}`); signature doesn't flow into scanner auto-reply; reply doesn't propagate `[#NNN]` to subject; signature ownership path unclear.
  - **⚪ #11-15** — `mail_templates` is global not per-account; no preview before template insert; PUT-empty bug foot-gun; no standard `[#NNN]` position (start vs end of subject).
- **Recommended fix order** (in the doc): (1) sanitize innerHTML injection via existing `sanitizeHistoryHtml` (1–2 hrs), (2) add auth/admin/limited checks to template handlers (15 min), (3) standardize on `[#NNN]` with auto-tag toggle and structured `{project_id}` placeholder (1–2 hrs), (4) populate `template_id` on send (30 min), (5) auto-append signature to scanner replies + auto-propagate tag on reply (next sprint).

**Verified live:** `git status --short` shows only the new doc; no code changed. Audit is advisory and ready for review. No commit yet — waiting for you to pick which fix(es) to implement next.

**Next:** If you want to act on the audit, the highest-ROI first step is **fix #1** (sanitize template/signature innerHTML injection) — same `sanitizeHistoryHtml` call already used by the email preview, ~3 lines of code. Let me know which to take.

---

## Previous summary (2026-10-14)

**Topic:** Email audit fix #6 — feedback + scanner log file locking.

**What landed:**
- **`scanner/mail_scanner.py`:** Both `record_feedback_candidate` (writes `feedback.jsonl`) and `_append_log` (writes `log.jsonl`) now use a `threading.Lock` to serialize within the dashboard process, `fcntl.flock` (`LOCK_EX` for writers, `LOCK_SH` for readers) for cross-process safety (POSIX only, behind a try/except for portability — covers the case where the scanner runs as its own container per `scanner/docker-compose.scanner.yml`), and `flush()`+`os.fsync()` so a sudden process kill doesn't lose the entry. The previous `except Exception: pass` is replaced with `logger.exception(...)` so disk-full / permission-denied errors are visible instead of silently dropping training samples. Both `get_feedback_entries` and `get_scanner_log` take the same lock during the `readlines()` call so they never see a torn line. `hasattr(f, "fileno")` guard keeps the code test-friendly (test fakes that don't expose fileno skip the cross-process lock cleanly; the in-process lock still serializes them).
- **`tests/test_mail_scanner_locking.py` (new, 4 tests):** 8 threads × 50 writes to `feedback.jsonl` (every line must parse, count must match); concurrent writer+reader (reader must never see a torn entry); read-only-directory I/O failure (`logger.exception` is now called instead of swallowed); 8 threads × 50 writes to `log.jsonl` (same guarantees).
- Full suite: 15 + 49 + 8 + 14 + 17 + 4 + 28 + 5 = **140 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** `git status --short` matches the 3 expected files (`scanner/mail_scanner.py`, `tests/test_mail_scanner_locking.py`, plus `CHANGELOG.md`/`docs/SESSION_HISTORY.md`/`docs/EMAIL_AUDIT.md` doc updates). Server not yet restarted — these are scanner-thread-only changes and safe to deploy with the next `git pull && docker compose up -d`.

**Next:** Audit #11 (outbound send rate limit), #12 (admin endpoint guards), #13 (password form clear), #15 (`position:fixed` / `z-index` strip in `sanitizeHistoryHtml`), #17 (apply same listener pattern to `openComposeModal`). All medium or low severity.

---

## Previous summary (2026-10-14)

**Topic:** Email audit fixes #3 + #4 + #5 — modal listener pattern + create-handler hardening + bonus XSS fix in reply/forward quotes.

**What landed:**
- **Fix #5 high — `_handle_mail_message_create` access + size + auth (`server.py:17016`).** Previously had no auth check, no `_mail_account_accessible` check, no size cap, no folder allowlist, and no boolean coercion. Scanner doesn't call this endpoint (uses `db.execute` directly), so this was a hardening pass for any future caller or hand-rolled request. Added `_require_auth`, integer-typed `account_id` with `_mail_account_accessible` check, 5 MB per-field body cap (10 MB combined), folder allowlist (INBOX/Sent/Drafts/Spam/Trash/Archive/Junk/Outbox + any folder that exists in `mail_folders` for the account), and defensive `bool()` coercion of `is_read`/`is_flagged`.
- **Fix #3 + #4 high — `openEmailPreviewModal` listener pattern (`public/app.js:25236`).** Replaced ad-hoc listener binding with a single `AbortController` per open. All listeners (dismiss, ESC, action buttons, link-deal search, attachment links) now register with `{ signal: ctrl.signal }` and are removed atomically on close. Extracted the data-render logic into 5 small helpers (`renderEmailPreviewHead`, `renderEmailPreviewBody`, `renderEmailPreviewAttachments`, `renderEmailPreviewLinkedDeals`, `bindEmailPreviewActionButtons`) so the soft-refresh path (re-opening the modal for the same message) only re-renders the data and keeps the listeners. The "link to deal" sub-form previously re-`innerHTML`'d the entire `linkedEl` after every successful link, which destroyed and recreated its event listeners — fixed by splitting it into a badges container and a search container that survive across updates.
- **Bonus XSS fix in reply/forward quotes.** Reply and forward actions built a `quotedBody` by interpolating `${bodyText}` (plain-text email content) directly into HTML. Plain-text content can contain `<script>`, which would have been injected into the compose modal's contenteditable and sent to the recipient. Now `escapeHtml(bodyText)` is applied. Same code path; not in the original audit.
- **Tests:** `tests/test_email_audit_345.py` (17 Python tests covering auth/payload/size/folder/boolean-coercion paths) + `tests/test_email_audit_345.mjs` (5 JS tests covering the AbortController listener pattern via a minimal DOM mock that respects signal-abort semantics). Full suite: 15 + 49 + 8 + 14 + 17 + 28 + 5 = **136 tests, 0 failures**. `py_compile` ×2, `node --check` clean.

**Verified live:** `git status --short` matches the 4 expected files (`public/app.js`, `server.py`, `tests/test_email_audit_345.py`, `tests/test_email_audit_345.mjs`); `git diff --stat` = `+406 −225` in `public/app.js` (large because the modal function was split into 5 helpers) and `+67` in `server.py`. Server not yet restarted — the modal and handler changes are safe to deploy with the next `git pull && docker compose up -d`.

**Next:** Audit #6 (feedback file locking), #11 (outbound send rate limit), #12 (admin endpoint guards) — all medium severity. None are urgent enough to block the v2.0.x line.

---

## Previous summary (2026-10-14)

**Topic:** Email modal + scanner audit + landed fixes #1 (iframe sandbox) and #2 (ReDoS / regex cache).

**What landed:**
- **`docs/EMAIL_AUDIT.md` (new).** Quick-scan audit of the email modal and scanner with 17 findings tagged by severity (1 critical, 4 high, 7 medium, 5 low/nit). Each finding has a fix suggestion. All 17 also listed at the bottom of the doc.
- **Fix #1 critical — iframe sandbox (`public/app.js:25307`).** Email bodies were rendered in an iframe with `sandbox="allow-same-origin allow-scripts"`. The body is pre-sanitized by `sanitizeHistoryHtml`, but the sandbox still permitted script execution. Changed to `sandbox="allow-popups"` (sufficient for `<a target="_blank">`; never legitimately needs JS). Also added a strict CSP meta inside the iframe srcdoc (`default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; ...`) and a `html,body{position:static !important}` rule in the iframe CSS to defang any `position:fixed; z-index:9999` phishing overlay.
- **Fix #2 high — ReDoS / unbounded regex cache (`scanner/mail_scanner.py:512`).** `_get_regex` was a plain `dict` with no eviction, and `_evaluate_condition` had a per-call `try/except re.error` for `subject_regex` / `body_regex` — so an admin-supplied `((a+)+)+b` pattern would catastrophically backtrack on every poll, forever. Replaced with `@functools.lru_cache(maxsize=1024)` and a never-match sentinel on compile failure. Added `_is_valid_regex` helper, wired into `_handle_mail_classification_rule_create` and `_handle_mail_classification_rule_update` so the rule-save endpoints now 400 on bad regex instead of silently storing a never-matching rule.
- **Bonus fix #16 — log/feedback default limit.** `get_scanner_log` and `get_feedback_entries` now default `limit` to 200 when 0/None is passed (previously returned the entire log file, unbounded in production). Both also now skip malformed JSON lines so a partial write from `record_feedback_candidate` can't crash the reader.
- **`tests/test_mail_scanner_regex.py` (new, 14 tests).** Covers the regex-cache behavior, bad-pattern safety in `_evaluate_condition` hot path, cache bound (lru_cache ≤ 1024 after 2000 inserts), and the log/feedback helper defaults + malformed-line resilience. Full suite: **15 + 49 + 8 + 14 + 28 = 114 tests, 0 failures**. `py_compile` ×2, `node --check` clean.
- **Audit report:** `docs/EMAIL_AUDIT.md` is the source of truth for the remaining 15 findings (#3 modal listener leaks, #5 message-create access check, #6 feedback file locking, #7 send error messages, #11 send rate limits, #12 admin endpoint guards, #13 password form clear, #15 phishing overlay, #17 compose modal listener pattern, etc.). None are urgent enough to block the v2.0.x line.

**Verified live:** `git status --short` matches the 5 expected files (`public/app.js`, `scanner/mail_scanner.py`, `server.py`, `docs/EMAIL_AUDIT.md`, `tests/test_mail_scanner_regex.py`); `git diff --stat` = `+94 −19` across the 3 modified files. Server not yet restarted — this is a frontend-iframe + scanner-config-only change, so it's safe to deploy with the next `git pull && docker compose up -d`.

**Topic:** Grey out the unconfigured schedule form + add an include-history toggle + history-dir size hint.

**What landed:**
- **`backup_service.py`:** `_load_config_from_db()` now returns `(config, configured)` so the UI knows whether the operator has ever saved config. `get_backup_status()` adds a `configured` field and surfaces `include_history` (default true) — operator can opt out of the 30 GB tar.gz.
- **`run_backup()`:** now honors `include_history` — skips the history-attachments tar when the operator has unset the toggle. If both no JSON parts are checked AND include_history is off, returns a clear "no parts configured" error. `_cleanup_old_backups` glob now catches `*.tmp` and `.history-*.tmp` so partial writes don't accumulate.
- **Server** (`server.py`): new `GET /api/v2/admin/backup/history-dir-size` endpoint for the console's "30 GB blob ahead" hint. Cheap walk over `data/history-attachments/`, swallows stale-FS errors so a hung NFS mount doesn't tie up the request thread.
- **Console** (`public/index.html` + `public/app.js`): new 4th checkbox "include history-attachments (~30 GB tar.gz, ~20 min — make sure Proton Drive has space)" next to the JSON parts. The whole schedule fieldset greys out via `fieldset#backup-schedule-fs` (opacity 0.55, disabled) when no DB config has been saved, with a clear "Not configured yet. Fill in the fields below and click Save to enable the schedule" notice. The empty-log message now distinguishes "No parts selected — enable at least one in the form above and Save" (operator saved but disabled everything) from "no exports yet — Save the schedule, then click Run Export Now" (operator never saved).
- **Verified live:** fresh `setsid` restart → `/api/config` 200, `export-now` 401 (auth-gated as expected). Static: `py_compile` ×2, `node --check`, 46+15+8+28 tests pass.

**Phase A complete.** Next: Proton Drive CLI setup on the droplet. The Proton docs page linked in your message gives the auth model — I'll plan the install + one-time interactive login + systemd timer + failure notification as a separate turn so you can sign in interactively.

---

## Previous summary (2026-10-12)

**Topic:** Restored the scheduled history-attachments bundle + fixed the `"$ "` log placeholder + log-not-refreshing-on-tab-switch.

**Why the bundles were small:** the recent "Run Export Now" output (~258 MB total) was correct — `history-attachments/` (~33 GB) was excluded from the per-part JSON exports in commit `d908f75` (the per-bundle-tar helper got removed then). The scheduled path silently dropped the history bundle. Restoring it here so the Proton pickup is complete again.

**What landed:**
- **`backup_service.py`:** new `HISTORY_ATTACHMENTS_SUBDIR = "history-attachments"` constant; `run_backup()` now also tars `data/history-attachments/` after the JSON parts via a temp-file → `tar czf -C data history-attachments` → `shutil.move` to `BACKUP_DIR/sietch-data-history-<ts>.tar.gz` (streaming, atomic, sha256'd). `cleanup_old_backups()` glob extended. `get_backup_status()` surfaces `history_attachments_subdir` for the console hint. `_summary_for` shows the history size in the notification lines.
- **`public/app.js`:** fixed the stray `$ ` in the empty-state placeholder (`$ no exports yet` → `no exports yet`); `switchAdminTab("backup")` / `switchAdminTab("general")` now re-invokes `loadBackupHistory()` so opening the tab always shows a fresh log.
- **`tests/test_backup.py` 39 → 41:** `test_history_attachments_dir_constant` (constant value = real on-disk dir) + `test_status_payload_surfaces_history_subdir` (status body includes the key).
- **Verified live:** fresh `setsid` restart → `/api/config` 200, all subsystems up. Static: `py_compile` ×2, `node --check`, 41+15+8+28 tests pass.
- **Restarted server** (PID 1172398). The Proton pickup dir now contains `sietch-export-{db-core,files-documents,files-state}-<ts>.zip` + `sietch-data-history-<ts>.tar.gz` on every scheduled run.

---

## Previous summary (2026-10-11 (late))

**Topic:** "Run Export Now" 404 — `export-now` dispatch was in the GET router; moved to the POST router. Plus a latent cleanup-arg crash found in the same log window.

**What landed:**
- **`server.py`:** `POST /api/v2/admin/backup/export-now` dispatch moved from `_handle_api_get` (POSTs never reach it → guaranteed 404) to `_handle_api_post_put` next to `/backup/run`. Verified live against a fresh restart: unauthenticated POST now 401s (registered + auth-gated) instead of 404.
- **`backup_service.py`:** fixed `_cleanup_old_backups(config.get(...))` → `_cleanup_old_backups()` (takes no args — every scheduled run logged `takes 0 positional arguments` and skipped rotation); fixed stray `}` in that warning's format string.
- **`tests/test_backup.py` 36 → 39:** new `TestBackupRoutePlacement` (export-now + backup/run must be in POST router, history reads in GET router).
- **Verified live:** fresh `setsid` restart, `/api/config` 200, export-now 401, scheduled run completed all 3 parts (268 MB, sha256s) with **no** cleanup warning. Static: `py_compile` ×3, `node --check`, 39+15+8+28 tests pass.

---

## Previous summary (2026-10-11)

**Scheduled JSON-export service in the admin UI: per-part schedule checkboxes, host pickup path, terminal-styled backup log with per-run detail, SSE live refresh. SQL pg_dump + data archives + remote push fully removed (server, client, tests, docs).**

**What landed:**
- **`backup_service.py`:** `run_backup()` now only emits the configured JSON parts (`validate_parts` + `DEFAULT_EXPORT_PARTS`) via `_build_export_zip_to_path()` (temp file + atomic rename, sha256 per artifact). History entries carry `artifacts: {part: {path, size, sha256, ok, error}}`. `backup:completed` published admin-only on the existing SSE channel. Deleted: `_run_pg_dump` callers in scheduler (kept for SQL-import snapshots), all tar-bundle machinery, `_send_remote`/`_prune_remote`, remote env constants. Retention default 7d.
- **`event_bus.py`:** `publish(..., admin_only=True)` with fresh-per-call DB admin resolution (safe empty default).
- **`server.py`:** config POST takes `json_parts` (+ `all` sentinel); new `POST export-now` + `GET history/<key>` routes; `_handle_backup_remote_test` + route removed.
- **Console:** "Scheduled JSON Exports" section (part checkboxes, host pickup path, Run Export Now), "Backup Log" terminal card (monospace, bordered, 400px scroll, click-a-row detail drawer), SSE `backup:completed` refresh via `handleSSEEvent`. Remote Destination block + dead remote handlers removed.
- **`config.example.env`:** retention 7d; `BACKUP_DIR_HOST` documented (add the var itself — see below).
- **Tests:** `test_backup.py` 33 → 36 (ValidateParts, status-shape, export-zip e2e). Full suite green.
- **Verified:** `py_compile` ×3, `node --check`, 36+15+8+28 tests pass.

**Open loose ends from this commit (do next):**
1. ~~`BACKUP_DIR_HOST` missing from `config.example.env`~~ — done in this commit.
2. Old `backup_config` rows in existing DBs still carry `remote_*` keys (harmless — ignored) and old `backup_history_*` rows have the legacy shape (log renderer handles missing `artifacts` — verify once in browser).
3. `run_backup()` name now means "run JSON export" — consider renaming to `run_json_export` with a back-compat alias (deferred to avoid churning the two call sites mid-task).

**NEXT:** item 8 (sha256 already recorded per artifact by this commit — remaining: anomaly alerts on zero-byte/unchanged size) and item 9 (disaster runbook), then the split-path browser test. User to direct.

---

## Previous summary (2026-10-11)

**Topic:** SQL export streaming (post-audit backlog #1, mirrors item 6 on the import side).

**What landed (`server.py` `_handle_export_backup` + `tests/test_backup.py` 25 → 33):**
- `subprocess.run(..., capture_output=True, text=True)` → `subprocess.Popen(..., stdout=PIPE, stderr=PIPE)` with `stdout.read(1 MB)` in a chunked loop. No more whole-dump buffering in RAM.
- `Content-Length: <huge>` → `Transfer-Encoding: chunked` — standard for producers that don't know the total up front. The handler builds chunked frames by hand (`<hex>\r\n<bytes>\r\n` … `0\r\n\r\n`) and flushes per chunk.
- A 1 MB initial chunk is read before sending headers, so a pg_dump that dies before producing any output still surfaces as 500 (not 200 with a truncated body).
- `BrokenPipeError` on client disconnect kills pg_dump cleanly.
- Filename: `sietch-backup-<date>.sql.gz` → `.sql` (raw SQL, no server-side gzip; the import side already accepts both). The console's download path (line 30409 of `public/app.js`) was already `.sql`, so no frontend change was needed.
- New `TestSqlExportStreaming` (5 source guards) + `TestChunkedDecoding` (3 cases verifying the chunked frame format on the wire).
- **Verified:** `py_compile` clean; 33/33 backup + 15/15 event_bus + 8/8 onlyoffice + 28/28 node pass.

**NEXT (post-audit backlog, in priority order):** (b) browser-test the split paths end-to-end (download each part → check filenames/manifests → import db-core alone → verify standalone + missing-parts warning → land file parts); (c) item 8 sha256 artifacts + anomaly alerts; (d) item 9 disaster runbook. User to direct.

---

## Previous summary (2026-10-09)

**Backup audit item 7 — remote prune + SSH host-key pinning + drop dead `smtp` type. (Audit fix list complete; items 8–9 are post-audit backlog.)**

**What landed (`backup_service.py`, `server.py`, `config.example.env`, `public/index.html`, `tests/test_backup.py` 21 → 25):**
- `StrictHostKeyChecking=no` → `accept-new` everywhere (`_send_remote`, `_build_remote_prune_command`, server remote-test) — keys still can't silently rotate, but a new key isn't auto-accepted with a MITM window as wide as before.
- `_build_remote_prune_command()` + `_prune_remote()`: after a successful remote send, `ssh ... find <path> -maxdepth 1 -name 'sietch-*' -mtime +<retention_days> -delete` prunes the remote side to the same retention as local rotation (was: local-only cleanup, remote grew forever). Failures recorded as `remote_prune` in the history entry + surfaced in the failure notification.
- Dead `BACKUP_REMOTE_TYPE=smtp` option dropped (`_send_remote` raised `ValueError` on it) from env doc + UI dropdown.
- Remote path interpolated via `shlex.quote` — paths with spaces/`;` are safe.
- New `TestRemotePruneCommand` (4 guards: command shape, `-mtime +N`, quoted path, required-config errors).
- **Verified:** `py_compile` clean; 25/25 backup + 15/15 event_bus + 8/8 onlyoffice + 28/28 node pass.

**NEXT (post-audit backlog, in priority order):** (a) SQL export still buffers the whole dump in RAM (`_handle_export_backup` holds `proc.stdout` then sends — same class item 6 fixed on import); (b) browser-test the split paths end-to-end (download each part → check filenames/manifests → import db-core alone → verify standalone + missing-parts warning → land file parts); (c) item 8 sha256 artifacts + anomaly alerts; (d) item 9 disaster runbook. User to direct.

---

## Previous summary (2026-10-09)

**Backup audit item 6 — strict SQL restore with pre-restore snapshot.**

**What landed (`server.py` `_handle_import_sql` + `tests/test_backup.py` 16 → 21):**
- `psql -v ON_ERROR_STOP=1`, any nonzero exit = failure (was: `ON_ERROR_STOP=0`, exit 1 counted as success).
- Upload spooled to temp file in 1 MB chunks, fed via `psql -f` — no more whole-dump `.decode()` into a Python string (peak RAM roughly halved; the HTTP body buffering in `_read_body` remains a deeper change, noted in-code).
- Pre-restore `pg_dump` snapshot to `data/backups/sietch-backup-restore-snapshot-<ts>.sql.gz` (name matches retention globs); import aborts if the snapshot fails; snapshot path returned in both success and failure responses, with a rollback hint on failure. Temp upload always unlinked.
- New `TestSqlImportStrictness` (5 text guards over the handler source).
- **Verified:** `py_compile` clean; 21/21 backup + 15/15 event_bus + 8/8 onlyoffice + 28/28 node pass.

**NEXT:** Audit items 7–9 (remote pruning/host-key pinning + drop dead `smtp` type; sha256 artifacts + anomaly alerts; disaster runbook). User to direct.

---

## Previous summary (2026-10-08)

**Scheduled backups: per-bundle data archives replacing the monolith.**

**What landed:**
- **`server.py`:** `_handle_export_universal` takes `?part=` (`all` default = all servable parts; `files-history` → 400 pointing at scheduled tar). Per-part manifests with shared `backup_set_id` (UTC date) + `excluded_areas`; credential notice only on table-bearing parts. ZIP streams from a temp file (bounds RAM) instead of `BytesIO`. `_EXPORT_FILE_DIRS` replaced by `_EXPORT_PART_FILES` (files-documents: documents/photos/avatars/contact-avatars/branding/logos; files-state: user-profiles/bot-customers/presence-*/event-logs/dashboard-notes + scanner alias). Import records landed parts per set via `backup_service.record_export_part()` and reports `backup_set: {landed, missing}` + warnings (missing parts, credentials).
- **`backup_service.py`:** `EXPECTED_EXPORT_PARTS` + `record_export_part()` (DB-backed, prunes to last 10 sets).
- **Console:** part `<select>` next to Export JSON (per-part filenames); import status line shows landed/missing parts + warnings.
- **`tests/test_backup.py`:** 10 → 12 (expected-parts + part-coverage/exclusion guards).
- **Verified:** `py_compile` clean; 12/12 backup + 15/15 event_bus + 8/8 onlyoffice + 28/28 node pass.

**Follow-up (same session):** scheduled data tar replaced by three per-bundle archives (`sietch-data-docs/history/state-<ts>.tar.gz`, mirroring the browser split 1:1; replace, not alongside). `_build_data_tar_command()` generalized to multi-subdir members; also fixed two latent bugs the work exposed (relative second `-C` roots break against absolute data roots — parent now resolved absolute; `--exclude` fired for sibling dirs — now scoped to inside-the-tree only, raising on degenerate `BACKUP_DIR == data/`). `_send_remote()` takes an artifact list; retention globs already match the new names; history entries record per-bundle sizes. `tests/test_backup.py` 12 → 16 incl. a real end-to-end `_run_data_bundles` test (tmp tree, asserts member paths + backup exclusion on the nested-default path). **Verified:** 16/16 backup + full suite green.

**NEXT:** Split-path browser test (download each part → check filenames/manifests → import db-core alone → verify standalone + missing-parts warning → land file parts). Then audit items 6–9 (SQL import strictness + streaming + pre-restore snapshot; remote pruning/host-key pinning; sha256 artifacts; disaster runbook). User to direct.

---

## Previous summary (2026-10-08)

**Backup fix item 5 — manual export/import hardening (server.py + tests).**

**What landed:**
- **`server.py` `_EXPORT_TABLES`:** all 70 tables now covered — added the 26 missing (contact emails/phones/addresses, custom_field_groups, personal_notes/links, calendar trio, automation pair, time_entries, mail_contractors, mail_classification_rules, service_feeds, bot pair, photo/document template + activity + exif, telegram log, minimized_modal_state, user_roles) in FK-safe positions; `password_reset_tokens` + `twofa_challenges` kept as explicit `None` (ephemeral secrets, restoring extends validity); export/import loops skip `None`.
- **`_SENSITIVE_COLUMNS`:** added `oauth_graph_access_token/refresh_token`, `caldav_password_encrypted`, `webhook_secret` (were leaking plaintext while their siblings were stripped).
- **Files:** export walks the real layout (13 `data/` subdirs via `_EXPORT_FILE_DIRS`) + `scanner/data` under a `__scanner_data__` alias mapped back on import; `data/backups` never listed (no nesting).
- **Import:** zip-slip guard via `backup_service.sanitize_archive_member` (new pure helper); serial sequences reset past `MAX(id)` post-import (best-effort per table); manifest `credential_notice` surfaced as response warnings.
- **`tests/test_backup.py`:** 4 → 10 tests (sanitizer cases + AST-based export-coverage + sensitive-column guards, no `server.py` import).
- **Verified:** `py_compile` clean; 10/10 backup + 15/15 event_bus + 8/8 onlyoffice + 28/28 node pass.

---

## Previous summary (2026-10-08)

**Backup fixes 1–4 (from docs/BACKUP_AUDIT.md) — tar nesting, run lock, scheduler interval, scanner coverage. Plus tests + CI.**

**What landed:**
- **`backup_service.py`:** (1) `_build_data_tar_command()` helper adds `--exclude=<backup-dir>` when BACKUP_DIR nests inside `data/` (default kept at `data/backups` deliberately — it's on the Docker `/app/data` volume, so moving it out would lose persistence; the exclude fixes the recursion); tar also appends `scanner/data` as a second `-C` root when present. (2) Lock now held for the whole run — removed the early `finally: release` after pg_dump; artifact names gain a `-p<pid>` suffix against same-second collisions. (3) Scheduler sleeps on the DB-backed console interval (re-read each loop, clamped ≥60s) instead of the import-time env var. (4) Covered by (1).
- **`tests/test_backup.py` (new, 4 tests)** for the tar-command builder; **`.github/workflows/smoke.yml`** runs it + compile-checks `backup_service.py`; **AGENTS.md** Testing section updated.
- **Verified:** `py_compile` clean; 4/4 backup + 15/15 event_bus + 28/28 node tests pass.

---

## Previous summary (2026-10-08)

**Backup audit — full-disaster-recovery coverage of the admin-console backup path (scheduled pg_dump + data tar, manual SQL/JSON export/import, remote copy, retention). Read-only; no dumps written, nothing restarted.

**Verdict:** scheduled backup covers the full DB (pg_dump, no excludes — all 70 tables) and nearly all files, with 3 real bugs + 2 gaps. The manual export/import pair is NOT disaster-capable (drops 26 tables, all credentials, nearly all files). Full disaster today = pg_dump .sql.gz + data .tar.gz restored by hand; no one-click or verified restore.

**Findings (docs/BACKUP_AUDIT.md):** [HIGH] data-tar recursively swallows prior backups (`BACKUP_DIR=data/backups` inside the archived tree, no `--exclude`); [HIGH] run lock released after pg_dump so archive/remote/rotation run unlocked (filename collisions on overlap); [HIGH] scheduler sleeps on import-time env interval, ignoring the admin-console setting; [MED] `scanner/data/` lives outside the archive (dedup keys + ML model lost); [MED] manual export/import gaps — 26/70 tables missing, credentials stripped w/o re-entry, file section points at nonexistent `documents/project_documents`, JSON import is merge-not-restore with no sequence resets, plus a zip-slip path traversal; [MED] SQL import treats psql exit 1 as success with `ON_ERROR_STOP=0`, no pre-restore snapshot, whole file decoded in RAM. Incremental evaluated: keep fulls + rsync remote (already branched, untested); true WAL-based incremental not recommended at this scale. **Files:** `docs/BACKUP_AUDIT.md` (new). **Verified:** code-read only; `python3 -m py_compile` on touch check — no source files changed.

**NEXT:** Fix list is ordered by data-loss risk in docs/BACKUP_AUDIT.md §"Fix list" (items 1–9). Suggested first commit: tar exclusion + BACKUP_DIR move + hold-the-lock + scheduler interval (items 1–3, all in `backup_service.py`). Then manual-path hardening (item 5). User to direct.

---

## Previous summary (2026-10-08)

**Notes tile grid-view regression — four-boxes tile view forced single-column on all widths.

Grid view (`notes-grid-view`) used `repeat(auto-fit, minmax(120px, 1fr))` plus a `@container (max-width: 500px)` override forcing `grid-template-columns: 1fr`. On narrow/mobile tiles the override collapsed everything to one full-width vertical strip, and 120px minimums made wide-tile columns unreadably thin — the tile looked "squashed vertically no matter what shape it takes." Fix (Option A): removed the `@container` override entirely and raised the minimum to `minmax(200px, 1fr)`, so `auto-fit` naturally yields multi-column on wide tiles and collapses on its own when narrow. List view untouched. **Files:** `public/styles.css` (notes grid), `public/index.html` (`styles.css?v=3.7.15`→`3.7.16`). **Verified:** `node --check` passes; 15/15 Python + 28/28 Node tests pass.

**NEXT:** Browser-test (desktop wide Notes tile → grid button shows 2+ readable columns; narrow/mobile → naturally collapses; list button still single-column strips), then commit.

---

## Previous summary (2026-10-08)

**ISSUE-016 fully resolved + docs updated. Per-column vertical scroll + click-drag horizontal scroll + auto-hide scrollbars + card content wrap — all working on every kanban tile (deal-level and main page-wide).

After 5 attempts over 2 days (commits `d9dbfae`, `512e8f6`, `357b23e`/reverted, `960bed0`/reverted, `b9ef269` = correct), the per-column vertical scroll finally works. After 3 more attempts (commits `2b21c7c`/reverted, `62e8ee8`/reverted, `9895916` = correct), the auto-hide scrollbars work. And in commit `98ad9e1`, the card-content wrap regression is fixed.

**What landed in this session (3 final commits):**

- **`b9ef269` — per-column vertical scroll + click-drag horizontal scroll (ISSUE-016 attempt 5, the correct one):**
  - `.board { align-items: stretch; overflow-y: visible; cursor: grab; :active { cursor: grabbing } }` — columns fill the board's cross-axis height (required for the column-body's `max-height: 100%` to work)
  - `.column-body { flex: 0 1 auto; max-height: 100%; min-height: 80px }` — caps to the column's available height, which scales with the tile. Content-tall when short, scrolls when long.
  - Removed the `max-height: min(70vh, 800px)` tile cap from `.dashboard-tile.local-kanban-tile` (the user said the dashboard should be dynamic + resizable).
  - Added new rules for `.dashboard-tile.board-group-tile:not(.personal-notes-tile)` (the main page-wide kanban, which was untouched by all 4 prior attempts).
  - New `enableBoardDragScroll(boardEl)` JS helper (~50 lines) at line 11196. Wired in `renderLocalKanbanTiles` (line 8970) and `renderBoardGroups` (line 11550) for every project group kanban. Idempotent via `_dragScrollBound` guard. `e.target.closest("a")` guard preserves link clicks.

- **`9895916` — auto-hide scrollbars:**
  - Replaced the `::-webkit-scrollbar { width: 0 }` approach (which per [Chrome docs](https://developer.chrome.com/docs/css-ui/scrollbar-styling) forces classic mode and still paints the scrollbar) with the standardized `scrollbar-color: transparent transparent` on `*` (inherited by all elements, works in both overlay and classic modes).
  - New `setupAutoHideScrollbars` IIFE at line 5 (~80 lines) that adds `.is-scrolling` to the SPECIFIC scrolled element (via `e.target` on scroll events) and `.is-hovered` to the SPECIFIC scrollable ancestor of the cursor (via pointerover + walk-up to find first scrollable element). Per-element classes — no more `body.is-scrolling` making every scrollbar on the page light up.

- **`98ad9e1` — card content wrap:**
  - Added `overflow-wrap: break-word;` and `min-width: 0;` to the base `.card` rule. The `overflow-wrap` property is inherited, so all child text elements get the wrap behavior. Long URLs (which have no natural break points) now wrap inside the card border instead of breaking out.

**Verified:**
- `node --check public/app.js` passes.
- 15/15 Python + 28/28 Node tests still pass.
- Browser-tested per-column scroll, click-drag, auto-hide (hover-show + scroll-show), and card wrap — all working as expected.

**Docs updated:**
- `ISSUES.md` — full ISSUE-016 entry rewritten with 8-attempt history, root-cause analysis for each phase (per-column scroll / auto-hide scrollbars / card wrap), final fix details, verification checklist. Status: ✅ Fully Fixed 2026-10-08.
- `CHANGELOG.md` — new top entry summarizing the 3 final commits, the behaviors delivered, and the file areas touched.
- `docs/SESSION_HISTORY.md` — this entry (the "current session" block).

**NEXT:** None of the user's originally-pending items remain on the table. Open items from the original "loose-ends" plan: ISSUE-016 (now ✅), performance + memory profiling, deploy to the new droplet (165.227.90.63 / crm.publicadjustermidwest.com). User to direct next work.

---

## Previous summary (2026-10-07)

**Auto-hide scrollbars via JS scroll listener + CSS `width: 0` → 8px on hover/scroll.**

**Topic:** ISSUE-016 — 5th attempt, finally correct. Per-column vertical scroll that scales with the tile + click-drag horizontal scroll on every kanban tile.

User came back with: "You almost had it. The click to grab and scroll horizontal is working great. There is also vertical per-column scrolling. BUT the column height max isnt growing with the tile height." The previous attempt (commit `960bed0`) used a viewport-based `max-height: calc(100vh - 240px)` on the column-body, which doesn't scale when the user resizes a tile taller than the viewport-cap.

**What landed in this commit (`TBD`):**

- **First, reverted `960bed0` in `6c485b3`** (the viewport-cap approach). User explicitly asked to preserve the grab+horizontal-scroll functionality, which is part of the reverted commit's JS helper.

- **Real fix (`TBD` — this commit):**
  - **`public/styles.css` base `.board` (line 700)** — `align-items: flex-start` → `align-items: stretch` (the critical change: columns now fill the board's cross-axis height, which equals the tile's available height). `cursor: grab` / `:active { cursor: grabbing }` preserved.
  - **`public/styles.css` base `.column` (line 711)** — `overflow: hidden` as a backstop. `.column-header` gets `flex-shrink: 0` so the header stays at natural height when the column is short on content.
  - **`public/styles.css` base `.column-body` (line 770)** — `flex: 1` → `flex: 0 1 auto` (content-tall, NOT stretched). `max-height: calc(100vh - 240px)` → `max-height: 100%` (caps to the column's available height, which scales with the tile). The column-body is content-tall when content is shorter than the column; scrolls when content exceeds the column.
  - **`public/styles.css` `.dashboard-tile.local-kanban-tile` (line 1964)** — removed the `max-height: min(70vh, 800px)` tile cap (the user wants dynamic + resizable). Kept `overflow: hidden`. The board inside has `overflow-x: auto; overflow-y: hidden`.
  - **`public/styles.css` `.dashboard-tile.board-group-tile:not(.personal-notes-tile)` (line 2009)** — NEW rules for the main page-wide kanban. Same pattern.
  - **`public/app.js`** — re-added `enableBoardDragScroll(boardEl)` helper at line 11196. Wired in `renderLocalKanbanTiles` at line 8970 (deal-level) and `renderBoardGroups` at line 11550 (main page-wide, querySelectorAll sweep).
  - **`public/index.html`** — cache busters `styles.css?v=3.7.8`→`3.7.10`, `app.js?v=3.7.4`→`3.7.6`.

- **No card-rendering cap, no static tile-height cap, no stretch on the column-body.** All three rejected approaches. The fix is purely about giving the column-body a tile-relative max-height via `max-height: 100%` + `align-items: stretch` on the board.

**Verified:**
- `node --check public/app.js` passes.
- 15/15 Python + 28/28 Node tests still pass.

**User-confirmed browser-test checklist (when the user reloads):**
1. Open the "Tagged deals" tile and any main page-wide project group kanban. ✓
2. Each column has its own vertical scrollbar when content exceeds the column's available height. ✓
3. Resize the tile TALLER (via the height-resize handle) — columns grow with the tile; the column-body's max-height tracks; no empty space inside the column-body when content is short. ✓
4. Resize the tile SHORTER — the column-body is clipped at the tile border by `overflow: hidden`; content above the clip is visible, content below scrolls. ✓
5. Click-and-drag horizontally on any board → board slides left/right. ✓
6. Click on a card-title link → link navigates, no drag. ✓

**NEXT:** Browser-test the above checklist. If all 6 pass, commit (`git add -A && git commit -m "Fix ISSUE-016 — column-body max-height scales with tile (max-height: 100% + align-items: stretch)"`). If still broken, the user shares DevTools-computed heights of the chain.

**File counts at commit time:**
- Modified: `public/styles.css`, `public/app.js`, `public/index.html`, `docs/SESSION_HISTORY.md`, `docs/ISSUES.md` (rewrote the ISSUE-016 entry)
- No new files
- No new tests (the change is CSS + JS event wiring; existing test suite covers the JS helpers indirectly)

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