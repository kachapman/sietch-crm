# Bug: Sietch shows the wrong time for emails (off by the sender's UTC offset)

- Date filed: 2026-11-09 (Ken, after the OAuth account-binding incident)
- Area: CRM Mail — ingestion (`scanner/mail_scanner.py`), storage
  (`mail_messages.date_received`), API serialization (`server.py`), display
  (`public/app.js`)
- Status: **fixed at source on branch `fix/oauth-account-binding`. Not
  deployed. Existing rows need the backfill (dry-run first, Ken approves).**
- Symptom: an email Outlook shows as received **Mon 11/9/2026 1:20 PM**
  (America/New_York, EST, i.e. **18:20:03Z**) appears in the Sietch email view
  as **"11/9/2026, 2:20:03 AM"**.

---

## 1. Why it is not "just UTC being displayed"

18:20Z displayed as UTC would read **6:20:03 PM**. A timezone conversion is
also bounded by ±14h and cannot preserve the minutes — the minutes/seconds
(20:03) survived, so the *value* was already wrong (or was parsed without its
offset) before rendering. Two independent defects compound:

| # | Layer | Defect |
|---|-------|--------|
| A | Storage | `date_received` was `TIMESTAMP` (no tz). PostgreSQL **silently ignores the offset** in a literal for that type, so the scanner stored the *sender's wall clock* (13:20) as if it were UTC. |
| B | Display | The frontend fed offset-less strings to `new Date()` (parsed as **browser-local**) and rendered with the browser timezone; ~40 hand-rolled `new Date(...).toLocaleString()` calls, no single formatter. |

A alone shifts every mail time by the sender's UTC offset (5h for an EST
sender, 4h in DST). B can shift it again — including *back* toward the truth
when the browser and the sender share a zone, which is why the bug "keeps
recurring" and why some rows look fine and others are wildly off.

## 2. Proof of A (run against the local dev DB, rolled back)

```
CREATE TEMP TABLE t (d TIMESTAMP);
INSERT INTO t (d) VALUES ('2026-11-09T13:20:03-05:00');
SELECT d FROM t;                    -- 2026-11-09 13:20:03   ← offset dropped
ALTER TABLE t ALTER COLUMN d TYPE timestamptz USING d AT TIME ZONE 'UTC';
SELECT d, d AT TIME ZONE 'America/New_York';  -- 13:20Z, 8:20 AM   ← 5h early
```

The session timezone was UTC, so this is not a `SET TIME ZONE` accident:
`timestamp` ignores the offset by definition. Every row imported by the old
scanner holds the sender's wall clock.

## 3. What was fixed

### Storage — migration 048
`mail_messages.date_received` → `timestamptz`, with
`USING date_received AT TIME ZONE 'UTC'` (declares the existing naive values
to be UTC — **no double shift**). Mirrored as idempotent boot DDL in
`server.py` and in `init.sql` for fresh installs. The DO block only converts a
still-naive column, so re-running is a no-op.

### One parser — `mail_dates.py`
`parse_email_date` / `parse_graph_received_datetime` / `iso_utc`:
- always returns an **aware UTC** datetime; never drops `tzinfo`;
- naive input is **UTC**, never server-local;
- RFC-2822 `Date:` headers (`-0500`, `GMT`, `+0530`, `(EST)` comments);
- Graph `receivedDateTime` with **7 fractional digits** (Python's
  `fromisoformat` accepts 6, so the fraction is normalized);
- ISO output always carries an offset (`+00:00` / `Z`).

The scanner now writes an aware datetime and emits `iso_utc_z(...)`; every
`str(<mail datetime>)` that reached the API was replaced with `iso_utc(...)`.

### One formatter — `public/mail-dates.js`
`app.js` imports it; the mail list, preview header, reply/forward quote and
thread rows route through it. Naive values are UTC; the wall clock is rendered
in the viewer's timezone (**user setting → browser → America/New_York**). The
module is pure, so `tests/test_mail_dates.mjs` imports the shipped code and
asserts exact strings (including DST).

### Existing rows — `scripts/backfill_mail_dates.py`
The `Date:` header saved in `mail_messages.raw_headers` is the message's own
statement of when it was sent, so it is authoritative. The script:
- **dry-run by default**; prints counts and a delta histogram (seconds) only —
  no subject, address, body or token;
- `--apply` corrects every row whose stored instant disagrees with its header
  (idempotent: a second run reports 0);
- `--apply-inferred` additionally shifts rows with no header by the modal
  (systematic) delta — opt-in, because it is inference, not proof;
- `--sample N` prints the largest deltas as `id, stored, header` for review.

Because defect A dropped the sender's offset, the expected histogram for a
US/Eastern sender is `-18000` (winter) / `-14400` (summer). That is the
deterministic signature the dry run should show before anyone applies it.

## 4. What the user sees after each step

| After | Display |
|-------|---------|
| Nothing (today) | wrong by the sender's offset, sometimes worse |
| This branch deployed | **still wrong for old rows** (they now carry an explicit, but incorrect, UTC instant) — correct for new mail |
| Backfill `--apply` | correct for rows with a saved `Date:` header |
| Backfill `--apply-inferred` | also corrected for header-less rows |

This is why the display fix alone is not enough and the backfill must run
after Ken approves it.

## 5. Tests

`tests/test_mail_dates.py` (41): RFC-2822 offsets, naive-is-UTC, epoch
seconds/ms, Graph 7-digit fractions, spring-forward/fall-back DST, ISO
serialization, backfill classification (idempotency, dry-run writes nothing,
apply writes only fixable rows, no content in the report), source guards.

`tests/test_mail_dates.mjs` (14): the shipped formatter — the reported case
must render `11/9/2026, 1:20:03 PM` and never `2:20:03 AM`; DST boundaries;
invalid timezone falls back to UTC, not browser-local.

## 6. Risks / follow-ups

- The backfill touches only `mail_messages.date_received`. `mail_messages`
  rows linked to deals keep their timestamps; ordering across DST boundaries
  may change slightly after the fix (it becomes correct).
- Other tables still store naive `TIMESTAMP` (`mail_folders.last_sync`,
  `mail_accounts.last_sync`, most of the CRM). They are out of scope for this
  incident; `mail_dates` is the shared helper to migrate them onto.
- `receivedDateTime` from Graph is supported by the parser, but **no Graph
  mail-fetch path exists yet** (Graph is send-only today), so it cannot be the
  source of the current values.
