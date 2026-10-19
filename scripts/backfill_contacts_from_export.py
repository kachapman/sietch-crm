#!/usr/bin/env python3
"""Backfill opportunities.contact_id from CRM_export/opportunities.json.

The 2026-07-20 sync ingestion dropped contact_id entirely, leaving every
opportunity with NULL contact_id. The data needed to fix this is still on
disk in /opt/sietch-crm/CRM_export/, which contains the OnlyOffice-exported
opportunity objects with their `contact.id` field intact.

This script:
  1. Loads CRM_export/opportunities.json (1,191 deals)
  2. Loads CRM_export/contacts.json (16 contacts)
  3. Builds a map: export_contact.id -> contacts.onlyoffice_id (local)
  4. Builds a map: export_opportunity.id -> opportunities.onlyoffice_id (local)
  5. For each opportunity with NULL contact_id:
     - Finds the export opportunity
     - Finds the export contact.id reference
     - Looks up local contacts.onlyoffice_id
     - UPDATEs opportunities.contact_id

DRY-RUN BY DEFAULT. Pass --write to actually mutate the DB.

Usage:
    python3 scripts/backfill_contacts_from_export.py                      # dry-run
    python3 scripts/backfill_contacts_from_export.py --write              # commit
    python3 scripts/backfill_contacts_from_export.py --source /path/to    # custom export path
"""
import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "CRM_export"


