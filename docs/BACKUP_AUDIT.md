# Backup Audit — full-disaster-recovery coverage

**Date:** 2026-10-08
**Scope:** admin-console backup path end-to-end — scheduled `backup_service.py`
backups, manual SQL/JSON exports, SQL/JSON imports (the restore path),
remote copy, retention, notifications. Goal: **full disaster recovery**
(whole DB + all files), restore path included. Incremental evaluated as
an option (§7).

**Method:** code-read audit (no dumps written, nothing restarted).
Sources of truth: 70-table inventory from `init.sql` + `migrations/`
+ runtime `CREATE TABLE` in `server.py`; on-disk write inventory of
`data/` + `scanner/data/`; handler reads of every backup/import route.

---

## Verdict

The **scheduled backup** (`pg_dump` + `data/` tar) covers the full DB
and nearly all files, with three real bugs (§1–§3) and two gaps (§4).
The **manual export/import pair** (the only in-console "restore" path)
is **not disaster-capable**: it silently drops 26 tables, all
credentials, and nearly all files (§5). A real disaster today =
`pg_dump` .sql.gz + `data/` .tar.gz restored by hand; there is no
one-click or verified restore.

---

## 1. [HIGH] `data/` tar recursively swallows prior backups (unbounded growth)

`backup_service.py:27` defaults `BACKUP_DIR=data/backups` — **inside**
the tree being archived. `_run_data_archive` (`backup_service.py:348`)
tars `data/` with **no `--exclude`**:

```python
["tar", "czf", str(output_path), "-C", str(data_dir.parent), str(data_dir.name)]
```

Every scheduled run therefore embeds all previous
`sietch-backup-*.sql.gz` + `sietch-data-*.tar.gz` files into the new
`sietch-data-<ts>.tar.gz` — including the tar currently being written
if the glob picks it up mid-run. Archives grow superlinearly; with the
33 GB `history-attachments/` tree on disk, one extra nested copy per
day exhausts the droplet fast. `_cleanup_old_backups` only deletes by
mtime afterwards — the bloat is already baked into the surviving
archives. The output file itself (`BACKUP_DIR/sietch-data-<ts>.tar.gz`)
is inside the source tree while tar reads it (torn/racy archive).

**Fix:** default `BACKUP_DIR` outside `data/` (e.g. `backups/` at repo
root, still on the `/app/data` volume in Docker via a second mount or
a documented path), **and** add `--exclude=data/backups` (parameterized
by actual `BACKUP_DIR`) to the tar command as defense-in-depth.
Existing nested archives should be treated as suspect (re-run a clean
full backup after the fix, verify size ≈ `du -sh data` minus backups).

## 2. [HIGH] The run lock only guards `pg_dump`, not the run

`run_backup` (`backup_service.py:232`) acquires `_backup_lock`, but the
`finally: _backup_lock.release()` at line 262–263 fires right after the
`pg_dump` phase — the data-archive, remote-send, and rotation phases
run **unlocked**. Two overlapping runs (manual "Run now" + scheduler,
or double-click) both enter the archive phase: same second-resolution
`ts_slug` filenames collide/overwrite, tar reads a tree another
process may be mid-write, and `_record_backup` interleaves history
rows. The trailing `finally` (line 317–319) checks `locked()` so it
never re-locks — the early release is the whole bug.

**Fix:** hold the lock for the entire run — move the single release to
the outer `finally` only, and add second+PID or monotonic suffix to
`ts_slug` filenames.

## 3. [HIGH] Scheduler ignores the admin-console interval setting

`_backup_loop` (`backup_service.py:421`) sleeps
`stop_event.wait(BACKUP_INTERVAL)` — the **import-time env var**, not
the DB-backed `config.get("interval")`. The admin UI interval field
writes to `system_settings.backup_config` and is displayed back, but
changing it never alters schedule timing until process restart (and
then only via env, not the saved value). Same staleness applies to
`enabled`: read fresh each loop (good) while interval is frozen (bad).
An operator who sets "every 6h" in the console silently keeps 24h.

**Fix:** `stop_event.wait(config.get("interval", BACKUP_INTERVAL))`
re-read each loop iteration (config is already loaded per iteration
at line 426).

## 4. [MEDIUM] `scanner/data/` lives outside the archive

`scanner/mail_scanner.py:37-45` roots scanner state at `scanner/` —
`processed_ids.json`, `log.jsonl`, `contractors.json`,
`cached_tags.json`, `feedback.jsonl`, `ml_models/sietch-classifier-head.pkl`.
`_run_data_archive` tars only `data/`. After a total-loss restore,
the scanner replays/re-fetches (IMAP UIDs reprocessed →
duplicate `mail_messages` rows, minus the `account_id:uid` dedup keys)
and the trained classifier head is gone (retrain from
`classifier_training_data`, which **is** in the DB dump — recoverable
but slow).

