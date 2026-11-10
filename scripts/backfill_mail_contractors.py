#!/usr/bin/env python3
"""Propose contractor-Contact mappings for mail accounts (dry-run by default).

Ken's model: a job's Contact is the contractor; a mailbox mapped to that
Contact is the one that may send for the contractor's jobs. This script
proposes mappings from the account's display name / from-name / address and
prints them for review. It never writes unless --apply is passed.

    python3 scripts/backfill_mail_contractors.py            # dry run
    python3 scripts/backfill_mail_contractors.py --json
    python3 scripts/backfill_mail_contractors.py --apply    # sets the mapping
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

# Tokens we look for in a Contact's company/name. Keep them lowercase.
KEYWORDS = {
    "aplus.estimates@outlook.com": ["a plus", "aplus", "a+"],
    "mdm.estimates@outlook.com": ["mdm"],
    "estimates.masterx@outlook.com": ["masterx", "master x"],
    "requests@sherwoodestimates.com": ["sherwood"],
    "crm@vanguardadj.online": ["vanguard"],
}


def _account_text(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in
                    ("email", "display_name", "smtp_from_name")).lower()


def propose() -> list[dict]:
    accounts = db.query_dicts(
        "SELECT id, email, display_name, smtp_from_name, contractor_contact_id "
        "FROM mail_accounts ORDER BY id")
    contacts = db.query_dicts(
        """SELECT id, company, first_name, last_name, email, is_company
             FROM contacts""")
    out = []
    for acct in accounts:
        keys = KEYWORDS.get((acct.get("email") or "").lower(), [])
        if not keys:
            # Fall back to the first significant word of the display name.
            words = [w for w in _account_text(acct).replace("@", " ").split() if len(w) >= 4]
            keys = words[:2]
        matches = []
        for c in contacts:
            text = " ".join(str(c.get(k) or "") for k in
                            ("company", "first_name", "last_name", "email")).lower()
            if any(k and k in text for k in keys):
                matches.append({
                    "id": c["id"],
                    "name": (c.get("company") or f"{c.get('first_name') or ''} {c.get('last_name') or ''}").strip(),
                })
        matches.sort(key=lambda m: len(m["name"] or ""))
        out.append({
            "accountId": acct["id"],
            "email": acct.get("email"),
            "displayName": acct.get("display_name") or "",
            "fromName": acct.get("smtp_from_name") or "",
            "currentlyMapped": acct.get("contractor_contact_id"),
            "proposed": matches[:3],
            "unmappedNote": ("" if matches else
                             "no matching Contact — likely unbranded/internal; leave unmapped"),
        })
    return out


def apply(proposals: list[dict]) -> int:
    changed = 0
    for p in proposals:
        if p["currentlyMapped"] or not p["proposed"]:
            continue
        target = p["proposed"][0]
        changed += db.execute(
            "UPDATE mail_accounts SET contractor_contact_id = %s WHERE id = %s",
            (target["id"], p["accountId"]))
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the proposed mappings")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db.init_db()
    proposals = propose()
    if args.json:
        print(json.dumps(proposals, indent=2, default=str))
    else:
        for p in proposals:
            cur = f" (currently #{p['currentlyMapped']})" if p["currentlyMapped"] else ""
            print(f"account {p['accountId']} {p['email']}{cur}")
            if p["proposed"]:
                for m in p["proposed"]:
                    print(f"   -> contact #{m['id']} {m['name']}")
            else:
                print(f"   -> {p['unmappedNote']}")
    if args.apply:
        print(f"\napplied: {apply(proposals)} account(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