def load_export(source_dir: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (opps_by_export_id, contacts_by_export_id)."""
    opps_file = source_dir / "opportunities.json"
    contacts_file = source_dir / "contacts.json"
    if not opps_file.exists() or not contacts_file.exists():
        sys.exit(f"missing export files in {source_dir} (expected opportunities.json + contacts.json)")
    opps_list = json.loads(opps_file.read_text(encoding="utf-8"))
    contacts_list = json.loads(contacts_file.read_text(encoding="utf-8"))
    opps = {str(o.get("id")): o for o in opps_list if isinstance(o, dict) and o.get("id") is not None}
    contacts = {str(c.get("id")): c for c in contacts_list if isinstance(c, dict) and c.get("id") is not None}
    return opps, contacts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help="path to CRM_export/ directory")
    ap.add_argument("--write", action="store_true",
                    help="commit updates (default is dry-run)")
    ap.add_argument("--host", default=os.environ.get("DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=int(os.environ.get("DB_PORT", "5432")))
    ap.add_argument("--name", default=os.environ.get("DB_NAME", "sietch_crm"))
    ap.add_argument("--user", default=os.environ.get("DB_USER", "sietch"))
    ap.add_argument("--password", default=os.environ.get("DB_PASSWORD", ""))
    args = ap.parse_args()

    print(f"[backfill] loading export from {args.source} (write={args.write})")
    opps_by_id, contacts_by_id = load_export(args.source)
    print(f"[backfill]   {len(opps_by_id)} opportunities, {len(contacts_by_id)} contacts in export")

    conn = psycopg2.connect(
        host=args.host, port=args.port, dbname=args.name,
        user=args.user, password=args.password,
    )
    conn.autocommit = False
    cur = conn.cursor()

    # Build local contacts map: onlyoffice_id -> id
    cur.execute("SELECT id, onlyoffice_id FROM contacts WHERE onlyoffice_id IS NOT NULL")
    contact_local = {str(row[1]): row[0] for row in cur.fetchall()}
    print(f"[backfill]   {len(contact_local)} local contacts (mapped by onlyoffice_id)")

    # Find opps with NULL contact_id that have an onlyoffice_id matching an export opp
    cur.execute(
        """
        SELECT o.id, o.title, o.onlyoffice_id
        FROM opportunities o
        WHERE o.contact_id IS NULL AND o.onlyoffice_id IS NOT NULL
        """
    )
    candidates = cur.fetchall()
    print(f"[backfill] {len(candidates)} opportunities have NULL contact_id and an onlyoffice_id")

    would_link = 0
    would_skip_no_export = 0
    would_skip_no_contact_ref = 0
    would_skip_no_local_contact = 0
    already_linked = 0

    updates: list[tuple[int, int]] = []
    for opp_id, title, onlyoffice_id in candidates:
        export_opp = opps_by_id.get(str(onlyoffice_id))
        if not export_opp:
            would_skip_no_export += 1
            continue
        contact_block = export_opp.get("contact") or {}
        if not isinstance(contact_block, dict):
            would_skip_no_contact_ref += 1
            continue
        export_contact_id = str(contact_block.get("id") or contact_block.get("ID") or "")
        if not export_contact_id or export_contact_id == "0":
            would_skip_no_contact_ref += 1
            continue
        local_contact_id = contact_local.get(export_contact_id)
        if not local_contact_id:
            would_skip_no_local_contact += 1
            continue
        updates.append((local_contact_id, opp_id))
        would_link += 1

    # Final integrity check: every contact_id in updates must still exist right now.
    # (Defends against the FK violation case where a target contact_id was deleted
    # between the initial scan and the write — e.g. by a parallel cleanup job.)
    if updates:
        target_ids = list({cid for cid, _ in updates})
        cur.execute("SELECT id FROM contacts WHERE id = ANY(%s)", (target_ids,))
        existing_ids = {row[0] for row in cur.fetchall()}
        before = len(updates)
        updates = [(cid, oid) for cid, oid in updates if cid in existing_ids]
        dropped = before - len(updates)
        if dropped:
            print(f"[backfill] integrity: dropped {dropped} updates whose target contact_id no longer exists")
            would_link -= dropped

    print()
    print("=== would do ===")
    print(f"  link contact_id: {would_link}")
    print(f"  skip (no export opp): {would_skip_no_export}")
    print(f"  skip (no contact ref in export): {would_skip_no_contact_ref}")
    print(f"  skip (contact ref not in local DB): {would_skip_no_local_contact}")
    print()

    # Show the first 10 planned updates for review
    if updates:
        print("=== sample planned updates (first 10) ===")
        cur.execute(
            "SELECT o.id, o.title, c.id, c.first_name, c.last_name, c.company "
            "FROM opportunities o JOIN contacts c ON c.id = ANY(%s) "
            "ORDER BY o.id LIMIT 10",
            ([uid for _, uid in updates],),
        )
        # Re-run a cleaner preview by direct lookup
        for local_cid, opp_id in updates[:10]:
            cur.execute(
                "SELECT o.title, c.first_name, c.last_name, c.company "
                "FROM opportunities o, contacts c "
                "WHERE o.id=%s AND c.id=%s",
                (opp_id, local_cid),
            )
            r = cur.fetchone()
            if r:
                print(f"  opp_id={opp_id:5} '{r[0][:40]:<40}' <- contact_id={local_cid} "
                      f"({(r[1] or '').strip()} {(r[2] or '').strip()} / {r[3] or ''})")

    if not args.write:
        print()
        print("=== dry-run only (no DB writes). pass --write to commit. ===")
        return

    if updates:
        from psycopg2.extras import execute_values
        # execute_values expects tuples in the column order declared in the
        # template: `AS data(opp_id, cid)`. We built updates as (cid, opp_id)
        # earlier — swap to (opp_id, cid) for the SQL template.
        # Defensive: verify every target contact_id still exists right now.
        ordered = [(oid, cid) for cid, oid in updates]
        all_cids = list({cid for _, cid in ordered})
        cur.execute("SELECT id FROM contacts WHERE id = ANY(%s)", (all_cids,))
        valid = {row[0] for row in cur.fetchall()}
        before = len(ordered)
        ordered = [(oid, cid) for oid, cid in ordered if cid in valid]
        dropped = before - len(ordered)
        if dropped:
            print(f"[backfill] dropped {dropped} updates with bad targets (sanity)")
        execute_values(
            cur,
            "UPDATE opportunities SET contact_id = data.cid "
            "FROM (VALUES %s) AS data(opp_id, cid) "
            "WHERE opportunities.id = data.opp_id",
            ordered,
        )
        conn.commit()
        print(f"[backfill] committed {len(ordered)} updates")

    # Verify
    cur.execute(
        "SELECT count(*) FILTER (WHERE contact_id IS NOT NULL), count(*) FROM opportunities"
    )
    with_c, total = cur.fetchone()
    print(f"[backfill] post-update: {with_c}/{total} opportunities have contact_id")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()