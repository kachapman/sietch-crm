# Bug: linked emails don't put their PDFs/documents in the project's Documents tab

- Date filed: 2026-11-03
- Area: CRM Mail → link email to project → project Documents tab
- Live site: `crm.publicadjustermidwest.com`
- Status: **fix implemented on branch `fix-mail-attachments`; NOT deployed**
  - Round 1: `240cbf9` — the original fix.
  - **Round 2** (independent review): 4 HIGH + 6 MEDIUM issues found and fixed.
    See §12 for the round-2 review log.
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
| `MAIL_ATTACHMENT_STORAGE_ENABLED` | `true` | **Kill switch.** `false` disables local storage entirely (metadata only — the old behaviour). Nothing is written to `data/mail-attachments/` and the link-time copy always falls back to the mailbox. |
| `MAIL_ATTACHMENT_FETCH_RETRIES` | `3` | Attempts for the link-time mailbox download. |
| `MAIL_ATTACHMENT_FETCH_BACKOFF` | `2.0` | Base seconds for the retry backoff (attempt N waits `N × backoff`). A server `Retry-After` (429/503) overrides this; the wait is capped at 120 s. |
| `MAIL_ATTACHMENT_DROP_STORED_ON_PURGE` | `false` | **Retention switch (see §5).** When the mail retention job purges old messages, also delete their stored attachment bytes. **Default `false` = keep the files** — the retention/backup policy for `data/mail-attachments/` is Ken's decision and is **not** made by this code. The setting exists so the policy can be flipped without another code change. |
| `MAIL_ATTACHMENT_STAGING_MAX_AGE` | `3600` | How long a staging folder from a **crashed** run is kept before it is swept. A folder younger than this belongs to a **running** sync and is never touched (accounts poll concurrently). |

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

### 5.1 Retention — the code is ready, the POLICY is Ken's

**The policy is deliberately NOT decided here.** What the code does:

- `MAIL_ATTACHMENT_DROP_STORED_ON_PURGE` defaults to **`false`** → the mail
  retention job **keeps** the stored files. Nothing is deleted implicitly.
- When (and only when) it is set to `true`, the retention job deletes the
  stored bytes of the messages it purges. It:
  - collects the `stored_path` values **before** the delete (the delete
    cascades the `mail_attachments` rows away);
  - refuses to delete anything outside `data/mail-attachments/` (containment
    check on the resolved path);
  - **never** touches `data/history-attachments/` — the project's copy is
    the customer's document and must outlive the mail account (the Oct 29
> 2026 bug).
- The **kill switch** `MAIL_ATTACHMENT_STORAGE_ENABLED=false` stops the
  tree from growing at all (nothing new is written).

So flipping the policy is a one-line env change, with no code change and no
migration. See §9 for what is still open.

## 6. Migration

### 6.1 `migrations/043_mail_attachment_storage.sql` (idempotent, `IF NOT EXISTS`)

- `mail_attachments.content_hash` — sha256 of the stored bytes.
- `mail_attachments.stored_size` — bytes actually written.
- `mail_attachments.download_error` — why a file was not stored locally.
- `mail_attachments.remote_part_id` — a **short, stable** identifier for
  the MIME part: `cid:<Content-ID>` when the part has one, otherwise
  `sha256:<16 hex>:<filename>:<size>` derived from the payload.
  ⚠ **NOT `str(att.part)`** — in imap_tools 1.14.0 `att.part` is the whole
  `email.message.EmailMessage`, whose string form carries the base64
  payload (a 300 KB PDF → **405,411 characters**). The `imap_part_id`
  column holds a Content-ID and stays as-is for compatibility.
- `history_attachments.mail_attachment_id` — the dedupe key, FK to
  `mail_attachments(id)` **ON DELETE SET NULL** (see below).
- `uq_history_attachments_mail_attachment` — UNIQUE
  `(event_id, mail_attachment_id) WHERE mail_attachment_id IS NOT NULL`.
