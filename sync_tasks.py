#!/usr/bin/env python3
"""Sync tasks from the live OnlyOffice portal."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

import psycopg2


RATE = 0.05
TIMEOUT = 30


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


def paginate(portal, token, base_path, page_size=500):
    items = []
    start = 0
    while True:
        sep = "&" if "?" in base_path else "?"
        path = f"{base_path}{sep}count={page_size}&startIndex={start}"
        data = unwrap(crm_get(portal, token, path))
        if not isinstance(data, list) or not data:
            break
        items.extend(data)
        if len(data) < page_size:
            break
        start += page_size
    return items


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

    # Load maps
    cur.execute("SELECT id, onlyoffice_id FROM opportunities WHERE onlyoffice_id IS NOT NULL")
    opp_map = {str(oid): int(iid) for iid, oid in cur.fetchall()}
    cur.execute("SELECT id, email FROM users")
    user_by_email = {e.lower(): i for i, e in cur.fetchall()}

    cur.execute("SELECT id, external_user_id FROM users WHERE external_user_id IS NOT NULL")
    user_by_oo = {oo: int(i) for i, oo in cur.fetchall()}

    # Fetch tasks
    open_tasks = paginate(args.portal_url, token, "/api/2.0/crm/task/filter?count=500&isClosed=false")
    closed_tasks = paginate(args.portal_url, token, "/api/2.0/crm/task/filter?count=500&isClosed=true")
    all_tasks = open_tasks + closed_tasks
    print(f"Fetched {len(open_tasks)} open + {len(closed_tasks)} closed = {len(all_tasks)} tasks")

    count = 0
    for t in all_tasks:
        if not isinstance(t, dict):
            continue
        title = str(t.get("title") or "").strip()
        if not title:
            continue
        crm_id = str(t.get("id") or "")
        # Skip if already exists
        if crm_id:
            cur.execute("SELECT 1 FROM tasks WHERE onlyoffice_id = %s", (crm_id,))
            if cur.fetchone():
                continue
        description = str(t.get("description") or "").strip() or None
        is_closed = bool(t.get("isClosed") or t.get("IsClosed") or False)
        # Link to opp
        opp_data = t.get("opportunity") or {}
        crm_opp_id = ""
        if isinstance(opp_data, dict):
            crm_opp_id = str(opp_data.get("id") or "")
        if not crm_opp_id:
            ent = t.get("entity") or {}
            if isinstance(ent, dict) and ent.get("entityType") == "opportunity":
                crm_opp_id = str(ent.get("entityId") or "")
        local_opp_id = opp_map.get(crm_opp_id)
        # Responsible
        resp_data = t.get("responsible") or {}
        crm_resp = str(resp_data.get("id") or "") if isinstance(resp_data, dict) else ""
        local_resp_id = user_by_oo.get(crm_resp)
        due_date = t.get("deadLine") or t.get("dueDate") or None
        if due_date:
            try:
                due_date = str(due_date)[:10]
            except Exception:
                pass
        priority = int(t.get("priority") or 0)
        closed_at = None
        if is_closed:
            closed_at = t.get("closedDate") or t.get("ClosedDate") or None
        try:
            cur.execute(
                """INSERT INTO tasks
                       (title, description, opportunity_id, responsible_user_id,
                        due_date, priority, is_closed, closed_at, created_at, onlyoffice_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                   ON CONFLICT (onlyoffice_id) WHERE onlyoffice_id IS NOT NULL DO NOTHING""",
                (title, description, local_opp_id, local_resp_id, due_date,
                 priority, is_closed, closed_at, crm_id or None),
            )
            count += 1
        except Exception as e:
            print(f"  [warn] insert task {crm_id}: {e}")

    conn.commit()
    print(f"Inserted {count} tasks (skipped {len(all_tasks) - count} existing)")
    conn.close()


if __name__ == "__main__":
    main()