#!/usr/bin/env python3
"""Fetch full email body for every Mail Message history event and embed it
into the email_snapshot JSON stored in history_events.content.

For each Mail Message event, the snapshot has a 'message_id'. We GET
filehandler.ashx?action=mailmessage&message_id={N} which returns the full
HTML (and embedded attachments URLs). We then UPDATE the row in place.

Idempotent: skips events where body_html and body_text are already populated.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2


TIMEOUT = 30
RATE = 0.05


def ssl_ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def crm_get_raw(portal, token, path, retries=3):
    url = f"{portal.rstrip('/')}{path}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json, text/html, */*",
                "Authorization": token,
            })
            with urllib.request.urlopen(req, context=ssl_ctx(), timeout=TIMEOUT) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", errors="replace")
            time.sleep(RATE)
            if "application/json" in ctype or raw.lstrip().startswith(("{", "[")):
                try:
                    return "json", json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return "html", raw
            if raw.strip().startswith("marker file"):
                return "marker", None
            return "html", raw
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return "err", e
        except Exception:
            time.sleep(0.5 * (2 ** attempt))
    return "err", None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--portal-url", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    p.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    p.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    p.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    p.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    print(f"Authenticating to {args.portal_url}...")
    auth_body = json.dumps({"userName": args.email, "password": args.password}).encode()
    req = urllib.request.Request(
        f"{args.portal_url.rstrip('/')}/api/2.0/authentication.json",
        data=auth_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=ssl_ctx(), timeout=20) as resp:
        auth = json.loads(resp.read().decode())
    token = auth.get("response", {}).get("token")
    if not token:
        sys.exit("No token")
    print("Authenticated.")

    conn = psycopg2.connect(host=args.db_host, port=args.db_port, dbname=args.db_name,
                            user=args.db_user, password=args.db_password)
    conn.autocommit = False
    cur = conn.cursor()

    # Mail Message events with a parseable message_id and an empty body
    cur.execute("""
        SELECT he.id, he.content
        FROM history_events he
        JOIN history_categories hc ON he.category_id = hc.id
        WHERE hc.title = 'Mail Message'
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} Mail Message events")

    if args.limit:
        rows = rows[: args.limit]

    updated = 0
    skipped = 0
    failed = 0
    for i, (event_id, content) in enumerate(rows):
        if not content:
            continue
        try:
            snapshot = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(snapshot, dict):
            continue
        # Skip if already populated
        if snapshot.get("body_html") and snapshot.get("body_text"):
            skipped += 1
            continue
        msg_id = snapshot.get("message_id") or snapshot.get("messageId") or snapshot.get("MessageId")
        if not msg_id:
            continue
        kind, payload = crm_get_raw(args.portal_url, token,
                                     f"/Products/CRM/HttpHandlers/filehandler.ashx?action=mailmessage&message_id={msg_id}")
        if kind == "err" or payload is None:
            failed += 1
            continue
        if kind == "json" and isinstance(payload, dict):
            # Unwrap envelope
            payload = payload.get("response", payload)
            html = payload.get("htmlBody") or payload.get("HtmlBody") or payload.get("bodyHtml") or ""
            text = payload.get("textBody") or payload.get("TextBody") or payload.get("plainText") or ""
            attachments_meta = payload.get("attachments") or []
        else:
            html = str(payload) if payload else ""
            text = ""
            attachments_meta = []
        if not html and not text:
            failed += 1
            continue
        # If htmlBody is empty but introduction is in content, keep introduction
        intro = snapshot.get("introduction") or ""
        text_fallback = text or intro
        snapshot["body_html"] = html
        snapshot["body_text"] = text_fallback
        # Add downloaded-attachment URLs to attachments list (filehandler URL)
        for a in attachments_meta:
            if not isinstance(a, dict):
                continue
            file_url = a.get("downloadUrl") or a.get("url") or ""
            snapshot.setdefault("attachments", []).append({
                "filename": a.get("filename") or a.get("title") or "",
                "size_bytes": a.get("size") or 0,
                "mime_type": a.get("mimeType") or a.get("contentType") or "",
                "url": file_url,
            })
        try:
            cur.execute(
                "UPDATE history_events SET content = %s WHERE id = %s",
                (json.dumps(snapshot, default=str), event_id),
            )
            updated += 1
        except Exception as e:
            print(f"  [warn] update event {event_id}: {e}")
            failed += 1
        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(rows)}] {updated} updated, {skipped} skipped, {failed} failed")
            conn.commit()

    conn.commit()
    print(f"\nDone: {updated} updated, {skipped} skipped, {failed} failed")
    conn.close()


if __name__ == "__main__":
    main()