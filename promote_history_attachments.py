#!/usr/bin/env python3
"""Promote history_attachments to project_documents so they appear in the
Documents tab and can be opened in the OnlyOffice editor.

For each history_attachments row with a file on disk:
  - Look up the linked opportunity_id via history_events
  - Insert into project_documents with the same file_path + filename + size
  - Use a NULL company_scope (treat as deal-scoped), uploaded_by from history
    event author if available

Idempotent: skip rows whose (opportunity_id, file_path) already exists in
project_documents.

Run once after the live sync. Safe to re-run.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    p.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    p.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    p.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    p.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    conn = psycopg2.connect(host=args.db_host, port=args.db_port, dbname=args.db_name,
                            user=args.db_user, password=args.db_password)
    conn.autocommit = False
    cur = conn.cursor()

    # All history attachments with their event's opportunity
    cur.execute("""
        SELECT ha.id, ha.event_id, ha.filename, ha.file_path, ha.file_size,
               ha.mime_type, ha.uploaded_by, ha.crm_file_id, he.opportunity_id
        FROM history_attachments ha
        JOIN history_events he ON ha.event_id = he.id
        ORDER BY ha.id
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} history_attachments rows")

    # Skip if no file on disk
    project_root = Path(__file__).resolve().parent
    skipped_no_file = 0
    skipped_dup = 0
    inserted = 0
    for r in rows:
        ha_id, event_id, filename, file_path, file_size, mime_type, uploaded_by, crm_fid, opp_id = r
        if not filename or not file_path:
            skipped_no_file += 1
            continue
        full = project_root / file_path
        if not full.is_file():
            skipped_no_file += 1
            continue
        # Check if a project_documents row already exists for this (opp, file_path)
        cur.execute(
            "SELECT 1 FROM project_documents WHERE opportunity_id = %s AND file_path = %s",
            (opp_id, file_path),
        )
        if cur.fetchone():
            skipped_dup += 1
            continue
        # Look up event author (creator) for uploaded_by if missing
        author_id = uploaded_by
        if not author_id:
            cur.execute("SELECT created_by FROM history_events WHERE id = %s", (event_id,))
            row = cur.fetchone()
            if row and row[0]:
                author_id = row[0]
        if args.dry_run:
            print(f"  [dry-run] would insert: ha_id={ha_id} opp_id={opp_id} filename={filename!r}")
            inserted += 1
        else:
            try:
                cur.execute(
                    """INSERT INTO project_documents
                           (opportunity_id, title, file_path, file_size, mime_type,
                            uploaded_by, uploaded_at, company_scope, folder_id)
                       VALUES (%s, %s, %s, %s, %s, %s, NOW(), FALSE, NULL)""",
                    (opp_id, filename, file_path, file_size, mime_type, author_id),
                )
                inserted += 1
            except Exception as e:
                print(f"  [warn] ha_id={ha_id}: {e}")
        if inserted % 100 == 0 and not args.dry_run:
            conn.commit()
            print(f"  progress: {inserted} inserted")

    if not args.dry_run:
        conn.commit()
    print()
    print(f"Inserted: {inserted}")
    print(f"Skipped (no file on disk or missing fields): {skipped_no_file}")
    print(f"Skipped (already exists in project_documents): {skipped_dup}")
    conn.close()


if __name__ == "__main__":
    main()