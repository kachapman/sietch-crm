#!/usr/bin/env python3
"""Backfill contact multi-value tables (contact_emails/phones/addresses)
from a contacts.json file (typically produced by the live export).

Idempotent — uses INSERT ... ON CONFLICT (onlyoffice_id) DO NOTHING.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True, help="Path to contacts.json")
    p.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    p.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    p.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    p.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    p.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    args = p.parse_args()

    contacts = json.load(open(args.source))
    if isinstance(contacts, dict) and "response" in contacts:
        contacts = contacts["response"]
    print(f"Loaded {len(contacts)} contacts from {args.source}")

    conn = psycopg2.connect(host=args.db_host, port=args.db_port, dbname=args.db_name,
                            user=args.db_user, password=args.db_password)
    cur = conn.cursor()

    # Build oo_id → local id map
    cur.execute("SELECT id, onlyoffice_id FROM contacts WHERE onlyoffice_id IS NOT NULL")
    cmap = {str(oo): int(i) for i, oo in cur.fetchall()}
    print(f"DB has {len(cmap)} contacts with onlyoffice_id")

    e_count = 0
    p_count = 0
    a_count = 0
    for c in contacts:
        if not isinstance(c, dict):
            continue
        oo_id = str(c.get("id") or "")
        local_id = cmap.get(oo_id)
        if not local_id:
            continue

        for it in (c.get("commonData") or []):
            if not isinstance(it, dict):
                continue
            data = str(it.get("data") or "").strip()
            if not data:
                continue
            info_type = it.get("infoType")
            is_primary = bool(it.get("isPrimary"))
            cat_id = it.get("category")
            cat_name = str(it.get("categoryName") or "")
            oo_sub_id = int(it.get("id") or 0) or None
            if info_type == 1:  # email
                cur.execute(
                    """INSERT INTO contact_emails (contact_id, email, category, category_id, is_primary, onlyoffice_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING""",
                    (local_id, data, cat_name or None, cat_id, is_primary, oo_sub_id),
                )
                e_count += 1
            elif info_type == 0:  # phone
                cur.execute(
                    """INSERT INTO contact_phones (contact_id, phone, category, category_id, is_primary, onlyoffice_id)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING""",
                    (local_id, data, cat_name or None, cat_id, is_primary, oo_sub_id),
                )
                p_count += 1

        for a in (c.get("addresses") or []):
            if not isinstance(a, dict):
                continue
            cur.execute(
                """INSERT INTO contact_addresses
                       (contact_id, street, city, state, zip, country, category, category_id, is_primary, onlyoffice_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING""",
                (local_id,
                 str(a.get("street") or "").strip() or None,
                 str(a.get("city") or "").strip() or None,
                 str(a.get("state") or "").strip() or None,
                 str(a.get("zip") or "").strip() or None,
                 str(a.get("country") or "").strip() or None,
                 str(a.get("categoryName") or "") or None,
                 a.get("category"),
                 bool(a.get("isPrimary")),
                 a.get("id")),
            )
            a_count += 1

    conn.commit()
    print(f"Backfilled: {e_count} emails, {p_count} phones, {a_count} addresses")
    conn.close()


if __name__ == "__main__":
    main()