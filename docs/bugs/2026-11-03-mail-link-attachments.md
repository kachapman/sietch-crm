# Bug: linked emails don't put their PDFs/documents in the project's Documents tab

- Date filed: 2026-11-03
- Area: CRM Mail → link email to project → project Documents tab
- Live site: `crm.publicadjustermidwest.com`
- Status: **fix implemented on branch `fix-mail-attachments`; NOT deployed**
- Priority: **High** (customer-visible: the files a customer sent by email never appear in the project)

---

## 1. Symptoms

- A user links an email to a project. The link reports **success**.
- The email appears in the project timeline with the attachment **filenames**.
- The project's **Documents tab is empty** (or missing some of the files).
- The attachment can be downloaded from the *mail* view but not from the project.
- On 2026-11-02, Outlook account 10's downloads failed (projects **75**, **42**,
  **1243**): ~20 linked emails have no files. ~163 older automatic links from
  2026-10-19 never got files copied either.

## 2. Root cause (verified in code)

### 2.1 Sync time: metadata only, no bytes

`scanner/mail_scanner.py` `_store_attachments` (was ~line 1970) inserted a
`mail_attachments` row per attachment and **nothing else**:

```python
db.execute(
    "INSERT INTO mail_attachments (message_id, filename, mime_type, size_bytes, imap_part_id) "
    "VALUES (%s, %s, %s, %s, %s)", ...)
```

No file was written to disk and no hash was computed. On prod only **171 of
2306** attachments had a `stored_path` — the ones written by the *send* path
(`server.py` `_handle_mail_send`), not by the scanner.

### 2.2 Link time: a live mailbox download, silently skipped on failure

`server.py` `_handle_mail_link` (~20461) → `create_email_history_snapshot`
(`scanner/mail_scanner.py` ~1644) → `_copy_mail_attachments_to_project` (~1489).

The copy needs bytes. With no local file it fell back to
`_fetch_mail_attachment_bytes` (~1576), a live IMAP fetch. That function
returned `None` on **any** failure, and the caller did:

```python
if not data:
    continue        # <-- the file is dropped, no log, no error
```

The UI still reported plain success. **A partial or total copy failure was
invisible.**

### 2.3 Relinking could not repair anything

`create_email_history_snapshot` returned early when the timeline entry already
existed:

```python
if existing:
    return True      # <-- the copy never ran again
```

So the obvious user workaround (unlink + relink, or just relink) did nothing.

### 2.4 Side bug: Content-ID vs part id

`_fetch_messages` recorded:

```python
# imap_tools: att.part is the raw MIME part object ... use content_id which
# matches the server-side download lookup key.
"part_id": att.content_id or "",
```

That value was stored in `mail_attachments.imap_part_id`, but the download
matched it against `att_obj.part` — a **part id**, never a Content-ID. The
match therefore never fired, and the fetch only ever worked through its
filename fallback. The same bug was in the two server-side download paths
(`server.py` ~22461 and ~22666).

### 2.5 Why 2026-11-02 failed

The link-time download used the **stale** `mail_accounts.oauth_access_token`.
The scanner's poll loop refreshes the token (`_refresh_oauth_token_if_needed`)
before logging in; the link-time path did not. A single expired or
throttled token made every download for that account fail — and every one of
those failures was silent.

## 3. The fix

### 3.1 Attachments are stored on disk at sync time

`scanner/mail_scanner.py`:

- `_fetch_messages` now carries the attachment **payload bytes** and the
  **real part id** (`_imap_part_id_str(att)`) in the message dict.
- `_store_attachments` writes the bytes to
  `data/mail-attachments/<message_id>/<attachment_id>_<safe_name>` and records
  `stored_path`, `content_hash` (sha256) and `stored_size`.
- Files are written to a `.part` temp file and renamed, so a crash never
  leaves a half-written file that looks like a valid local copy.