**Fix:** include `scanner/data/` in the tar (second `-C` root), or move
scanner state under `data/scanner/` (single-tree invariant: "everything
the server writes lives under `data/`").

## 5. [MEDIUM] Manual export/import is not a restore path — gaps

The console offers SQL download (`_handle_export_backup`,
`server.py:11733`), universal JSON ZIP
(`_handle_export_universal`, `server.py:11672`), JSON import
(`server.py:11768`), SQL import (`server.py:11847`). None of these
round-trips a disaster:

- **26 of 70 tables missing from `_EXPORT_TABLES`** (`server.py:11618`):
  `contact_emails`, `contact_phones`, `contact_addresses`,
  `custom_field_groups`, `personal_notes`, `personal_note_links`,
  `calendar_sources`, `calendar_events`, `ics_feed_cache`,
  `automation_rules`, `automation_logs`, `time_entries`,
  `twofa_challenges`, `mail_contractors`,
  `mail_classification_rules`, `service_feeds`, `bot_config`,
  `bot_activity_log`, `photo_report_templates`, `document_templates`,
  `document_activity`, `photo_exif_cache`,
  `telegram_notification_log`, `password_reset_tokens`,
  `minimized_modal_state`, `user_roles`; `sessions` is listed but
  `LIMIT 0` (deliberate). A JSON "restore" silently drops contacts'
  emails/phones/addresses, all personal notes, all calendar sources,
  all automation rules, and the bot token.
- **Credentials stripped, never restored:** `_SENSITIVE_COLUMNS`
  (`server.py:11665`) drops `password_hash/salt/encrypted`,
  `smtp_password_encrypted`, `oauth_access_token/refresh_token`,
  `token` on export. Import has no re-entry path — restored mail
  accounts can't authenticate, users can't log in. Silent.
- **Files: export writes ~nothing.** The file section
  (`server.py:11702`) archives `data/documents/project_documents` +
  `data/photos` — but on-disk layout is
  `data/documents/<opp_id>/`, `data/documents/shared/...`, and
  `data/photos/<opp_id>/`. `data/documents/project_documents` does
  **not exist** (`ls data/documents` → `2383 2384 2411 2531 3572
  shared`), so the `if not src_dir.exists(): continue` skips it
  silently. Missing entirely: `history-attachments/` (33 GB),
  `documents/shared/`, `avatars/`, `contact-avatars/`,
  `branding/`, `logos/`, `user-profiles/*.json`,
  `bot-customers/`, `presence-messages/`, `event-logs/`.
- **JSON import is merge, not restore:** `INSERT ... ON CONFLICT DO
  NOTHING` (`server.py:11819`) skips conflicting rows without report
  per-row (counts only), and sequences are never reset — restored
  serial PKs + un-advanced sequences → duplicate-key errors on the
  next inserts.
- **Path traversal in JSON import:** `target = data_dir /
  info.filename[6:]` (`server.py:11836`) with no sanitization — a
  crafted ZIP writes outside `data/`.

**Fix (ordered):** (a) sanitize zip paths (`..` rejection) — security;
(b) extend `_EXPORT_TABLES` to all 70 tables and the file section to
the real on-disk layout (`documents/`, `photos/`,
`history-attachments/`, `avatars/`, `contact-avatars/`, `branding/`,
`logos/`, `user-profiles/`, `bot-customers/`, `presence-messages/`,
`event-logs/`, `scanner/data/`); (c) add `setval()` sequence resets
post-import; (d) document that credential columns must be
re-entered (or add an explicit encrypted secrets export); (e) label
the JSON path "merge/migration aid," not "backup."

## 6. [MEDIUM] SQL import accepts partial restores as success

`_handle_import_sql` runs `psql -v ON_ERROR_STOP=0`
(`server.py:11878`) and treats exit code **1 as success**
(line 11887: `if proc.returncode != 0 and proc.returncode != 1`).
A dump that fails halfway (constraint violation, OOM, encoding
error) returns `{"ok": True}` with stderr truncated to 1000 chars.
No pre-restore snapshot, no row-count verification, whole file
decoded into memory (`file_data.decode`, line 11880 — multi-GB dump
= OOM). Mirror issue on export: `proc.stdout` held fully in RAM
before send (line 11755).

**Fix:** `ON_ERROR_STOP=1`, fail on any nonzero exit, stream
upload to temp file + `psql -f` instead of decoding into memory,
and take an automatic pre-restore `pg_dump` snapshot first.

## 7. Incremental — evaluation (option, not implemented)

Today: full `pg_dump` + full `data/` tar every run. No WAL
archiving, no `rsync --link-dest`, no mtime/size diffing. For the
current scale (DB tens of MB + 33 GB mostly-static attachments),
nightly fulls to local disk + remote copy are proportionate; the
pain point is §1 (nesting), not full-vs-incremental.

- **Cheapest real win:** keep fulls, but `rsync` the two artifacts
  instead of `scp` re-send (line 385–393 already has an `rsync`
  branch — untested; the `sftp` branch silently falls back to `scp`
  at line 391). Add `--link-dest` rotation on the remote side for
  cheap incrementals without changing the dump logic.
- **True incremental** (WAL-G / `pg_basebackup` + WAL, or
  block-level volume snapshots) is disproportionate ops load for a
  single-droplet app and makes restore a chain (base + WAL replay)
  instead of a file. Not recommended now; revisit if DB >10 GB or
  RPO <24h is required.
- **Not recommended:** timestamp-based "incremental" table dumps —
  no `updated_at` on most tables, so it degrades into full dumps
  with extra code.

## 8. [LOW] Hardening notes (all verified in code)

- **Auth:** every backup/import/export route is `_require_admin` at
  dispatch (`server.py:3733-3799`). No gap.
- **Container tooling:** `Dockerfile:11` installs `postgresql-client`,
  `rsync`, `openssh-client` — `pg_dump`/`psql` present; the
  `FileNotFoundError` handlers are dead-but-harmless.
- **`.env` excluded everywhere** (never read into dumps — correct).
  But there is **no re-provision checklist**: a fresh droplet needs
  `DB_PASSWORD`, `COOKIE_SECRET`, `DOCS_JWT_SECRET`,
  `TELEGRAM_BOT_TOKEN`, OAuth secrets re-entered by hand. Add the
  list to the restore runbook (`docs/SECRETS_AUDIT.md` §6 already
  enumerates them — link it).
- **Remote copy:** `StrictHostKeyChecking=no` (`backup_service.py:376`,
  `server.py:12216`) — MITM-able; pin host keys. `BACKUP_REMOTE_TYPE=smtp`
  in `config.example.env:84` is a dead option (`_send_remote` raises
  `ValueError`, line 395). Remove it from the example env.
- **Notify prefs bug:** `_notify_admins` line 160 computes `want_email`
  from the failure flag for both branches, then overrides for success
  at 164–166 — net effect correct, dead expression. Harmless; clean
  up when touching the file.
- **Relative `Path("data")`** in `_run_data_archive` (line 350) and
  the export/import handlers breaks if the server ever starts from a
  different CWD (today `start.sh`/Docker both use repo root / `/app`).
  Resolve against the module file (`Path(__file__).parent / "data"`).
- **No integrity verification anywhere:** no checksums recorded,
  no test-restore, no size/row-count sanity bounds. At minimum record
  `sha256` per artifact in the history entry and alert on
  zero-byte/unchanged-size anomalies.
- **Retention asymmetry:** `_cleanup_old_backups` deletes local files
  past retention but never prunes the remote side — remote grows
  forever. Either prune remote or document it.

---

## Coverage matrix

### DB tables (70 total) — scheduled `pg_dump`: ✅ all (no excludes)

Manual universal export (`_EXPORT_TABLES`, 43 entries): ✅ 43 incl.
empty `sessions`; ❌ 26 listed in §5 + `sessions` data (deliberate).

### Files — scheduled `data/` tar: ✅ all under `data/` **except**:

| Path | In tar? | Notes |
|---|---|---|
| `user-profiles/`, `dashboard-notes/`, `user-presence/`, `presence-messages/`, `event-logs/`, `bot-customers/` (JSON stores) | ✅ | core state, covered |
| `photos/<opp>/`, `documents/<opp>/`, `documents/shared/` | ✅ | covered |
| `history-attachments/<opp>/<event>/` (33 GB) | ✅ | covered — the bulk of every archive |
| `avatars/`, `contact-avatars/`, `branding/`, `logos/` | ✅ | covered |
| `backups/` (prior artifacts) | ⚠️ | covered but **shouldn't be** — §1 |
| `scanner/data/` (dedup keys, ML model) | ❌ | outside `data/` — §4 |
| `.env`, TLS keys | ❌ | deliberate — needs re-provision runbook (§8) |
| `contact-photos/` | ➖ | configured default, unused (live path is `contact-avatars/`); empty dir, nothing to lose |

Manual universal export files section: effectively ❌ (wrong source
paths — §5).

---

## Fix list (ordered by data-loss risk)

1. Exclude `BACKUP_DIR` from the tar + move default out of `data/` (§1).
2. Hold `_backup_lock` for the whole run; unique artifact names (§2).
3. Scheduler uses DB interval, not import-time env (§3).
4. Cover `scanner/data/` (include in tar or move under `data/`) (§4).
5. Harden the manual path: zip-slip guard, full 70-table + real file
   layout coverage, sequence resets, credential re-entry docs (§5).
6. SQL import: `ON_ERROR_STOP=1`, nonzero-is-failure, stream to temp
   file, pre-restore auto-snapshot (§6).
7. Remote: prune-to-retention + host-key pinning; drop dead `smtp`
   remote type (§8).
8. Record sha256 per artifact; alert on zero-byte/size anomalies (§8).
9. Write the disaster runbook: fresh droplet → install → `.env`
   (var list) → `psql -f` dump → untar `data/` → restart → verify
   checklist. Link `docs/SECRETS_AUDIT.md` §6 for secrets.

## Verification (for the fix commit)

- `python3 -m py_compile backup_service.py server.py`.
- Unit-testable pure bits (`ts_slug` uniqueness, retention glob,
  interval selection) via the existing `tests/` pattern.
- Dry-run audit re-check: live `SELECT tablename FROM pg_tables` +
  `du` tree vs. tar listing (`tar tzf`) — read-only, no restore.
- No live restore performed in this audit.
