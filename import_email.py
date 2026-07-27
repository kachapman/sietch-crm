#!/usr/bin/env python3
"""Import email data from OnlyOffice CRM into Sietch CRM.

This script pulls email data from the old OnlyOffice CRM database
and inserts it into the new Sietch CRM mail tables.

Designed for Phase 4 (cutover phase) but schema-compatible with
Phase 3 — no changes needed when migration time comes.

Usage:
    python import_email.py --source-db onlyoffice_crm --target-db sietch_crm

Requires:
    - psycopg2 (pip install psycopg2-binary)
    - Access to both old and new PostgreSQL databases
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_email")


def connect_db(host: str, port: int, dbname: str, user: str, password: str):
    """Connect to a PostgreSQL database."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=port, dbname=dbname,
            user=user, password=password,
        )
        return conn
    except ImportError:
        logger.error("psycopg2 not installed. Run: pip install psycopg2-binary")
        sys.exit(1)
    except Exception as e:
        logger.error("Failed to connect to %s@%s:%s/%s: %s", user, host, port, dbname, e)
        sys.exit(1)


def import_mail_accounts(source_conn, target_conn) -> int:
    """Import mail accounts from source to target database."""
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()

    src_cur.execute("""
        SELECT id, email, imap_host, imap_port, password_encrypted,
               sync_enabled, sync_interval_seconds, last_sync, last_uid, owner_user_id
        FROM mail_accounts
    """)
    accounts = src_cur.fetchall()
    count = 0
    for row in accounts:
        try:
            tgt_cur.execute("""
                INSERT INTO mail_accounts
                (email, imap_host, imap_port, password_encrypted, sync_enabled,
                 sync_interval_seconds, last_sync, last_uid, owner_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                    imap_host = EXCLUDED.imap_host,
                    imap_port = EXCLUDED.imap_port,
                    password_encrypted = EXCLUDED.password_encrypted,
                    sync_enabled = EXCLUDED.sync_enabled
                RETURNING id
            """, (row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]))
            count += 1
        except Exception as e:
            logger.warning("Failed to import account %s: %s", row[1], e)
    target_conn.commit()
    return count


def import_mail_messages(source_conn, target_conn) -> int:
    """Import mail messages from source to target database."""
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()

    src_cur.execute("""
        SELECT account_id, imap_uid, message_id, in_reply_to, from_addr, to_addr,
               cc_addr, subject, body_text, body_html, date_received, is_read,
               is_flagged, is_archived, folder, conversation_id
        FROM mail_messages
        ORDER BY date_received ASC
    """)
    messages = src_cur.fetchall()
    count = 0
    for row in messages:
        try:
            tgt_cur.execute("""
                INSERT INTO mail_messages
                (account_id, imap_uid, message_id, from_addr, to_addr, cc_addr,
                 subject, body_text, body_html, date_received, is_read,
                 is_flagged, is_archived, folder)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, imap_uid, folder) DO NOTHING
            """, (
                row[0], row[1], row[2], row[4], row[5], row[6],
                row[7], row[8], row[9], row[10], row[11],
                row[12], row[13], row[14],
            ))
            count += 1
        except Exception as e:
            logger.warning("Failed to import message uid=%s: %s", row[1], e)
    target_conn.commit()
    return count


def import_mail_deal_links(source_conn, target_conn) -> int:
    """Import mail-deal links from source to target database."""
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()

    src_cur.execute("""
        SELECT message_id, opportunity_id, linked_by_user_id, linked_at
        FROM mail_deal_links
    """)
    links = src_cur.fetchall()
    count = 0
    for row in links:
        try:
            tgt_cur.execute("""
                INSERT INTO mail_deal_links (message_id, opportunity_id, linked_by_user_id, linked_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (message_id, opportunity_id) DO NOTHING
            """, (row[0], row[1], row[2], row[3]))
            count += 1
        except Exception as e:
            logger.warning("Failed to import link msg=%d opp=%d: %s", row[0], row[1], e)
    target_conn.commit()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Import email data from OnlyOffice CRM")
    parser.add_argument("--source-host", default=os.getenv("SOURCE_DB_HOST", "localhost"))
    parser.add_argument("--source-port", type=int, default=int(os.getenv("SOURCE_DB_PORT", "5432")))
    parser.add_argument("--source-db", default=os.getenv("SOURCE_DB_NAME", "onlyoffice_crm"))
    parser.add_argument("--source-user", default=os.getenv("SOURCE_DB_USER", "onlyoffice"))
    parser.add_argument("--source-password", default=os.getenv("SOURCE_DB_PASSWORD", ""))
    parser.add_argument("--target-host", default=os.getenv("DB_HOST", "db"))
    parser.add_argument("--target-port", type=int, default=int(os.getenv("DB_PORT", "5432")))
    parser.add_argument("--target-db", default=os.getenv("DB_NAME", "sietch_crm"))
    parser.add_argument("--target-user", default=os.getenv("DB_USER", "sietch"))
    parser.add_argument("--target-password", default=os.getenv("DB_PASSWORD", ""))
    args = parser.parse_args()

    logger.info("Connecting to source: %s@%s:%s/%s", args.source_user, args.source_host, args.source_port, args.source_db)
    source = connect_db(args.source_host, args.source_port, args.source_db, args.source_user, args.source_password)

    logger.info("Connecting to target: %s@%s:%s/%s", args.target_user, args.target_host, args.target_port, args.target_db)
    target = connect_db(args.target_host, args.target_port, args.target_db, args.target_user, args.target_password)

    logger.info("Importing mail accounts...")
    accounts = import_mail_accounts(source, target)
    logger.info("Imported %d mail accounts", accounts)

    logger.info("Importing mail messages...")
    messages = import_mail_messages(source, target)
    logger.info("Imported %d mail messages", messages)

    logger.info("Importing mail-deal links...")
    links = import_mail_deal_links(source, target)
    logger.info("Imported %d mail-deal links", links)

    source.close()
    target.close()
    logger.info("Email import complete: %d accounts, %d messages, %d links", accounts, messages, links)


if __name__ == "__main__":
    main()