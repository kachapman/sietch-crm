#!/usr/bin/env python3
"""Backfill opportunities.contact_id for deals NOT in CRM_export/.

The export-based backfill (scripts/backfill_contacts_from_export.py) covers
1,184 of 1,242 deals. The remaining 51 are the deals created 2026-10-19 by
the live sync (onlyoffice_id >= 1241) — they were never exported to
CRM_export/.

This script fetches each remaining deal from the live OnlyOffice API
(/api/2.0/crm/opportunity/{id}), pulls its contact.id, maps to local
contacts.onlyoffice_id, and writes opportunities.contact_id.

DRY-RUN BY DEFAULT. Pass --write to commit.

Required:
  --oo-email / --oo-password    OnlyOffice admin creds (or OO_EMAIL / OO_PASSWORD env)
"""
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


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def oo_authenticate(portal: str, email: str, password: str) -> str | None:
    """Authenticate to OnlyOffice via /api/2.0/authentication.json. Returns bearer token."""
    url = f"{portal.rstrip('/')}/api/2.0/authentication.json"
    body = json.dumps({"userName": email, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=15) as resp:
            return json.loads(resp.read()).get("response", {}).get("token")
    except urllib.error.HTTPError as e:
        sys.exit(f"OnlyOffice auth HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
    except Exception as e:
        sys.exit(f"OnlyOffice auth failed: {e}")


def oo_get(portal: str, token: str, path: str) -> dict | list | None:
    url = f"{portal.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", default=os.environ.get("OO_PORTAL_URL",
                                                       "https://office.publicadjustermidwest.com"))
    ap.add_argument("--oo-email", default=os.environ.get("OO_EMAIL", ""))
    ap.add_argument("--oo-password", default=os.environ.get("OO_PASSWORD", ""))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="max deals to process (0 = all)")
    ap.add_argument("--host", default=os.environ.get("DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=int(os.environ.get("DB_PORT", "5432")))
    ap.add_argument("--name", default=os.environ.get("DB_NAME", "sietch_crm"))
    ap.add_argument("--user", default=os.environ.get("DB_USER", "sietch"))
    ap.add_argument("--password", default=os.environ.get("DB_PASSWORD", ""))
    args = ap.parse_args()

    if not args.oo_email or not args.oo_password:
        sys.exit("OO_EMAIL and OO_PASSWORD required (env or --oo-email / --oo-password)")

    print(f"[backfill-live] auth against {args.portal} as {args.oo_email} (write={args.write})")
    token = oo_authenticate(args.portal, args.oo_email, args.oo_password)
    if not token:
        sys.exit("OnlyOffice auth returned no token")

    conn = psycopg2.connect(
        host=args.host, port=args.port, dbname=args.name,
        user=args.user, password=args.password,
    )
    conn.autocommit = False
    cur = conn.cursor()

    # Build local contacts map
    cur.execute("SELECT id, onlyoffice_id FROM contacts WHERE onlyoffice_id IS NOT NULL")
    contact_local = {str(row[1]): row[0] for row in cur.fetchall()}
    print(f"[backfill-live]   {len(contact_local)} local contacts")

    # Find opportunities with NULL contact_id — these are the 51 + any others
    # not yet handled. We try fetching each from OnlyOffice by onlyoffice_id.
    cur.execute(
        """
        SELECT o.id, o.title, o.onlyoffice_id
        FROM opportunities o
        WHERE o.contact_id IS NULL AND o.onlyoffice_id IS NOT NULL
        ORDER BY o.id
        """
    )
    candidates = cur.fetchall()
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"[backfill-live] {len(candidates)} opportunities need a contact")

    linked = 0
    fetched = 0
    skipped_no_contact = 0
    skipped_no_local_match = 0
    skipped_404 = 0
    skipped_other = 0

    for opp_id, title, onlyoffice_id in candidates:
        # OnlyOffice API: /api/2.0/crm/opportunity/{id} returns the deal envelope
        data = oo_get(args.portal, token, f"/api/2.0/crm/opportunity/{onlyoffice_id}")
        if data is None:
            skipped_404 += 1
            continue
        # API returns {"response": {...}} wrapper in 7.x
        if isinstance(data, dict) and "response" in data:
            data = data["response"]
        fetched += 1
        contact_block = (data.get("contact") if isinstance(data, dict) else None) or {}
        if not isinstance(contact_block, dict):
            skipped_no_contact += 1
            continue
        crm_contact_id = str(contact_block.get("id") or contact_block.get("ID") or "")
        if not crm_contact_id or crm_contact_id == "0":
            skipped_no_contact += 1
            continue
        local_contact_id = contact_local.get(crm_contact_id)
        if not local_contact_id:
            skipped_no_local_match += 1
            continue
        if args.write:
            cur.execute(
                "UPDATE opportunities SET contact_id = %s WHERE id = %s AND contact_id IS NULL",
                (local_contact_id, opp_id),
            )
        linked += 1
        # Be polite to the OnlyOffice API
        time.sleep(0.05)

    if args.write:
        conn.commit()
    cur.close()
    conn.close()

    print()
    print("=== summary ===")
    print(f"  deals fetched from OnlyOffice: {fetched}")
    print(f"  contact_id writes: {linked}")
    print(f"  skipped (no contact ref on deal): {skipped_no_contact}")
    print(f"  skipped (contact ref not in local DB): {skipped_no_local_match}")
    print(f"  skipped (404 from OnlyOffice): {skipped_404}")
    print(f"  skipped (other): {skipped_other}")
    if not args.write:
        print()
        print("=== dry-run only (no DB writes). pass --write to commit. ===")


if __name__ == "__main__":
    main()