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
    """Import mail accounts from source to target database.

    Credentials are NEVER rewritten by email (2026-11-10). The old
    `ON CONFLICT (email) DO UPDATE SET password_encrypted = EXCLUDED...`
    meant importing the legacy dump could replace the password of a live
    account row (owned by someone else) with the old CRM's copy — a
    credential write keyed by email instead of by account id.

    Existing mailboxes are skipped and reported; a row is only created when
    that mailbox does not exist yet.
    """
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()

    src_cur.execute("""
        SELECT id, email, imap_host, imap_port, password_encrypted,
               sync_enabled, sync_interval_seconds, last_sync, last_uid, owner_user_id
        FROM mail_accounts
    """)
    accounts = src_cur.fetchall()
    count = 0
    skipped = 0
    for row in accounts:
        try:
            tgt_cur.execute(
                "SELECT id FROM mail_accounts WHERE lower(email) = lower(%s) LIMIT 1",
                (row[1],),
            )
            existing = tgt_cur.fetchone()
            if existing:
                skipped += 1
                logger.warning(
                    "Skipping account %s: already connected as mail account %s "
                    "(credentials are never overwritten by email)",
                    row[1], existing[0])
                continue
            tgt_cur.execute("""
                INSERT INTO mail_accounts
                (email, imap_host, imap_port, password_encrypted, sync_enabled,
                 sync_interval_seconds, last_sync, last_uid, owner_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]))
            count += 1
        except Exception as e:
            logger.warning("Failed to import account %s: %s", row[1], e)
    target_conn.commit()
    if skipped:
        logger.info("Skipped %d existing mail account(s); no credentials were changed", skipped)
    return count


def load_account_id_map(source_conn, target_conn) -> dict[int, int]:
    """Map source `mail_accounts.id` -> target id, matched by EMAIL.

    The legacy dump and the live CRM have unrelated id sequences: copying a
    message's `account_id` verbatim files it under whatever mailbox happens to
    own that id in the target — i.e. some other person's mailbox (receive-side
    audit, 2026-11-10). The mailbox ADDRESS is the only identity both sides
    agree on, so the map is built from that and never from the id.
    """
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()
    src_cur.execute("SELECT id, lower(email) FROM mail_accounts WHERE email IS NOT NULL")
    src_rows = src_cur.fetchall()
    tgt_cur.execute("SELECT id, lower(email) FROM mail_accounts WHERE email IS NOT NULL")
    target_by_email = {row[1]: int(row[0]) for row in tgt_cur.fetchall()}
    mapping: dict[int, int] = {}
    for src_id, email in src_rows:
        target_id = target_by_email.get(email)
        if target_id is not None:
            mapping[int(src_id)] = target_id
    return mapping


def import_mail_messages(source_conn, target_conn) -> int:
    """Import mail messages from source to target database.

    The owning account is remapped through `load_account_id_map` (email, never
    the raw source id). A message whose source account has no target mailbox is
    SKIPPED and reported instead of being filed under an unrelated account.
    """
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()
    account_map = load_account_id_map(source_conn, target_conn)

    src_cur.execute("""
        SELECT account_id, imap_uid, message_id, in_reply_to, from_addr, to_addr,
               cc_addr, subject, body_text, body_html, date_received, is_read,
               is_flagged, is_archived, folder, conversation_id
        FROM mail_messages
        ORDER BY date_received ASC
    """)
    messages = src_cur.fetchall()
    count = 0
    skipped = 0
    for row in messages:
        try:
            source_account_id = row[0]
            target_account_id = account_map.get(int(source_account_id)) if source_account_id is not None else None
            if source_account_id is not None and target_account_id is None:
                skipped += 1
                continue
            tgt_cur.execute("""
                INSERT INTO mail_messages
                (account_id, imap_uid, message_id, from_addr, to_addr, cc_addr,
                 subject, body_text, body_html, date_received, is_read,
                 is_flagged, is_archived, folder)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, imap_uid, folder) DO NOTHING
            """, (
                target_account_id, row[1], row[2], row[4], row[5], row[6],
                row[7], row[8], row[9], row[10], row[11],
                row[12], row[13], row[14],
            ))
            count += 1
        except Exception as e:
            logger.warning("Failed to import message uid=%s: %s", row[1], e)
    target_conn.commit()
    if skipped:
        logger.warning(
            "Skipped %d message(s) whose source account has no matching mailbox in the "
            "target (filed under nothing rather than another account)", skipped)
    return count


def load_message_id_map(source_conn, target_conn, account_map: dict[int, int]) -> dict[int, int]:
    """Map source `mail_messages.id` -> target id.

    A deal link is a FK to a message row, so copying `message_id` verbatim
    attaches a project's history to whatever message owns that id in the
    target — a different customer's email (receive-side audit, 2026-11-10).
    The rows are matched on their real identity: (remapped account_id,
    imap_uid, folder), which is exactly the mail_messages unique key.
    """
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()
    src_cur.execute("SELECT id, account_id, imap_uid, folder FROM mail_messages")
    src_rows = src_cur.fetchall()
    tgt_cur.execute("SELECT id, account_id, imap_uid, folder FROM mail_messages")
    target_by_key = {
        (row[1], str(row[2]), row[3]): int(row[0])
        for row in tgt_cur.fetchall()
    }
    mapping: dict[int, int] = {}
    for src_id, account_id, imap_uid, folder in src_rows:
        if account_id is None:
            target_account_id = None
        else:
            target_account_id = account_map.get(int(account_id))
            if target_account_id is None:
                continue
        target_id = target_by_key.get((target_account_id, str(imap_uid), folder))
        if target_id is not None:
            mapping[int(src_id)] = target_id
    return mapping


def import_mail_deal_links(source_conn, target_conn) -> int:
    """Import mail-deal links from source to target database.

    Both ids are remapped: the message through `load_message_id_map` (a link
    must point at the SAME email in the target) and the account through the
    email-keyed account map. Links whose message is not present in the target
    are skipped and reported.
    """
    src_cur = source_conn.cursor()
    tgt_cur = target_conn.cursor()
    account_map = load_account_id_map(source_conn, target_conn)
    message_map = load_message_id_map(source_conn, target_conn, account_map)

    src_cur.execute("""
        SELECT message_id, opportunity_id, linked_by_user_id, linked_at
        FROM mail_deal_links
    """)
    links = src_cur.fetchall()
    count = 0
    skipped = 0
    for row in links:
        try:
            target_message_id = message_map.get(int(row[0]))
            if target_message_id is None:
                skipped += 1
                continue
            tgt_cur.execute("""
                INSERT INTO mail_deal_links (message_id, opportunity_id, linked_by_user_id, linked_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (message_id, opportunity_id) DO NOTHING
            """, (target_message_id, row[1], row[2], row[3]))
            count += 1
        except Exception as e:
            logger.warning("Failed to import link msg=%d opp=%d: %s", row[0], row[1], e)
    target_conn.commit()
    if skipped:
        logger.warning(
            "Skipped %d link(s) whose message was not imported into the target "
            "(never attached to a different email)", skipped)
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