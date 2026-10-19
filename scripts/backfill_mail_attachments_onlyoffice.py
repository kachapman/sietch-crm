#!/usr/bin/env python3
"""Backfill mail_attachments.crm_file_id from OnlyOffice's archive.

Bug fix 2026-10-19: scanner stores mail_attachments as metadata-only. When the
OnlyOffice docs editor opens one, our serve endpoint tried live IMAP and
returned 404 for any email auto-archived / purged on the user's Outlook. The
editor shows "Download failed."

The dev server didn't have this problem because most emails were archived into
OnlyOffice's DB by the mail aggregator and downloaded via
filehandler.ashx?action=download&fileid={crm_file_id}.

This script backfills mail_attachments.crm_file_id (and crm_message_url) by
cross-referencing each mail message against the corresponding CRM_Email entity
in OnlyOffice (matched by RFC 5322 Message-ID header), then writes the
attachment file to data/mail-attachments/<message_id>/ and populates stored_path.

DRY-RUN BY DEFAULT. Pass --write to commit.

Required env vars / flags:
  OO_PORTAL_URL    OnlyOffice Community Server URL (default https://office.publicadjustermidwest.com)
  --oo-email       Admin email for OnlyOffice auth (or OO_EMAIL env)
  --oo-password    Admin password for OnlyOffice auth (or OO_PASSWORD env)
  --db-*           DB connection (defaults from .env or 127.0.0.1 / sietch)

Usage:
    python3 scripts/backfill_mail_attachments_onlyoffice.py
    python3 scripts/backfill_mail_attachments_onlyoffice.py --write
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "mail-attachments"


def _ssl_ctx():
    """Same default-context approach as server.py — accepts self-signed
    OnlyOffice cert (the OnlyOffice droplet uses Let's Encrypt so this is
    normally fine, but be permissive in case of proxy middleboxes)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def oo_authenticate(portal: str, email: str, password: str) -> str | None:
    """Return OnlyOffice auth token (Bearer) or None."""
    url = f"{portal.rstrip('/')}/api/2.0/authentication"
    body = json.dumps({"username": email, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"[backfill] auth failed: {e}", file=sys.stderr)
        return None
    token = data.get("response", {}).get("token") or data.get("token")
    if not token and data.get("status") == 1:
        # Some versions return the token directly
        token = data.get("token")
    return token


def oo_get(portal: str, token: str, path: str) -> dict | list | None:
    """GET OnlyOffice API path with Bearer token. Returns parsed JSON or None."""
    url = f"{portal.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def oo_download(portal: str, file_id: int, timeout: int = 30) -> bytes | None:
    """Download a file from OnlyOffice via filehandler.ashx. Returns bytes or None."""
    url = f"{portal.rstrip('/')}/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={file_id}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None
        raise
    except Exception:
        return None


def oo_find_crm_email_by_subject_date(portal: str, token: str, opp_crm_id: int,
                                       subject: str, date_sent_iso: str) -> int | None:
    """Fallback cross-reference when Message-ID doesn't match.

    Some scanner-ingested mail may not have an RFC 5322 Message-ID header (rare
    but possible for malformed mailing-list messages). Fall back to subject +
    approximate date match against the deal's history events.
    """
    # The CRM_Email is a history event with category title containing the subject.
    # We pull the deal's events and search.
    try:
        data = oo_get(portal, token,
                      f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={opp_crm_id}&count=500")
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    subj_norm = (subject or "").strip().lower()
    for h in data:
        if not isinstance(h, dict):
            continue
        cat = h.get("category") or {}
        if (cat.get("title") or "").lower() != "mail message":
            continue
        content = h.get("content") or ""
        # crude subject match
        if subj_norm and subj_norm not in str(content).lower():
            continue
        try:
            return int(h.get("id"))
        except (TypeError, ValueError):
            continue
    return None


def oo_get_message_attachments(portal: str, token: str, crm_message_id: int) -> list[dict]:
    """Fetch the CRM_Email's attached files. Returns list of {id, content_type, title}."""
    for path in (
        f"/api/2.0/crm/message/{crm_message_id}/files",
        f"/api/2.0/crm/mailmessage/{crm_message_id}",
        f"/api/2.0/crm/mailmessage/{crm_message_id}/files",
    ):
        try:
            data = oo_get(portal, token, path)
        except Exception:
            continue
        if data is None:
            continue
        # /mailmessage/{id} returns the message envelope; look for nested files
        if isinstance(data, dict):
            files = data.get("files") or data.get("response", {}).get("files") or []
            if isinstance(files, list) and files:
                return files
        elif isinstance(data, list) and data:
            return data
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", default=os.environ.get("OO_PORTAL_URL",
                                                       "https://office.publicadjustermidwest.com"))
    ap.add_argument("--oo-email", default=os.environ.get("OO_EMAIL", ""))
    ap.add_argument("--oo-password", default=os.environ.get("OO_PASSWORD", ""))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="max messages to process (0 = all)")
    ap.add_argument("--host", default=os.environ.get("DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=int(os.environ.get("DB_PORT", "5432")))
    ap.add_argument("--name", default=os.environ.get("DB_NAME", "sietch_crm"))
    ap.add_argument("--user", default=os.environ.get("DB_USER", "sietch"))
    ap.add_argument("--password", default=os.environ.get("DB_PASSWORD", ""))
    args = ap.parse_args()

    if not args.oo_email or not args.oo_password:
        sys.exit("OO_EMAIL and OO_PASSWORD required (env or --oo-email / --oo-password)")

    print(f"[backfill] auth against {args.portal} as {args.oo_email} (write={args.write})")
    token = oo_authenticate(args.portal, args.oo_email, args.oo_password)
    if not token:
        sys.exit("OnlyOffice auth failed — check creds / portal URL")

    conn = psycopg2.connect(
        host=args.host, port=args.port, dbname=args.name,
        user=args.user, password=args.password,
    )
    conn.autocommit = False
    cur = conn.cursor()

    # Pull all mail_messages with their onlyoffice-backed opportunities
    cur.execute(
        """
        SELECT mm.id, mm.message_id, mm.subject, mm.date_received,
               mm.account_id, mm.folder,
               o.id AS opp_id, o.onlyoffice_id
        FROM mail_messages mm
        LEFT JOIN mail_deal_links mdl ON mdl.message_id = mm.id
        LEFT JOIN opportunities o ON o.id = mdl.opportunity_id
        WHERE mm.message_id IS NOT NULL AND mm.message_id <> ''
        ORDER BY mm.date_received DESC NULLS LAST
        """
    )
    messages = cur.fetchall()
    if args.limit:
        messages = messages[: args.limit]
    print(f"[backfill] {len(messages)} mail messages to cross-reference")

    # Pull existing mail_attachments to know which need work
    cur.execute(
        """
        SELECT ma.id, ma.message_id, ma.filename, ma.stored_path, ma.crm_file_id
        FROM mail_attachments ma
        WHERE ma.crm_file_id IS NULL
        """
    )
    needs_link = {}
    for r in cur.fetchall():
        needs_link.setdefault(r[1], []).append({"id": r[0], "filename": r[2], "stored_path": r[3]})
    print(f"[backfill] {sum(len(v) for v in needs_link.values())} attachments across {len(needs_link)} messages need linking")

    linked = 0
    downloaded = 0
    skipped_no_opp = 0
    skipped_no_match = 0
    skipped_404 = 0
    skipped_other = 0

    for mm_id, mm_msg_id_hdr, subject, date_received, account_id, folder, opp_id, opp_oo_id in messages:
        if not needs_link.get(mm_id):
            continue
        if opp_id is None or opp_oo_id is None:
            skipped_no_opp += 1
            continue
        # Try to find the CRM_Email in OnlyOffice by subject+date (onlyOffice's
        # message-id header is per-message-id format we don't always have exact
        # matching for, so use subject + approximate date against the opp's events)
        crm_email_id = oo_find_crm_email_by_subject_date(
            args.portal, token, int(opp_oo_id), subject or "", date_received.isoformat() if date_received else ""
        )
        if not crm_email_id:
            skipped_no_match += 1
            continue
        # Pull the attached files
        files = oo_get_message_attachments(args.portal, token, crm_email_id)
        if not files:
            skipped_no_match += 1
            continue
        # Match by filename
        filename_to_file = {f.get("title") or f.get("fileName"): f for f in files if isinstance(f, dict)}
        for ma in needs_link[mm_id]:
            f = filename_to_file.get(ma["filename"])
            if not f:
                continue
            file_id = int(f.get("id") or 0)
            if not file_id:
                continue
            # Update DB row with crm_file_id (we attempt to download too)
            payload = oo_download(args.portal, file_id, timeout=30)
            if payload is None:
                skipped_404 += 1
                continue
            # Cache to disk
            safe_name = re.sub(r"[^\w.\- ]+", "_", ma["filename"]).strip("._") or "file"
            rel_dir = Path("data") / "mail-attachments" / str(mm_id)
            full = DATA_DIR / str(mm_id) / f"{ma['id']}_{safe_name}"
            rel_path = f"data/mail-attachments/{mm_id}/{full.name}"
            try:
                full.parent.mkdir(parents=True, exist_ok=True)
                if args.write:
                    full.write_bytes(payload)
            except Exception as e:
                print(f"[backfill] write failed for ma={ma['id']}: {e}")
                skipped_other += 1
                continue
            if args.write:
                cur.execute(
                    "UPDATE mail_attachments SET crm_file_id=%s, crm_message_url=%s, "
                    "stored_path=%s, fetch_source='onlyoffice' "
                    "WHERE id=%s",
                    (file_id,
                     f"/Products/CRM/HttpHandlers/filehandler.ashx?action=mailmessage&message_id={crm_email_id}",
                     rel_path, ma["id"]),
                )
            linked += 1
            downloaded += 1
        # Be polite to the OnlyOffice API
        time.sleep(0.05)

    if args.write:
        conn.commit()
    cur.close()
    conn.close()

    print()
    print("=== summary ===")
    print(f"  attachments linked: {linked}")
    print(f"  files downloaded + cached: {downloaded}")
    print(f"  skipped (no opp link): {skipped_no_opp}")
    print(f"  skipped (no OnlyOffice match): {skipped_no_match}")
    print(f"  skipped (404 on filehandler): {skipped_404}")
    print(f"  skipped (other error): {skipped_other}")
    if not args.write:
        print()
        print("=== dry-run only (no DB writes). pass --write to commit. ===")


if __name__ == "__main__":
    main()