- The bytes are **not** put into `mail_messages.attachments_json` (that column
  is JSON — raw bytes would raise `Object of type bytes is not JSON
  serializable` and break the whole mail sync). Only metadata goes there,
  as before.

### 3.2 The link-time copy is reliable, observable and idempotent

`scanner/mail_scanner.py`:

- `_copy_mail_attachments_to_project` → `copy_mail_attachments_to_project_detailed`
  returns `{copied, total, existing, skipped[]}`.
- The **local file is preferred**; the mailbox download is the fallback.
- Every skipped file is **logged** with message id, attachment id, filename
  and reason (no tokens, no passwords):
  `Mail attachment skipped: message=… attachment=… filename=… reason=…`
- **Idempotent:** `history_attachments.mail_attachment_id` records the source
  attachment, and a unique index on `(event_id, mail_attachment_id)` makes a
  second copy of the same attachment impossible. Relinking or re-running the
  backfill never doubles a file in the Documents tab.
- `create_email_history_snapshot` now runs the copy on the **repair path too**
  (when the timeline entry already exists), so relinking repairs a project
  whose files are missing.
- `create_email_history_snapshot_detailed` is the report-returning variant.

### 3.3 The mailbox download retries and refreshes the token

`scanner/mail_scanner.py`:

- `_fetch_mail_attachment_bytes` retries with backoff
  (`MAIL_ATTACHMENT_FETCH_RETRIES` attempts, default 3;
  `MAIL_ATTACHMENT_FETCH_BACKOFF` base seconds, default 2.0).
- **Before every retry the mailbox OAuth token is refreshed** using the same
  `_refresh_oauth_token_if_needed` the scanner's poll loop uses.
- `_fetch_mail_attachment_bytes_once` matches the MIME part by the **real part
  id** (`remote_part_id`), then by the recorded Content-ID, then by filename
  (last-resort).
- The token is never logged.

### 3.4 The server matches parts correctly too

`server.py` gained `_imap_part_id_str()` and `_pick_imap_attachment()`; both
IMAP download paths (`_handle_mail_attachment_download` and the serve path)
now use it, so they no longer rely on the broken part-id match.

### 3.5 The UI reports what actually happened

`server.py` `_handle_mail_link` now returns:

```json
{ "ok": true, "linked": true, "historyCreated": true,
  "filesCopied": 2, "filesTotal": 3,
  "filesSkipped": [ { "attachment_id": 42, "filename": "x.pdf", "reason": "…" } ] }
```

`public/app.js` shows **"Linked to … — 2 of 3 files copied"** and uses the
**warning** toast style when `filesCopied < filesTotal`. The mail-inbox
bulk-link button aggregates the counts across the selected messages.

### 3.6 The one-time backfill

- Admin endpoint: `POST /api/v2/admin/sync/backfill-mail-attachments`
  (admin-only, `_require_admin`).
- CLI: `backfill_mail_attachments.py`.
- **Dry run is the DEFAULT**; a real run needs an explicit `apply` /
  `--apply`.
- It handles both damage families:
  - **(a)** the link and the timeline entry exist but the files are missing →
    copy the files only;
  - **(b)** the link exists but there is **no** timeline entry → create the
    entry through the existing shared path (`create_email_history_snapshot`),
    which then copies the files.
- Idempotent, with per-email reporting and a failure list carrying reasons.
- Optional filters: `project_id`, `account_id`, `since_days`, `limit`.

