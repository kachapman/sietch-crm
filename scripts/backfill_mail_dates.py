#!/usr/bin/env python3
"""Recompute stored mail timestamps from their original source (idempotent).

WHY
---
Live bug 2026-11-09: an email Outlook shows as received Mon 11/9/2026 1:20 PM
(EST = 18:20Z) appeared in Sietch as "11/9/2026, 2:20:03 AM". A wall-clock
offset can never preserve the minutes, so at least some stored values were
already wrong — this script fixes those rows, not just the display.

WHAT IS AUTHORITATIVE
---------------------
`mail_messages.raw_headers` holds the original `Date:` header the scanner
saved at import time ("Mon, 9 Nov 2026 13:20:03 -0500"). That header is the
message's own statement of when it was sent, so it is the source of truth.
For every row that still has one, the script compares the stored instant with
the header's instant and can correct it exactly.

Rows with no `raw_headers` (manually inserted, imported from OnlyOffice
history, or headers pruned) cannot be verified per row. For those the script
can *infer* the systematic offset from the verifiable rows (the modal delta)
and apply it — opt-in via `--apply-inferred`, because it is inference, not
proof.

SAFETY
------
* DRY RUN BY DEFAULT. It prints counts and a delta histogram only: no
  subject, no address, no body, no token. `--sample` prints row ids and the
  two timestamps so a wrong delta can be inspected.
* Idempotent: a second run reports 0 rows to fix.
* Only `mail_messages.date_received` is ever written. No row is deleted.
* Run AFTER migration 048 (which retypes the column to `timestamptz`).

USAGE (read-only until --apply; run on the CRM host, Ken approves the apply)
---------------------------------------------------------------------------
    ./.venv/bin/python3 scripts/backfill_mail_dates.py                  # dry run
    ./.venv/bin/python3 scripts/backfill_mail_dates.py --sample 10      # + examples
    ./.venv/bin/python3 scripts/backfill_mail_dates.py --json
    ./.venv/bin/python3 scripts/backfill_mail_dates.py --apply          # header-exact fixes
    ./.venv/bin/python3 scripts/backfill_mail_dates.py --apply-inferred # + inferred shift
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mail_dates  # noqa: E402

# A stored value within this many seconds of the header is considered correct
# (the header has 1-second resolution and some senders round).
TOLERANCE_SECONDS = 1


def extract_date_header(raw_headers: Any) -> str:
    """Return the raw `Date:` header value from a saved header block, or "".

    Handles both "Date: ..." lines and folded continuations, and is
    case-insensitive. Never returns more than the header value.
    """
    if not raw_headers:
        return ""
    text = str(raw_headers)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.lower().startswith("date:"):
            value = line.split(":", 1)[1].strip()
            # RFC 5322 folding: continuation lines start with whitespace.
            j = i + 1
            while j < len(lines) and lines[j][:1] in (" ", "\t"):
                value += " " + lines[j].strip()
                j += 1
            return value.strip()
    return ""


def classify_rows(rows: list[dict], tolerance: int = TOLERANCE_SECONDS) -> dict:
    """Pure classifier — no DB, no writes. Used by main() and the tests.

    Returns {
      total, ok, fixable_from_header, no_header,
      delta_histogram: {seconds: count},   # only non-zero deltas
      inferred_offset_seconds: int | None, # modal delta over fixable rows
      inferred_rows: int,
      samples: [ {id, stored, header, delta_seconds} ],
      fixes: [ {id, target} ],             # header-exact corrections
      inferred_fixes: [ {id, target} ],    # header-less rows + inferred shift
    }
    """
    ok = 0
    fixable: list[dict] = []
    no_header: list[dict] = []
    samples: list[dict] = []
    deltas: Counter[int] = Counter()

    for row in rows:
        stored = mail_dates.parse_email_date(row.get("date_received"))
        header_value = extract_date_header(row.get("raw_headers"))
        header_dt = mail_dates.parse_email_date(header_value) if header_value else None
        if header_dt is None:
            no_header.append({**row, "_stored": stored})
            continue
        if stored is None:
            # A row with a header but no stored value is simply filled in.
            fixable.append({**row, "_stored": stored, "_header": header_dt})
            continue
        delta = int(round((stored - header_dt).total_seconds()))
        if abs(delta) <= tolerance:
            ok += 1
            continue
        deltas[delta] += 1
        fixable.append({**row, "_stored": stored, "_header": header_dt, "_delta": delta})

    inferred_offset: int | None = None
    inferred_fixes: list[dict] = []
    if deltas:
        # The systematic (modal) error. Ties pick the largest absolute value,
        # then the smallest, so the choice is deterministic.
        inferred_offset = max(
            deltas.items(), key=lambda kv: (kv[1], abs(kv[0]), -kv[0]))[0]
        if inferred_offset and no_header:
            for row in no_header:
                stored = row.get("_stored")
                if stored is None:
                    continue
                inferred_fixes.append({
                    "id": row["id"],
                    "target": stored.timestamp() - inferred_offset,
                    "stored": mail_dates.iso_utc(stored),
                })

    fixes = [{
        "id": row["id"],
        "target": row["_header"].timestamp(),
        "stored": mail_dates.iso_utc(row.get("_stored")),
        "header": mail_dates.iso_utc(row["_header"]),
        "delta_seconds": row.get("_delta", 0),
    } for row in fixable]

    samples = sorted(
        fixes, key=lambda f: abs(f["delta_seconds"]), reverse=True)

    return {
        "total": len(rows),
        "ok": ok,
        "fixable_from_header": len(fixes),
        "no_header": len(no_header),
        "delta_histogram": {str(k): v for k, v in sorted(deltas.items())},
        "inferred_offset_seconds": inferred_offset,
        "inferred_rows": len(inferred_fixes),
        "fixes": fixes,
        "inferred_fixes": inferred_fixes,
        "samples": samples,
    }


def load_rows(account_ids: list[int] | None, limit: int | None) -> list[dict]:
    import db

    db.init_db()  # this tool writes, so the pool + migrations are wanted
    sql = "SELECT id, account_id, date_received, raw_headers FROM mail_messages"
    params: tuple = ()
    if account_ids:
        sql += " WHERE account_id = ANY(%s)"
        params = (list(account_ids),)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT %s"
        params = params + (int(limit),)
    return [dict(r) for r in (db.query_dicts(sql, params) if params else db.query_dicts(sql))]


def apply_fixes(fixes: list[dict]) -> int:
    """Write the corrected instants. Returns the number of rows changed."""
    import db

    changed = 0
    for fix in fixes:
        target = datetime.fromtimestamp(float(fix["target"]), tz=timezone.utc)
        changed += db.execute(
            "UPDATE mail_messages SET date_received = %s WHERE id = %s",
            (target, fix["id"]),
        )
    return changed


def render(report: dict, sample: int = 0) -> str:
    lines = ["MAIL DATE BACKFILL — DRY RUN (nothing written)"]
    lines.append(f"rows scanned:            {report['total']}")
    lines.append(f"already correct:         {report['ok']}")
    lines.append(f"fixable from Date header:{report['fixable_from_header']:>4}")
    lines.append(f"no Date header stored:   {report['no_header']}")
    if report["delta_histogram"]:
        lines.append("delta histogram (stored − header, seconds):")
        for delta, count in report["delta_histogram"].items():
            lines.append(f"  {delta:>8}s  ×{count}")
    if report["inferred_offset_seconds"] is not None:
        lines.append(
            f"inferred systematic offset: {report['inferred_offset_seconds']}s "
            f"({report['inferred_rows']} header-less row(s) would be shifted with --apply-inferred)")
    if sample:
        lines.append("")
        lines.append("largest deltas (id, stored, header):")
        for fix in report["samples"][:sample]:
            lines.append(
                f"  #{fix['id']}: stored {fix['stored'] or '(null)'}  header {fix['header']}  "
                f"delta {fix['delta_seconds']}s")
    lines.append("")
    lines.append("No subject/address/body is read or printed.")
    if report["fixable_from_header"]:
        lines.append("Apply with: --apply   (header-exact, idempotent)")
    else:
        lines.append("Nothing to fix from headers.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", action="append", type=int, default=None,
                        help="limit to an account id (repeatable)")
    parser.add_argument("--limit", type=int, default=None, help="scan at most N rows")
    parser.add_argument("--apply", action="store_true",
                        help="write the header-exact corrections")
    parser.add_argument("--apply-inferred", action="store_true",
                        help="ALSO shift header-less rows by the inferred offset")
    parser.add_argument("--sample", type=int, default=0,
                        help="print the N largest deltas (ids + timestamps only)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        rows = load_rows(args.account, args.limit)
    except Exception as e:
        print(f"ERROR: could not read mail_messages: {e}", file=sys.stderr)
        return 2

    report = classify_rows(rows)
    changed = 0
    inferred_changed = 0
    if args.apply and report["fixes"]:
        changed = apply_fixes(report["fixes"])
    if args.apply_inferred and report["inferred_fixes"]:
        inferred_changed = apply_fixes(report["inferred_fixes"])

    if args.json:
        payload = {k: v for k, v in report.items() if k not in ("fixes", "inferred_fixes")}
        payload["applied"] = changed
        payload["applied_inferred"] = inferred_changed
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(render(report, sample=args.sample))
    if args.apply:
        print(f"\napplied: {changed} row(s) corrected from their Date header")
    if args.apply_inferred:
        print(f"applied (inferred): {inferred_changed} row(s) shifted by the modal offset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
