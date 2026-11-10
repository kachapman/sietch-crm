#!/usr/bin/env python3
"""Find (and optionally remove) duplicate mail rows. DRY RUN BY DEFAULT.

Two duplicate classes exist today:

1. Same (account_id, message_id): a UIDVALIDITY change renumbers UIDs and the
   old code re-imported the message. Keep the row with the most links, else the
   lowest id.
2. Sent copies: the CRM stores a copy with imap_uid `sent:outgoing:<id>` when it
   sends, and the IMAP Sent folder sync stores the same message again. Keep the
   CRM copy (it carries the deal link + stored attachments) and remove the IMAP
   duplicate.

    python3 scripts/dedupe_mail_messages.py            # report only
    python3 scripts/dedupe_mail_messages.py --apply    # delete duplicates
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def find_message_id_dupes() -> list[dict]:
    return db.query_dicts(
        """SELECT account_id, message_id, array_agg(id ORDER BY id) AS ids, count(*) AS n
             FROM mail_messages
            WHERE message_id IS NOT NULL AND message_id <> ''
            GROUP BY account_id, message_id
           HAVING count(*) > 1
            ORDER BY account_id, message_id""")


def find_sent_dupes() -> list[dict]:
    return db.query_dicts(
        """SELECT m.account_id,
                  lower(coalesce(m.subject, '')) AS subject,
                  lower(coalesce(m.to_addr, '')) AS to_addr,
                  count(*) FILTER (WHERE m.imap_uid LIKE 'sent:outgoing:%') AS crm_copies,
                  array_agg(m.id ORDER BY m.id) AS ids
             FROM mail_messages m
            WHERE m.folder = 'Sent'
            GROUP BY 1, 2, 3
           HAVING count(*) FILTER (WHERE m.imap_uid LIKE 'sent:outgoing:%') > 0
              AND count(*) FILTER (WHERE m.imap_uid NOT LIKE 'sent:outgoing:%') > 0
            ORDER BY 1, 2""")


def _links(message_id: int) -> int:
    row = db.query_one(
        "SELECT count(*) AS n FROM mail_deal_links WHERE message_id = %s", (message_id,))
    return int((row or {}).get("n") or 0)


def plan() -> dict:
    mid = []
    for d in find_message_id_dupes():
        ids = [int(i) for i in d["ids"]]
        keep = max(ids, key=lambda i: (_links(i), -i))
        mid.append({"accountId": d["account_id"], "messageId": d["message_id"],
                    "keep": keep, "delete": [i for i in ids if i != keep]})
    sent = []
    for d in find_sent_dupes():
        ids = [int(i) for i in d["ids"]]
        keep = None
        for i in ids:
            row = db.query_one("SELECT imap_uid FROM mail_messages WHERE id = %s", (i,))
            if row and str(row.get("imap_uid") or "").startswith("sent:outgoing:"):
                keep = i
                break
        if keep is None:
            keep = ids[0]
        sent.append({"accountId": d["account_id"], "keep": keep,
                     "delete": [i for i in ids if i != keep]})
    return {"messageIdGroups": mid, "sentGroups": sent,
            "messageIdDeletes": sum(len(g["delete"]) for g in mid),
            "sentDeletes": sum(len(g["delete"]) for g in sent)}


def apply(plan_data: dict) -> int:
    deleted = 0
    for group in plan_data["messageIdGroups"] + plan_data["sentGroups"]:
        for mid in group["delete"]:
            try:
                db.execute("DELETE FROM mail_deal_links WHERE message_id = %s", (mid,))
                db.execute("DELETE FROM mail_attachments WHERE message_id = %s", (mid,))
                deleted += db.execute("DELETE FROM mail_messages WHERE id = %s", (mid,))
            except Exception as exc:
                print(f"  could not delete {mid}: {exc}", file=sys.stderr)
    return deleted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db.init_db()
    data = plan()
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print("MAIL DEDUPE — " + ("APPLY" if args.apply else "DRY RUN (nothing deleted)"))
        print(f"message_id duplicate groups: {len(data['messageIdGroups'])} "
              f"(would delete {data['messageIdDeletes']} rows)")
        print(f"CRM+IMAP sent duplicate groups: {len(data['sentGroups'])} "
              f"(would delete {data['sentDeletes']} rows)")
        for g in (data["messageIdGroups"] + data["sentGroups"])[:20]:
            print(f"  account {g['accountId']}: keep {g['keep']}, delete {g['delete']}")
    if args.apply:
        print(f"deleted: {apply(data)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