## 4. New settings

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_ATTACHMENT_MAX_BYTES` | `26214400` (25 MB) | Per-file limit for the local store. Bigger files are **not** stored locally; the metadata row is still written and the link-time mailbox fallback can still fetch them. Each skip is logged. |
| `MAIL_ATTACHMENT_STORAGE_ENABLED` | `true` | `false` disables local storage entirely (metadata only — the old behaviour). |
| `MAIL_ATTACHMENT_FETCH_RETRIES` | `3` | Attempts for the link-time mailbox download. |
| `MAIL_ATTACHMENT_FETCH_BACKOFF` | `2.0` | Base seconds for the retry backoff (attempt N waits `N × backoff`). |

All are read from the environment at import time by
`scanner/mail_scanner.py`. They are **not** in `.env` by default — the
defaults are correct for prod.

## 5. Disk use

| Path | What | Growth |
|---|---|---|
| `data/mail-attachments/<message_id>/<attachment_id>_<name>` | The **source** copy written at sync time. One per attachment, ≤ 25 MB each. | ~ the total size of all inbound mail attachments. For the current prod volume (2306 attachments) this is on the order of **a few GB**; it grows with every new attachment. |
| `data/history-attachments/<opp_id>/<event_id>/` | The **project** copy made when an email is linked. | Grows only when an email is linked to a project. This tree is already the ~33 GB bulk of the backup bundle (`docs/BACKUP_AUDIT.md`). |

Both live under `data/`, i.e. the **`dashboard-data` Docker volume** on the
production droplet — the same volume that already holds the 33 GB
`history-attachments` tree. **No new volume, no new mount, no new backup
path**: `data/mail-attachments/` is inside `data/`, so it is covered by the
existing volume and can be included in the backup bundle later if wanted.

> ⚠ **Deployment note.** The scanner runs **in-process** in the dashboard
> container (`server.py` → `mail_scanner.start_scanner()`), so it writes to
> the *same* `data/` tree the link-time copy reads. The optional standalone
> `scanner/scanner_service.py` deployment has its **own** `scanner-data`
> volume — if that mode is ever used, the scanner's `data/mail-attachments/`
> is NOT the dashboard's, and the link-time copy would fall back to the
> mailbox download (which still works, with retries). Not the prod topology;
> flagged so nobody enables it without reading this.

**Pruning (deferred):** neither tree is pruned today — `data/mail-attachments/`
keeps the source copy even after the file has been copied into a project.
A retention/pruning job (e.g. drop source copies older than N days once the
project copy exists) is **deferred** — see §9.

## 6. Migration

`migrations/043_mail_attachment_storage.sql` (idempotent, `IF NOT EXISTS`):

- `mail_attachments.content_hash` — sha256 of the stored bytes.
- `mail_attachments.stored_size` — bytes actually written.
- `mail_attachments.download_error` — why a file was not stored locally.
- `mail_attachments.remote_part_id` — the **real** IMAP part id (the
  `imap_part_id` column holds a Content-ID and stays as-is for compatibility).
- `history_attachments.mail_attachment_id` — the dedupe key, FK to
  `mail_attachments(id)` ON DELETE CASCADE.
- `uq_history_attachments_mail_attachment` — UNIQUE
  `(event_id, mail_attachment_id) WHERE mail_attachment_id IS NOT NULL`.
- `idx_mail_attachments_content_hash` — partial index for hash lookups.

The migration runner in `db.py` applies it automatically on boot (it records
the filename in `_applied_migrations`). The code degrades gracefully if it
has not been applied yet: the copy falls back to a filename check for
idempotency, and the backfill endpoint falls back to a timeline-entry-only
detection query.

## 7. How to run the backfill

### Dry run (writes NOTHING) — the default

CLI:

```bash
cd /opt/sietch-crm
python3 backfill_mail_attachments.py \
  --db-host 127.0.0.1 --db-name sietch_crm --db-user sietch --db-password "$DB_PASSWORD"
```

Admin API (an admin session required):

```bash
curl -X POST https://crm.publicadjustermidwest.com/api/v2/admin/sync/backfill-mail-attachments \
  -H 'Content-Type: application/json' \
  -b "$ADMIN_COOKIE" \
  -d '{"dry_run": true}'
