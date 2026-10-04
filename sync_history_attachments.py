#!/usr/bin/env python3
"""Download history-attachments from the live OnlyOffice portal.

Walks every history_event that has a crm_event_id, fetches the full
event JSON from /api/2.0/crm/history/filter?entityType=opportunity&entityId=...,
and downloads any files[] it contains. Idempotent — re-runs skip files
already recorded by crm_file_id.

Usage:
    DB_HOST=127.0.0.1 DB_PASSWORD=... python3 sync_history_attachments.py \
        --portal-url https://office.publicadjustermidwest.com \
        --email bot@vanguardadj.com --password ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2


RATE = 0.05
TIMEOUT = 30
HISTORY_DIR = Path(__file__).resolve().parent / "data" / "history-attachments"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def ssl_ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def crm_get(portal, token, path, retries=3):
    url = f"{portal.rstrip('/')}{path}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": token})
            with urllib.request.urlopen(req, context=ssl_ctx(), timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                time.sleep(RATE)
                return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise
    return None


def unwrap(data):
    if isinstance(data, dict):
        if "response" in data:
            return data["response"]
        if "result" in data:
            return data["result"]
    return data


def crm_get_raw(url, headers=None, retries=3, timeout=60):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, context=ssl_ctx(), timeout=timeout) as resp:
                return resp.read()
        except Exception:
            time.sleep(0.5 * (2 ** attempt))
    return None


def safe_filename(name):
    out = re.sub(r"[^\w.\-]+", "_", name or "").strip("._")
    return out or "file"


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

    # Index: opp_id -> [event_id, crm_event_id, created_at]
    cur.execute("""
        SELECT he.id, he.opportunity_id, he.crm_event_id, o.onlyoffice_id
        FROM history_events he
        JOIN opportunities o ON he.opportunity_id = o.id
        WHERE he.crm_event_id IS NOT NULL
        ORDER BY o.onlyoffice_id::bigint
    """)
    events = []
    for he_id, opp_id, crm_eid, oo_id in cur.fetchall():
        if oo_id and crm_eid:
            events.append((he_id, opp_id, str(oo_id), int(crm_eid)))
    print(f"Loaded {len(events)} history events with crm_event_id.")

    # Existing attachments as (event_id, crm_file_id) pairs — the same file MAY
    # legitimately appear on multiple events, so dedup must be per-event, not
    # global by crm_file_id.
    cur.execute("SELECT event_id, crm_file_id FROM history_attachments WHERE crm_file_id IS NOT NULL")
    existing_files = {(r[0], r[1]) for r in cur.fetchall()}
    print(f"Already have {len(existing_files)} attachments by (event_id, crm_file_id).")

    # Group events by opp
    by_opp: dict[str, list[tuple[int, int]]] = {}
    for he_id, opp_id, oo_id, crm_eid in events:
        by_opp.setdefault(oo_id, []).append((he_id, crm_eid))

    if args.limit:
        keys = sorted(by_opp.keys(), key=lambda x: int(x))[: args.limit]
        by_opp = {k: by_opp[k] for k in keys}

    total = len(by_opp)
    start = time.time()
    downloaded = 0
    skipped = 0
    failed = 0
    processed = 0

    for oo_id, opp_events in by_opp.items():
        processed += 1
        if processed % 5 == 0:
            elapsed = time.time() - start
            rate = processed / elapsed if elapsed > 0 else 0
            remaining = (total - processed) / rate if rate > 0 else 0
            print(f"  [{processed}/{total}] {downloaded} downloaded, {skipped} skipped, {failed} failed ({int(elapsed)}s elapsed, ~{int(remaining)}s left)")

        try:
            data = crm_get(args.portal_url, token, f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={oo_id}&count=500")
            hist_list = unwrap(data) or []
        except Exception as e:
            print(f"  [warn] history fetch for opp {oo_id} failed: {e}")
            failed += 1
            continue

        # Build map: crm_event_id -> files[]
        crmid_to_files = {}
        for h in hist_list:
            if not isinstance(h, dict):
                continue
            ceid = int(h.get("id") or 0)
            if ceid:
                crmid_to_files[ceid] = h.get("files") or []

        for he_id, crm_eid in opp_events:
            files = crmid_to_files.get(crm_eid, [])
            for f_item in files:
                if not isinstance(f_item, dict):
                    continue
                f_id = int(f_item.get("id") or 0)
                if not f_id:
                    continue
                if (he_id, f_id) in existing_files:
                    skipped += 1
                    continue
                f_title = str(f_item.get("title") or f"file_{f_id}").strip()
                file_size = int(f_item.get("pureContentLength") or f_item.get("size") or 0)
                file_ext = Path(f_title).suffix.lstrip(".").lower()
                mime_type = _guess_mime(file_ext)
                view_url = f_item.get("viewUrl") or f_item.get("webUrl") or ""

                # Owner
                owner = f_item.get("createdBy") or {}
                owner_id = (owner.get("id") or "") if isinstance(owner, dict) else ""

                # Download
                url = f"{args.portal_url.rstrip('/')}/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid={f_id}"
                raw = crm_get_raw(url, headers={"Authorization": token}, timeout=60)

                rel_dir = Path("data") / "history-attachments" / oo_id / str(he_id)
                local_path = HISTORY_DIR / oo_id / str(he_id) / f"{f_id}_{safe_filename(f_title)}"
                local_path.parent.mkdir(parents=True, exist_ok=True)
                actual_size = file_size
                if raw:
                    local_path.write_bytes(raw)
                    actual_size = len(raw)
                    downloaded += 1
                else:
                    actual_size = 0
                rel_path = str(rel_dir / f"{f_id}_{safe_filename(f_title)}")
                try:
                    cur.execute(
                        """INSERT INTO history_attachments
                               (event_id, filename, file_path, file_size, mime_type, uploaded_by,
                                original_url, crm_file_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (event_id, crm_file_id) WHERE crm_file_id IS NOT NULL DO NOTHING""",
                        (he_id, f_title, rel_path, actual_size, mime_type,
                         None, view_url, f_id),
                    )
                    if cur.rowcount:
                        existing_files.add((he_id, f_id))
                except Exception as exc:
                    print(f"    [warn] insert for file {f_id} on event {he_id}: {exc}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    # Rebuild cursor state after rollback
                    cur = conn.cursor()
                    cur.execute("SELECT event_id, crm_file_id FROM history_attachments WHERE crm_file_id IS NOT NULL")
                    existing_files = {(r[0], r[1]) for r in cur.fetchall()}

        if processed % 5 == 0:
            conn.commit()

    conn.commit()
    elapsed = time.time() - start
    print(f"\nDone: {downloaded} downloaded, {skipped} skipped, {failed} failed in {int(elapsed)}s")
    conn.close()


def _guess_mime(ext):
    m = {
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt": "application/vnd.ms-powerpoint",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "txt": "text/plain",
        "csv": "text/csv",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
        "webp": "image/webp",
        "esx": "application/octet-stream",
    }
    return m.get(ext.lower(), "application/octet-stream")


if __name__ == "__main__":
    main()