- `idx_mail_attachments_content_hash` — partial index for hash lookups.

### 6.2 `migrations/044_project_documents_unique_file_path.sql` (round 3)

Creates `uq_project_documents_opp_file_path` — UNIQUE
`(opportunity_id, file_path) WHERE is_deleted = FALSE`. It makes a
double-click (two concurrent link requests) unable to show the same PDF
twice in the Documents tab.

⚠ **Why this is a SEPARATE migration.** A UNIQUE index cannot be created on
a table that already holds duplicates — Postgres raises

```
ERROR:  could not create unique index "uq_project_documents_opp_file_path"
DETAIL:  Key (opportunity_id, file_path)=(42, data/x.pdf) is duplicated.
```

and the whole migration file would then fail. `db.py`'s runner records a
failed migration as "applied" (so it is never retried), so the index would
silently never exist. Migration `044` therefore **checks first**:

| Situation | What 044 does |
|---|---|
| **No duplicates** | Builds the index (a `NOTICE` says so). |
| **Duplicates exist** | **Skips** the index and logs a loud `WARNING` with the **count** and a **sample of up to 10 offending keys** (`opportunity_id=… file_path=… (xN)`). |

⚠ **It never deletes or modifies any data.** The duplicate rows are left
exactly as they are — deduplicating them is a separate, explicit operator
decision (they are visible in the admin console's Documents list).

When the index is skipped, the link-time copy still works, but the
double-click protection is gone (the `ON CONFLICT ... DO NOTHING` has no
index to arbitrate against) — that is why the warning is loud.

### 6.3 Why `ON DELETE SET NULL` (and not CASCADE)

`mail_attachments` rows are deleted when the mail account is deleted, when
a message is emptied from Trash, and when the mail retention job purges old
mail. With `ON DELETE CASCADE` that would delete the **project's** timeline
file rows — the Oct 29 2026 bug all over again ("deleting the mail
account cascaded `mail_attachments` away and the project's files
vanished"). `SET NULL` keeps the project's copy: the timeline row, the
Documents row and the file on disk all survive; only the link back to the
mail attachment is dropped (a later relink then recognises the file as a
legacy copy by filename + size).

### 6.4 Every `project_documents` INSERT is conflict-safe

The new unique index applies to **every** row in the table, not just the
mail-attachment ones. The PDF generators name their file
`"<epoch seconds>_<name>.pdf"`, so two generations in the same second with
the same name would collide — and a plain `INSERT` would then raise and
**break the generation**.

`server.py` therefore routes every `project_documents` INSERT through
`_insert_project_document_safely()`, which:

1. inserts with `ON CONFLICT (opportunity_id, file_path) WHERE is_deleted = FALSE DO NOTHING`;
2. if nothing was inserted (the row already exists), reuses the existing
   row's id;
3. if the index does not exist (migration `044` skipped it), falls back to
   a plain INSERT.

Rows with `opportunity_id = NULL` (the standalone/personal documents) are
unaffected: NULLs never conflict in a Postgres UNIQUE index.

### 6.5 State of the dev database

Migrations `043` and `044` were applied to the shared dev database
`sietch_crm` (local, **not** prod) during rounds 1–2 and have since been
**corrected in place** to match the final migrations:
`history_attachments_mail_attachment_id_fkey` is `ON DELETE SET NULL`, and
`uq_project_documents_opp_file_path` exists. The dev database has **0
duplicate** `(opportunity_id, file_path)` keys and holds **no test rows**
(1 user = the original `dev@localhost`, 0 test opportunities, 0 test mail
accounts).

> ⚠ **On prod, migration `044` will report whether it built the index.**
> If prod has duplicate `(opportunity_id, file_path)` rows among the active
> `project_documents`, `044` will SKIP the index and log the count plus a
> sample of the offending keys — nothing is changed. That is expected and
> safe; the duplicates would then need an operator decision.

The migration runner in `db.py` applies it automatically on boot (it records
the filename in `_applied_migrations`). The code degrades gracefully if it
has not been applied yet: the copy falls back to a filename + size check
for idempotency, and the backfill endpoint falls back to a
timeline-entry-only detection query.

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
   bound. **The policy is Ken's call and is NOT decided here.** The code is
   ready: `MAIL_ATTACHMENT_DROP_STORED_ON_PURGE` (default `false` = keep)
   makes the mail retention job delete the stored bytes of the messages it
   purges, with a containment check so it can only ever touch
   `data/mail-attachments/`; and `MAIL_ATTACHMENT_STORAGE_ENABLED=false`
   stops the tree growing at all. Flipping the policy is a one-line env
   change — no code change, no migration. See §5.1.
2. **The `data/mail-attachments/` tree is not in the backup bundle.**
   `backup_service.py` tars `history-attachments/` only. The new source tree
   is not backed up. It is *derivable* (the files are still in the mailbox),
   so this is not urgent, but it is a gap — and it is part of the same
   Ken-decides question as item 1.
3. **The 25 MB limit is per file, not per message.** A message with fifty
   20 MB attachments still writes ~1 GB. A per-message cap would be better.
4. **The link API is still synchronous.** Copying files (and, on a miss, a
   mailbox download with up to 3 retries and backoff) happens inside the HTTP
   request. On a large message this can hold the request open for ~10s. A
   background-job variant would be better; not done because it changes the
   UI contract (the user would not get the "N of M files copied" feedback).
5. **`history_attachments.mail_attachment_id` is not backfilled** for the
   rows that already exist. Those rows are deduped by the filename + size
   fallback in the copy path. A one-off UPDATE could link them, but the
   existing rows are exactly the ones that need no repair.
6. **The old `imap_part_id` column still holds Content-IDs.** Renaming or
   repointing it would break the two server-side download paths that still
   read it as a fallback. The new `remote_part_id` column carries the short
   MIME-part identifier; the old column is left alone.
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
9. **The COC form's "Completion Date" is silently ignored** (found during
   the round-3 verification, NOT caused by this work). The frontend sends
   `completion_date` (`public/app.js:47634`) but no backend code reads it —
   `_create_coc_pdf` hard-codes `datetime.now()`
   (`server.py:11643`). The PDF therefore always shows today's date, not
   the date the user typed. The round-3 instruction was to change this only
   if the COC generator shared code with the mail-attachment path — **it
   does not** (see §13), so the item was skipped as instructed. It is a
   one-line fix (`_create_coc_pdf` gains a `completion_date` parameter,
   the two COC handlers pass `payload.get("completion_date")`) but it
   belongs to its own change, with its own test.
10. **The COC / invoice / photo-report PDF file names use whole seconds**
    (`<epoch seconds>_<name>.pdf`), so two generations in the same second
    with the same name produce the same `file_path`. With the new unique
    index this is now handled (the insert reuses the existing row) rather
    than raising, but the second PDF still overwrites the first FILE on
    disk. Using a higher-resolution timestamp (or a random suffix) would
    fix the file-level collision. Not changed here — out of scope.

## 10. Tests

`tests/test_mail_link_attachments.py` — **76 tests**, needs a **real
PostgreSQL throwaway database** (it exercises INSERTs and the unique
indexes).

**The database safety check is MANDATORY** (round 2, hardened in round 3):

- `MAILATT_TEST_DB_NAME` is **required** — without it the suite refuses to
  run.
- The current database name must match it exactly.
- A hard-coded denylist refuses `sietch_crm` (and `postgres` /
  `template0` / `template1` / `tg_dupe_scratch`) **even if**
  `MAILATT_TEST_DB_NAME` names them.
- ⚠ **The name is checked BEFORE any migration can run** (round 3 / M4).
  `db.init_db()` also runs `db.py`'s migration runner, so the check uses a
  **raw psycopg2 connection** that does not touch the database; only after
  the name is confirmed does the suite call `db.init_db()`.

The suite SKIPs (exit 0) when the DB is unreachable, when it has no CRM
schema, when migration `043` is not applied, or when the safety check
fails.

```bash
createdb sietch_mailatt_test
psql -h 127.0.0.1 -U sietch -d sietch_mailatt_test -f init.sql
for f in migrations/0*.sql; do psql -h 127.0.0.1 -U sietch -d sietch_mailatt_test -f "$f"; done

DB_HOST=127.0.0.1 DB_NAME=sietch_mailatt_test DB_USER=sietch DB_PASSWORD=... \
MAILATT_TEST_DB_NAME=sietch_mailatt_test \
python3 tests/test_mail_link_attachments.py
```

`tests/test_mail_link_files_toast.mjs` — 6 tests (no DB needed): the link
toasts must carry the file counts, use the warning style on a partial copy,
and the link API response must expose `filesCopied` / `filesTotal` /
`filesSkipped`.

```bash
node --test tests/test_mail_link_files_toast.mjs
```

### 10.1 The MIME-part tests use REAL imap_tools objects

The round-1 tests used a hand-rolled fake with a `.part_id` attribute.
**Real `imap_tools.MailAttachment` objects have no such attribute** — the
fake tested a fiction, which is exactly how the `str(att.part)` bug got
through. The tests now build real `MailAttachment` objects from real MIME
messages (`email.message.EmailMessage` + `imap_tools.MailAttachment`), and
one test asserts the dangerous value directly:

```python
naive = str(att.part)
self.assertGreater(len(naive), 400_000)   # 405,411 chars for a 300 KB PDF
self.assertIn("QUFB", naive)              # the base64 payload IS in there
```

Covered:

- the retry succeeds after a transient failure (429 → 200);
- the retry gives up after the configured attempts;
- the token is refreshed **before the FIRST attempt** (not just the retries);
- the server's `Retry-After` is honoured;
- a hostile `Retry-After` is capped;
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
- the short MIME-part identifier selects the right MIME part;
- the Content-ID is still a usable fallback;
- **the MIME-part identifier is short and carries NO payload** (the H1
  regression — a 300 KB PDF's `str(att.part)` is 405,411 chars);
- the identifier uses the Content-ID when the part has one;
- the identifier is stable for the same bytes and differs for different
  bytes;
- the server-side identifier is short and payload-free too;
- the server-side picker selects the right part / falls back to the filename /
  returns None;
- **a legacy copy (no `mail_attachment_id`) is not duplicated** by a relink
  (the H2 regression);
- a legacy copy with a **different size** is a different file and IS copied;
- the backfill does not duplicate a legacy copy;
- the backfill dry run does **not** report a legacy copy as missing;
- **deleting the mail attachment keeps the timeline row, the Documents row
  and the file on disk** (the H3 regression);
- deleting the mail message keeps the project's copy;
- the FK is `ON DELETE SET NULL` (asserted against `pg_constraint`);
- **`_fetch_messages` is a generator** (the H4 regression);
- the generator yields one message at a time (nothing is fetched eagerly);
- the staged file is moved into the store (not read into RAM);
- an oversized staged file is refused and removed;
- the storage kill switch stores nothing;
- retention keeps the stored files by default and never touches the
  project's copy;
- **a memory measurement**: a 200-message folder of 2 MB attachments (400 MB)
  keeps the live set under 16 MB and carries **zero** attachment bytes in
  the message dict;
- a direct old-vs-new memory comparison (the old shape holds the bytes, the
  new one does not);
- **the timeline lookup matches the message id exactly** — message 12 does
  not match an entry for 1200 (the `strpos` prefix bug).

Every fix has a **verified true-negative check** — the test was confirmed to
fail with the fix temporarily reverted:

| Bug | Probe | Test that failed |
|---|---|---|
| H1 | restore `str(att.part)` | `test_part_identifier_is_short_and_carries_no_payload` |
| H2 | drop the legacy filename+size dedupe | `test_relink_does_not_duplicate_a_legacy_copy` |
| H3 | re-create the FK as `ON DELETE CASCADE` | `test_deleting_the_mail_attachment_keeps_the_project_copy` |
| H4 | make `_fetch_messages` return a list | `test_fetch_messages_is_a_generator`, `test_the_generator_yields_one_message_at_a_time` |
| exact match | restore `content LIKE '%"message_id": 12%'` | `test_the_scanner_lookup_does_not_match_a_longer_id` |
| **N1** | go back to a shared staging folder | `test_two_concurrent_account_syncs_keep_their_own_files` |
| **N2** | put the index back into 043 | `test_the_index_is_not_in_migration_043` |
| **M4** | call `init_db()` before the name check | `test_a_mismatched_name_is_refused` |
| round 1 | remove the repair path | `test_relink_copies_files_onto_an_existing_timeline_entry` |
| round 1 | remove the idempotency check | `test_linking_twice_adds_no_duplicate_documents` |
| JS | remove the bulk-link file note | `the bulk mail-link handler shows "N of M files copied"` |
| JS | remove the single-link count block | `the single-email link (mail preview) shows the file counts` |

## 11. Files changed

| File | What |
|---|---|
| `scanner/mail_scanner.py` | Sync-time storage (streaming, staged to disk), **run-scoped staging folders**, short MIME-part identifier, retry/token refresh + `Retry-After`, detailed copy report, multi-key idempotent copy (incl. legacy), repair path, `ON CONFLICT DO NOTHING`, retention switch, new settings. |
| `server.py` | Link API file counts, `_create_email_history_event_detailed`, admin backfill endpoint + route (legacy-aware counts), `_pick_imap_attachment` / `_imap_part_id_str`, both IMAP download paths fixed, exact timeline matching, `_insert_project_document_safely` (every `project_documents` INSERT is conflict-safe). |
| `public/app.js` | "N of M files copied" in both link toasts (warning style on a partial copy). |
| `migrations/043_mail_attachment_storage.sql` | New columns + the dedupe unique index + the FK corrected to `ON DELETE SET NULL`. |
| `migrations/044_project_documents_unique_file_path.sql` | **new** — the `project_documents` unique index, with a duplicate check that skips (and logs) instead of failing. |
| `backfill_mail_attachments.py` | New CLI (dry-run by default); legacy-aware "already complete" counting; exact timeline matching. |
| `tests/test_mail_link_attachments.py` | New suite (76 tests, needs a throwaway PostgreSQL; mandatory DB safety check before any migration). |
| `tests/test_mail_link_files_toast.mjs` | New suite (6 tests, no DB): the link toasts must carry the file counts. |
| `.github/workflows/smoke.yml` | Loads the CRM schema into the CI postgres service and runs the new Python suite. |

## 12. Round-2 review log (independent review of `240cbf9`)

Ten issues were raised against the round-1 commit. All are fixed.

### HIGH

**H1 — `_imap_part_id_str` returned the whole MIME part.**
`str(att.part)` in imap_tools 1.14.0 is the complete
`email.message.EmailMessage` **including the base64 payload**: a 300 KB PDF
becomes a **405,411-character** string. It was being written to
`mail_attachments.remote_part_id` and `mail_messages.attachments_json`
and returned by the inbox/message APIs.
*Fixed:* the identifier is now `cid:<Content-ID>` when the part has one,
otherwise `sha256:<16 hex>:<filename>:<size>` — short, stable, derived
from the payload, capped at 200 chars, never payload text. The round-1
tests used a fake `.part_id` attribute that **real imap_tools objects do
not have**; they now build real `MailAttachment` objects from real MIME
messages, and one test asserts the dangerous value directly.

**H2 — legacy copies were duplicated.**
Files copied before migration `043` have `mail_attachment_id = NULL`, so
the dedupe (which only looked at that column) did not recognise them: a
relink or a backfill turned 1 timeline row + 1 Documents row into 2 + 2.
*Fixed:* `_find_existing_project_copy` matches on `mail_attachment_id`
**or** filename + size (in this event, or in another event of the same
project). The backfill's `copied_count` counts legacy copies too, so the
dry-run numbers are right. The filename-only fallback now applies **only**
when the size is unknown.

**H3 — the FK was `ON DELETE CASCADE`.**
Deleting a mail account, emptying Trash or the retention cleanup would
have deleted the **project's** timeline file rows — the Oct 29 2026 bug.
*Fixed:* migration `043` now uses `ON DELETE SET NULL` (043 has not
shipped, so the migration itself was edited). The dev database
`sietch_crm`, where `043` had already been applied, was corrected in place.

**H4 — the sync held the whole folder in RAM.**
`_fetch_messages` built a LIST of every message with the attachment bytes
inside; a first sync, a mailbox renumbering or a watermark reset could
take the CRM down.
*Fixed:* `_fetch_messages` is now a **generator**. It collects the UIDs
first (cheap), then fetches and yields **one message at a time**; each
message's attachment bytes are written to a **staging file** on disk as it
is fetched and the dict carries only the path. `_store_attachments` MOVES
the staged file into place (never reads it into RAM) and hashes it in
chunks. The staging directory is swept at the start of every poll — see
§13 (round 3) for how that sweep was made run-scoped.

### MEDIUM

- **`Retry-After` honoured; token refreshed before the FIRST attempt.**
  The 2026-11-02 failure was a stale token, so refreshing only before the
  *retries* wasted the first attempt. The token is now refreshed before
  attempt 1 and before every retry, and a `Retry-After` (429/503)
  replaces the plain backoff (capped at 120 s).
- **Double-click race.** There is no plain-insert fallback that bypasses
  the dedupe any more: the `history_attachments` INSERT uses
  `ON CONFLICT (event_id, mail_attachment_id) ... DO NOTHING` and the
  `project_documents` INSERT uses
  `ON CONFLICT (opportunity_id, file_path) ... DO NOTHING` (backed by the
  new `uq_project_documents_opp_file_path` index). The losing request
  deletes its file and reports the attachment as already copied.
- **Exact timeline matching.** `strpos(content, '"message_id": 12')` also
  matched `"message_id": 120` and `1200`. The lookup is now a regex
  requiring a delimiter (`([,}])`) after the id — in the scanner, in the
  backfill endpoint and in the CLI.
- **Mandatory test-DB safety.** `MAILATT_TEST_DB_NAME` is required, must
  match exactly, and a denylist refuses `sietch_crm` even when it is named
  explicitly.
- **`MAIL_ATTACHMENT_STORAGE_ENABLED`** documented as the kill switch
  (default `true`).
- **Retention is ready but NOT decided.** `MAIL_ATTACHMENT_DROP_STORED_ON_PURGE`
  (default `false` = keep) makes the retention job delete the stored bytes
  of the messages it purges, with a containment check; the project's copy
  is never touched. The policy itself is Ken's call — see §5.1.

## 13. Round-3 review log (independent review of `ffe826e`)

Three issues were raised against the round-2 commit. All are fixed.

### N1 — staging folder collisions between concurrent runs

Accounts poll **concurrently** (`_poll_mailboxes` uses a
`ThreadPoolExecutor` with up to 4 workers, and the manual per-account
workers run in their own threads). Round 2 gave every run the SAME staging
folder `data/mail-attachments/.staging/`, and:

* each run **swept that folder at the start** — so run B starting while
  run A was mid-flight **deleted run A's staged files**;
* the staged file name was `<uid>_<pid>_<index>_<name>` — two accounts
  fetching the same UID could write the same name.

*Fixed:*

* every run gets its **own** subfolder `data/mail-attachments/.staging/<run-id>/`,
  where the run id is `acct<account id>-<random 12 hex>` — the account id
  keeps it traceable, the random suffix keeps two concurrent runs of the
  *same* account apart;
* a run clears **only its own** folder, and removes **only its own** folder
  when it finishes (in a `finally`, so an abandoned generator still cleans
  up);
* leftovers from a **crashed** run are swept by AGE
  (`MAIL_ATTACHMENT_STAGING_MAX_AGE`, default 3600 s) — a folder younger
  than that belongs to a **running** sync and is never touched;
* the staged file name also carries a random token, so a name collision is
  impossible even inside one folder.

### N2 — the unique index moved out of 043 into 044

The `uq_project_documents_opp_file_path` index was in migration `043`, which
was wrong: a UNIQUE index cannot be created on a table that already holds
duplicates, and `db.py`'s runner records a failed migration as "applied" (so
it would never be retried).

*Fixed:* the index moved to **`migrations/044_project_documents_unique_file_path.sql`**,
which **checks for duplicates first**:

* no duplicates → builds the index (a `NOTICE` says so);
* duplicates → **skips** the index and logs a loud `WARNING` with the
  **count** and a **sample of up to 10 offending keys**.

⚠ The migration **never deletes or modifies any data** — the duplicate rows
are left exactly as they are. See §6.2.

Migration `044` also applies to **every** row in `project_documents`, not
just the mail-attachment ones — so all the PDF generators (COC, invoice,
photo report, document upload, doc create/copy, bot upload) now insert
through `_insert_project_document_safely()`, which uses
`ON CONFLICT ... DO NOTHING` and reuses the existing row on a conflict.
Without that, two PDFs generated in the same second with the same name
would have raised and **broken the generation**. See §6.4.

### M4 — the test database name must be checked BEFORE any migration

`db.init_db()` also runs `db.py`'s **migration runner**. The round-2 setup
called it **before** the database name was checked — so pointing the suite
at a non-test database would silently apply the pending migrations to it.

*Fixed:* the name is now checked first, using a **raw psycopg2
connection** (`_current_database_name()`) that does not touch the database
in any way. Only after the name is confirmed does the suite call
`db.init_db()`.

### The classic COC generator — NOT shared, item skipped

The instruction was: *"if the classic COC generator shares code with the path
you touch, make the classic COC use the entered `completion_date` instead of
the current date. If that code is not shared on this branch, skip this item
and say so."*

**It is not shared — the item is skipped.** Verified:

* the COC code lives in `server.py` ~11163–11700 (`_handle_project_coc_generate`,
  `_handle_standalone_coc_generate`, `_create_coc_pdf`). The whole
  mail-attachment diff is at lines 2161, 4776, 12406, 12454, 20458, 20495,
  22158 and 22363 — **no overlap**;
* `_create_coc_pdf` calls only `fpdf` methods plus `_doc_pdf_safe`,
  `_resolve_document_template` and `_humanize_file_size` — **none** of
  which this work touched;
* the COC path never touches `mail_scanner`, `_pick_imap_attachment`,
  `_imap_part_id_str` or the attachment copy — **none** of which it uses.

⚠ **A real bug was found while verifying this** (and is left alone, because
the instruction says to skip the item when the code is not shared): the
frontend sends `completion_date` in the COC payload
(`public/app.js:47634`), but **no backend code reads it** —
`_create_coc_pdf` hard-codes `datetime.now().strftime("%m/%d/%Y")`. So the
completion date the user types into the COC form is **silently ignored** and
the PDF always shows today's date. This is unrelated to the mail-attachment
work (it is not caused by it) and is listed in §9 for a separate fix.