```

Useful filters (both interfaces): `project_id`, `account_id`, `since_days`,
`limit`.

```bash
# only the projects that were hit on 2026-11-02
python3 backfill_mail_attachments.py --project 75 --project 42 --project 1243
```

### Real run

```bash
python3 backfill_mail_attachments.py --apply
```

or

```bash
curl -X POST .../api/v2/admin/sync/backfill-mail-attachments -d '{"apply": true}'
```

The CLI writes a JSON report to `backfill-mail-attachments-report.json`.

## 8. Rollout steps

1. **Backup prod** — `pg_dump -Fc` the prod DB, and snapshot the
   `dashboard-data` volume's `data/mail-attachments/` + `data/history-attachments/`
   trees (the latter is the ~33 GB backup bundle — use the existing
   `backup_service.py` bundle, do not hand-copy 33 GB).
2. **Deploy + migrate** — `git pull` on the droplet, `docker compose build dashboard`,
   `docker compose up -d dashboard`. Migration `043` is applied automatically on
   boot (check the container log for `Applying migration 043_mail_attachment_storage.sql`).
3. **Backfill dry run on prod** — the command in §7. Save the report.
4. **Ken reviews the dry-run report** — check the counts:
   - ~20 emails from the 2026-11-02 failure,
   - ~163 older automatic links from 2026-10-19,
   - the rest of the linked mail.
5. **Real run** — `--apply`. Re-run once more to confirm it is idempotent
   (the second run must report `0 file(s) copied`).
6. **Spot-check projects 75, 42 and 1243** — open each project's
   **Documents** tab and confirm the PDFs are there and open in the document
   editor.

## 9. Deferred / not done

1. **Pruning of `data/mail-attachments/`.** The source copy is kept even
   after the file has been copied into a project, so the tree grows without
   bound. A retention job (drop the source copy once the project copy
   exists and the mail is older than N days) is deferred — it needs a disk
   budget decision from Ken first.
2. **The `data/mail-attachments/` tree is not in the backup bundle.**
   `backup_service.py` tars `history-attachments/` only. The new source tree
   is not backed up. It is *derivable* (the files are still in the mailbox),
   so this is not urgent, but it is a gap.
3. **The 25 MB limit is per file, not per message.** A message with fifty
   20 MB attachments still writes ~1 GB. A per-message cap would be better.
4. **The link API is still synchronous.** Copying files (and, on a miss, a
   mailbox download with up to 3 retries and backoff) happens inside the HTTP
   request. On a large message this can hold the request open for ~10s. A
   background-job variant would be better; not done because it changes the
   UI contract (the user would not get the "N of M files copied" feedback).
5. **`history_attachments.mail_attachment_id` is not backfilled** for the
   rows that already exist. Those rows are deduped by the filename fallback
   in the copy path. A one-off UPDATE could link them, but the existing rows
   are exactly the ones that need no repair.
6. **The old `imap_part_id` column still holds Content-IDs.** Renaming or
   repointing it would break the two server-side download paths that still
   read it as a fallback. The new `remote_part_id` column carries the real
   part id; the old column is left alone.
7. **The mail modal's own attachment list still resolves live.** The
   project's Documents tab is fixed; the *mail* attachment download path
   still prefers the live mailbox for attachments with no local copy. It
   works (the part-id match is fixed), but it is still mailbox-dependent.
8. **Two pre-existing test failures, unrelated to this work** (they fail
   identically on the base commit `c637526`):
   - `tests/test_scanner_toggle_gating.py` —
     `test_link_step_uses_helper` expects the link step to call
     `_is_behavior_enabled(toggles.get("auto_link_project_id", {}), account_id)`;
     the current code hoisted that check into a `link_allowed` variable.
   - `tests/test_sync_pull_deals_fields.py` —
     `test_update_writes_responsible_user_id` expects the UPDATE to write
     `bid_value`; the current UPDATE does not.
   Both are **source-text guards** that drifted from the code. Not touched
   here (out of scope) — flagged so they are not mistaken for a regression.

## 10. Tests

`tests/test_mail_link_attachments.py` — 30 tests, needs a **real PostgreSQL**
throwaway database (it exercises INSERTs and the unique indexes). It SKIPs
(exit 0) when the DB is unreachable, when the DB has no CRM schema, or when
migration `043` is not applied.

```bash
createdb sietch_mailatt_test
psql -h 127.0.0.1 -U sietch -d sietch_mailatt_test -f init.sql
for f in migrations/0*.sql; do psql -h 127.0.0.1 -U sietch -d sietch_mailatt_test -f "$f"; done

