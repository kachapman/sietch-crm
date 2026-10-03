#!/usr/bin/env python3
"""Focused live sync: per-opportunity tags + history + custom fields.

Used after import_json_export.py has loaded the bulk data, to fill in the
per-deal details that the JSON export omits.

Reads like a run from migrate_from_onlyoffice's tag/cf/history steps but
runs SEPARATELY so it can be retried without re-fetching the bulk data.

Usage:
    DB_HOST=127.0.0.1 DB_PASSWORD=... python3 sync_opp_details.py \
        --portal-url https://office.publicadjustermidwest.com \
        --email bot@vanguardadj.com \
        --password ...
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


RATE = 0.05
TIMEOUT = 30


def ssl_ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def crm_get(portal, token, path):
    url = f"{portal.rstrip('/')}{path}"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": token})
            with urllib.request.urlopen(req, context=ssl_ctx(), timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                time.sleep(RATE)
                return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 3:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise


def unwrap(data):
    if isinstance(data, dict):
        if "response" in data:
            return data["response"]
        if "result" in data:
            return data["result"]
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", default=os.getenv("DB_PORT", "5432"))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "sietch_crm"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "sietch"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--limit", type=int, default=0, help="stop after N opps (0 = all)")
    parser.add_argument("--since-id", type=int, default=0, help="only process opps with onlyoffice_id >= this")
    args = parser.parse_args()

    print(f"Authenticating to {args.portal_url}...")
    auth_body = json.dumps({"userName": args.email, "password": args.password}).encode()
    req = urllib.request.Request(
        f"{args.portal_url.rstrip('/')}/api/2.0/authentication.json",
        data=auth_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
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
    cur = conn.cursor()

    # Load opp map
    cur.execute("SELECT id, onlyoffice_id FROM opportunities WHERE onlyoffice_id IS NOT NULL ORDER BY onlyoffice_id::bigint")
    opp_map = {str(oid): int(iid) for iid, oid in cur.fetchall()}
    print(f"Loaded {len(opp_map)} opportunities to sync.")

    # Load tag map
    cur.execute("SELECT id, title FROM tag_definitions")
    tag_map = {t[1].lower(): t[0] for t in cur.fetchall()}

    # Load CF map (field_id by int_id from field_key)
    cur.execute("SELECT id, field_key FROM custom_field_definitions")
    cf_map = {}
    for fid, fkey in cur.fetchall():
        if fkey and fkey.startswith("field_"):
            try:
                cf_map[fkey[6:]] = fid
            except Exception:
                pass

    # Load existing history dedup index for all opps
    cur.execute("SELECT opportunity_id, crm_event_id FROM history_events WHERE crm_event_id IS NOT NULL")
    existing_hist_keys = set()
    for opp_id, crm_eid in cur.fetchall():
        if crm_eid is not None:
            existing_hist_keys.add((int(opp_id), int(crm_eid)))

    total = len(opp_map) if not args.limit else min(len(opp_map), args.limit)
    history_count = 0
    tag_count = 0
    cf_count = 0
    start_time = time.time()
    processed = 0

    oids = sorted(opp_map.keys(), key=lambda x: int(x))
    if args.since_id:
        oids = [o for o in oids if int(o) >= args.since_id]
    if args.limit:
        oids = oids[: args.limit]

    for oo in oids:
        processed += 1
        local_id = opp_map[oo]
        if processed % 25 == 0:
            elapsed = time.time() - start_time
            print(f"  [{processed}/{len(oids)}] tags={tag_count} cf={cf_count} hist={history_count} ({elapsed:.0f}s)")

        # ── Tags ──
        try:
            data = crm_get(args.portal_url, token, f"/api/2.0/crm/opportunity/tag/{oo}")
            tags = unwrap(data) or []
            for t in tags:
                # Live API returns strings; old API may return dicts.
                if isinstance(t, dict):
                    title = (t.get("title") or t.get("name") or "").strip()
                elif isinstance(t, str):
                    title = t.strip()
                else:
                    title = ""
                if not title:
                    continue
                tag_id = tag_map.get(title.lower())
                if not tag_id:
                    cur.execute("INSERT INTO tag_definitions (title, color) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING id", (title, "#6c757d"))
                    row = cur.fetchone()
                    if row:
                        tag_id = row[0]
                    else:
                        cur.execute("SELECT id FROM tag_definitions WHERE title = %s", (title,))
                        row = cur.fetchone()
                        if row:
                            tag_id = row[0]
                    if tag_id:
                        tag_map[title.lower()] = tag_id
                if tag_id:
                    cur.execute(
                        "INSERT INTO opportunity_tags (opportunity_id, tag_id, assigned_at) VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                        (local_id, tag_id),
                    )
                    tag_count += 1
        except Exception as e:
            print(f"  [warn] tags for opp {oo}: {e}")

        # ── Custom fields ──
        try:
            data = crm_get(args.portal_url, token, f"/api/2.0/crm/opportunity/{oo}/customfield")
            cf_list = unwrap(data) or []
            for cf in cf_list:
                if not isinstance(cf, dict):
                    continue
                cf_crm_id = str(cf.get("id") or "")
                cf_value = cf.get("value") or cf.get("fieldValue") or ""
                if isinstance(cf_value, dict):
                    cf_value = cf_value.get("title") or cf_value.get("text") or cf_value.get("value") or ""
                cf_value = str(cf_value).strip()
                v3_field_id = cf_map.get(cf_crm_id)
                if v3_field_id and cf_value:
                    cur.execute(
                        """INSERT INTO opportunity_custom_field_values (opportunity_id, field_id, field_value)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (opportunity_id, field_id) DO UPDATE SET field_value = EXCLUDED.field_value""",
                        (local_id, v3_field_id, cf_value),
                    )
                    cf_count += 1
        except Exception as e:
            print(f"  [warn] CF for opp {oo}: {e}")

        # ── History events ──
        try:
            data = crm_get(args.portal_url, token, f"/api/2.0/crm/history/filter?entityType=opportunity&entityId={oo}&count=500")
            hist_list = unwrap(data) or []
            for h in hist_list:
                if not isinstance(h, dict):
                    continue
                crm_eid = int(h.get("id") or 0)
                if crm_eid and (local_id, crm_eid) in existing_hist_keys:
                    continue
                cat_data = h.get("category") or {}
                cat_title = (cat_data.get("title") or "Note").strip() if isinstance(cat_data, dict) else "Note"
                cur.execute("SELECT id FROM history_categories WHERE title = %s", (cat_title,))
                row = cur.fetchone()
                if row:
                    cat_id = row[0]
                else:
                    cur.execute("INSERT INTO history_categories (title, is_system) VALUES (%s, TRUE) RETURNING id", (cat_title,))
                    cat_id = cur.fetchone()[0]
                raw_content = h.get("content") or h.get("Content") or ""
                event_content = str(raw_content).strip() or None if isinstance(raw_content, str) else None
                event_created = h.get("created") or h.get("Created") or None
                create_by = h.get("createBy") or {}
                crm_author_id = str(create_by.get("id") or "") if isinstance(create_by, dict) else ""
                cur.execute("SELECT id FROM users WHERE external_user_id = %s", (crm_author_id,))
                author_row = cur.fetchone()
                author_id = author_row[0] if author_row else None
                cur.execute(
                    """INSERT INTO history_events
                           (opportunity_id, category_id, content, created_by, created_at, backdated_created_at,
                            crm_event_id, crm_entity_id)
                       VALUES (%s,%s,%s,%s,COALESCE(%s,NOW()),%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (local_id, cat_id, event_content, author_id, event_created, event_created,
                     crm_eid or None, int(oo) if oo.isdigit() else None),
                )
                if crm_eid:
                    existing_hist_keys.add((local_id, crm_eid))
                history_count += 1
        except Exception as e:
            print(f"  [warn] history for opp {oo}: {e}")

        # Commit every 25 opps
        if processed % 25 == 0:
            conn.commit()

    conn.commit()
    elapsed = time.time() - start_time
    print(f"\nDone: {tag_count} tags, {cf_count} CF values, {history_count} history events in {elapsed:.0f}s")
    conn.close()


if __name__ == "__main__":
    main()