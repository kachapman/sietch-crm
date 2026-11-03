#!/usr/bin/env python3
"""One-time backfill: put linked emails' files into their project's Documents tab.

Bug fix 2026-11-03. Linking an email to a project is supposed to COPY the
email's attachments (PDFs, DOCX, ...) into the project's own storage so they
show up in the project's Documents tab and survive the mail account going
away. That copy silently dropped files:

  * the scanner stored attachment METADATA only (no bytes on disk), so the
    copy needed a live mailbox download;
  * any failure of that download returned None and the file was skipped
    SILENTLY, while the UI reported success;
  * relinking did not help — the snapshot helper returned early when the
    timeline entry already existed.

This script finds linked emails whose files never made it into the project
and copies them.

    (a) link exists + timeline entry exists -> copy the missing files only
    (b) link exists + NO timeline entry     -> create the entry (through the
        same shared helper the server uses) and copy the files

DRY RUN IS THE DEFAULT. A real run needs --apply.

    # dry run (writes nothing)
    python3 backfill_mail_attachments.py --db-host 127.0.0.1 --db-name sietch_crm

    # real run
    python3 backfill_mail_attachments.py --db-host 127.0.0.1 --db-name sietch_crm --apply

    # narrow the scope
    python3 backfill_mail_attachments.py --project 75 --apply
    python3 backfill_mail_attachments.py --account 10 --since-days 30 --apply
    python3 backfill_mail_attachments.py --limit 20 --apply

Idempotent: the copy is keyed on (timeline event, mail attachment), so a
second run adds nothing.

NEVER run this against production without Ken reviewing the dry-run report
first. See docs/bugs/2026-11-03-mail-link-attachments.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    p.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    p.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    p.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    p.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    p.add_argument("--apply", action="store_true",
                   help="Actually copy the files. Without this flag the script "
                        "only reports what it WOULD do (dry run).")
    p.add_argument("--project", type=int, default=0, help="Limit to one opportunity id")
    p.add_argument("--account", type=int, default=0, help="Limit to one mail account id")
    p.add_argument("--since-days", type=int, default=0,
                   help="Only mail received in the last N days")
    p.add_argument("--limit", type=int, default=0, help="Cap the number of emails processed")
    p.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = p.parse_args()

    # db.py reads the DB_* env vars; set them from the CLI args so the
    # scanner helpers (which go through db.py) hit the right database.
    os.environ["DB_HOST"] = args.db_host
    os.environ["DB_PORT"] = str(args.db_port)
    os.environ["DB_NAME"] = args.db_name
    os.environ["DB_USER"] = args.db_user
    os.environ["DB_PASSWORD"] = args.db_password

    import db
    db.init_db()

    import scanner.mail_scanner as ms

    dry_run = not args.apply
    if dry_run:
        print("DRY RUN — reporting only, zero writes. Use --apply to actually copy files.\n")

    where = []
    params: list = []
    if args.project:
        where.append("l.opportunity_id = %s")
        params.append(args.project)
    if args.account:
        where.append("m.account_id = %s")
        params.append(args.account)
    if args.since_days > 0:
        where.append("m.date_received >= NOW() - (%s * INTERVAL '1 day')")
        params.append(args.since_days)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    rows = db.query_dicts(
        """SELECT l.message_id, l.opportunity_id, m.subject, m.from_addr,
                  m.date_received, m.account_id, o.title AS opp_title,
                  (SELECT he.id FROM history_events he
                    WHERE he.opportunity_id = l.opportunity_id
                      AND he.content ~ ('"message_id": ' || l.message_id || '([,}])')
                    ORDER BY he.id LIMIT 1) AS event_id,
                  (SELECT COUNT(*) FROM mail_attachments ma
                    WHERE ma.message_id = l.message_id) AS att_count,
                  (SELECT COUNT(*)
                     FROM mail_attachments ma
                     JOIN history_attachments ha
                       ON ha.event_id = (SELECT he2.id FROM history_events he2
                           WHERE he2.opportunity_id = l.opportunity_id
                             AND he2.content ~ ('"message_id": ' || l.message_id || '([,}])')
                           ORDER BY he2.id LIMIT 1)
                      AND (ha.mail_attachment_id = ma.id
                           OR (ha.filename = ma.filename
                               AND ha.file_size = ma.size_bytes))
                    WHERE ma.message_id = l.message_id) AS copied_count
             FROM mail_deal_links l
             JOIN mail_messages m ON m.id = l.message_id
             JOIN opportunities o ON o.id = l.opportunity_id"""
        + where_sql +
        " ORDER BY l.message_id",
        tuple(params),
    )

    need_entry: list[dict] = []
    need_files: list[dict] = []
    for r in rows:
        if int(r.get("att_count") or 0) <= 0:
            continue
        # A LEGACY copy (no mail_attachment_id) counts as copied when the
        # filename + size match — otherwise the dry run would report every
        # old email as missing files and the real run would duplicate them.
        if int(r.get("copied_count") or 0) >= int(r.get("att_count") or 0):
            continue
        if not r.get("event_id"):
            need_entry.append(r)
        else:
            need_files.append(r)

    work = need_entry + need_files
    if args.limit > 0:
        work = work[:args.limit]
        need_entry = [r for r in need_entry if r in work]
        need_files = [r for r in need_files if r in work]

    already_complete = sum(
        1 for r in rows
        if int(r.get("att_count") or 0) > 0
        and int(r.get("copied_count") or 0) >= int(r.get("att_count") or 0)
    )
    print(f"Linked emails scanned : {len(rows)}")
    print(f"Already complete      : {already_complete}")
    print(f"Missing timeline entry: {len(need_entry)}")
    print(f"Missing project files : {len(need_files)}")
    print(f"Emails to process     : {len(work)}\n")

    items: list[dict] = []
    failures: list[dict] = []
    created_entries = 0
    copied_files = 0
    total_files = 0

    for r in work:
        mid = int(r["message_id"])
        opp = int(r["opportunity_id"])
        att_count = int(r.get("att_count") or 0)
        copied_count = int(r.get("copied_count") or 0)
        item = {
            "message_id": mid,
            "opportunity_id": opp,
            "project": r.get("opp_title") or "",
            "subject": r.get("subject") or "",
            "from": r.get("from_addr") or "",
            "date": str(r.get("date_received") or ""),
            "action": "create_entry_and_copy" if not r.get("event_id") else "copy_files",
            "copied": 0,
            "total": att_count,
            "already_copied": copied_count,
            "skipped": [],
        }
        if dry_run:
            missing = max(0, att_count - copied_count)
            item["would_add"] = (
                f"timeline entry + {att_count} file(s)"
                if item["action"] == "create_entry_and_copy"
                else f"{missing} file(s)")
            items.append(item)
            print(f"[DRY] {mid} -> project {opp} ({item['project']}): {item['would_add']}"
                  f"  | {item['subject'][:60]}")
            continue

        if item["action"] == "create_entry_and_copy":
            report = ms.create_email_history_snapshot_detailed(opp, mid)
            if report.get("history_created"):
                created_entries += 1
        else:
            report = ms.copy_mail_attachments_to_project_detailed(
                opp, int(r["event_id"]), mid)
        item["copied"] = int(report.get("copied") or 0)
        item["total"] = int(report.get("total") or 0)
        item["skipped"] = report.get("skipped") or []
        copied_files += item["copied"]
        total_files += item["total"]
        for s in item["skipped"]:
            failures.append({
                "message_id": mid, "opportunity_id": opp,
                "attachment_id": s.get("attachment_id"),
                "filename": s.get("filename"), "reason": s.get("reason"),
            })
        items.append(item)
        print(f"[{'OK ' if item['copied'] else '-- '}] {mid} -> project {opp} "
              f"({item['project']}): copied {item['copied']}/{item['total']}"
              f"  | {item['subject'][:60]}")
        for s in item["skipped"]:
            print(f"        SKIPPED {s.get('filename')}: {s.get('reason')}")

    summary = {
        "dry_run": dry_run,
        "apply": not dry_run,
        "scanned": len(rows),
        "missing_entry": len(need_entry),
        "missing_files": len(need_files),
        "processed": len(items),
        "created_entries": created_entries,
        "copied_files": copied_files,
        "total_files": total_files,
        "failures": failures,
        "items": items,
    }

    print()
    print(f"{'DRY RUN' if dry_run else 'APPLIED'}: "
          f"{created_entries} timeline entries created, "
          f"{copied_files} file(s) copied, {len(failures)} failure(s)")

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        out = Path("backfill-mail-attachments-report.json")
        out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