DB_HOST=127.0.0.1 DB_NAME=sietch_mailatt_test DB_USER=sietch DB_PASSWORD=... \
MAILATT_TEST_DB_NAME=sietch_mailatt_test \
python3 tests/test_mail_link_attachments.py
```

`MAILATT_TEST_DB_NAME` is a safety belt: the suite refuses to run against a
database whose name does not match, so it can never touch prod by accident.

`tests/test_mail_link_files_toast.mjs` — 6 tests (no DB needed): the link
toasts must carry the file counts, use the warning style on a partial copy,
and the link API response must expose `filesCopied` / `filesTotal` /
`filesSkipped`.

```bash
node --test tests/test_mail_link_files_toast.mjs
```

Covered:

- the retry succeeds after a transient failure (429 → 200);
- the retry gives up after the configured attempts;
- the token is refreshed before a retry;
- the link API reports `"2 of 3"` (partial copy) with the skip reason;
- the already-linked path reports the counts too;
- relinking copies the files onto an existing timeline entry;
- the detailed snapshot reports the repair;
- linking twice adds no duplicate Documents entry;
- relinking to another project copies again (legitimate second copy);
- the unique index blocks a duplicate `history_attachments` row;
- sync-time storage writes the file and its sha256;
- an oversized file is skipped, the metadata is kept and the skip is logged;
- an oversized payload is not carried in memory;
- the stored attachment is used instead of the mailbox;
- `attachments_json` stays JSON-serializable (the bytes never reach it);
- the full `_store_message` path survives a binary payload;
- the backfill dry run writes nothing;
- the dry run is the default;
- the real run does exactly what the dry run listed;
- the real run is idempotent;
- the backfill creates a missing timeline entry;
- the backfill reports failures with reasons;
- the backfill filters by project;
- the real part id (not the Content-ID) selects the right MIME part;
- the Content-ID is still a usable fallback;
- the server-side picker selects the right part / falls back to the filename /
  returns None.

Both true-negative checks were verified by temporarily reverting the fix:
removing the repair path fails `test_relink_copies_files_onto_an_existing_timeline_entry`,
and removing the idempotency check fails `test_linking_twice_adds_no_duplicate_documents`.
The same was done for the JS guards: removing the bulk-link file note fails
`the bulk mail-link handler shows "N of M files copied"`, and removing the
single-link count block fails `the single-email link (mail preview) shows the
file counts`.

## 11. Files changed

| File | What |
|---|---|
| `scanner/mail_scanner.py` | Sync-time storage, retry/token refresh, detailed copy report, idempotent copy, repair path, part-id matching, new settings. |
| `server.py` | Link API file counts, `_create_email_history_event_detailed`, admin backfill endpoint + route, `_pick_imap_attachment` / `_imap_part_id_str`, both IMAP download paths fixed. |
| `public/app.js` | "N of M files copied" in both link toasts (warning style on a partial copy). |
| `migrations/043_mail_attachment_storage.sql` | New columns + the dedupe unique index. |
| `backfill_mail_attachments.py` | New CLI (dry-run by default). |
| `tests/test_mail_link_attachments.py` | New suite (30 tests, needs a throwaway PostgreSQL). |
| `tests/test_mail_link_files_toast.mjs` | New suite (6 tests, no DB): the link toasts must carry the file counts. |
| `.github/workflows/smoke.yml` | Loads the CRM schema into the CI postgres service and runs the new Python suite. |
