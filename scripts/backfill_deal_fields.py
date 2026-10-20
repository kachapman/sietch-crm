#!/usr/bin/env python3
"""Backfill the standard deal fields that the 2026-10-19 sync dropped.

13 opportunities (ids 1235-1247) were created by a buggy sync path that
wrote only 7 columns into `opportunities`, so they came out with:

    stage_id = NULL, stage_type = 0, responsible_user_id = NULL,
    actual_close_date = NULL, actual_close_value = NULL, bid_value = NULL
    expected_close_date = NULL   (+ contact_id for one of them)

Every other deal (imported via migrate_from_onlyoffice.py) has all of these.

This script re-pulls each affected deal from the OnlyOffice CRM API and
writes the resolved values back. It runs on the Sietch droplet and talks to
OnlyOffice over HTTPS (same auth as the other backfill scripts).

DRY-RUN BY DEFAULT. --write commits.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2

OO_PORTAL = os.environ.get("OO_PORTAL_URL", "https://office.publicadjustermidwest.com")


def _ssl_ctx():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def oo_authenticate(portal: str, email: str, password: str) -> str | None:
    url = f"{portal.rstrip('/')}/api/2.0/authentication.json"
    body = json.dumps({"userName": email, "password": password}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=20) as resp:
            return json.loads(resp.read()).get("response", {}).get("token")
    except urllib.error.HTTPError as e:
        sys.exit(f"OnlyOffice auth HTTP {e.code}: "
                 f"{e.read().decode('utf-8', errors='replace')[:200]}")
    except Exception as e:
        sys.exit(f"OnlyOffice auth failed: {e}")


def oo_get_opportunity(portal: str, token: str, oo_id: str) -> dict | None:
    url = f"{portal.rstrip('/')}/api/2.0/crm/opportunity/{urllib.parse.quote(str(oo_id))}"
    req = urllib.request.Request(url, headers={
        "Authorization": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None
    if isinstance(data, dict) and "response" in data:
        data = data["response"]
    return data if isinstance(data, dict) else None


def _is_closure_sentinel(val: str) -> bool:
    """The CRM sends .NET's DateTime.MinValue as a close date when unset."""
    s = str(val or "").strip()
    return s.startswith("0001-01-01") or s.startswith("0001-1-1") or s == ""


def _same_date(a, b) -> bool:
    """Compare a DB DATE value with a 'YYYY-MM-DD' string from the API."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a)[:10] == str(b)[:10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", default=OO_PORTAL)
    ap.add_argument("--oo-email", default=os.environ.get("OO_EMAIL", ""))
    ap.add_argument("--oo-password", default=os.environ.get("OO_PASSWORD", ""))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max deals (0 = all)")
    ap.add_argument("--only", default="",
                    help="comma-separated opportunity ids to restrict to "
                         "(default: every opportunity missing a field)")
    ap.add_argument("--min-id", type=int, default=0,
                    help="only consider opportunities with id >= this")
    ap.add_argument("--host", default=os.environ.get("DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=int(os.environ.get("DB_PORT", "5432")))
    ap.add_argument("--name", default=os.environ.get("DB_NAME", "sietch_crm"))
    ap.add_argument("--user", default=os.environ.get("DB_USER", "sietch"))
    ap.add_argument("--password", default=os.environ.get("DB_PASSWORD", ""))
    args = ap.parse_args()

    if not args.oo_email or not args.oo_password:
        sys.exit("OO_EMAIL and OO_PASSWORD required (env or --oo-email / --oo-password)")

    print(f"[backfill-deals] auth against {args.portal} as {args.oo_email} (write={args.write})")
    token = oo_authenticate(args.portal, args.oo_email, args.oo_password)
    if not token:
        sys.exit("OnlyOffice auth returned no token")

    conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.name,
                            user=args.user, password=args.password)
    conn.autocommit = False
    cur = conn.cursor()

    # Deals missing the standard fields. The 2026-10-19 batch is ids 1235-1247;
    # the query is written generally so a future bad sync is also caught.
    only_ids = []
    if args.only:
        only_ids = [int(x) for x in args.only.split(",") if x.strip().isdigit()]

    cur.execute("""
        SELECT o.id, o.title, o.onlyoffice_id,
               o.stage_id, o.stage_type, o.responsible_user_id,
               o.actual_close_date, o.actual_close_value, o.bid_value,
               o.expected_close_date, o.contact_id
        FROM opportunities o
        WHERE o.onlyoffice_id IS NOT NULL
          AND (o.stage_id IS NULL
               OR o.responsible_user_id IS NULL
               OR o.actual_close_date IS NULL
               OR o.bid_value IS NULL
               OR o.expected_close_date IS NULL
               OR o.contact_id IS NULL)
        ORDER BY o.id
    """)
    candidates = [r for r in cur.fetchall()
                  if (not only_ids or r[0] in only_ids) and r[0] >= args.min_id]
    if args.limit:
        candidates = candidates[: args.limit]
    scope = (f"ids={args.only}" if only_ids else
             (f"min-id>={args.min_id}" if args.min_id else "all"))
    print(f"[backfill-deals] {len(candidates)} opportunities need field backfill (scope: {scope})")

    # Local lookup maps
    cur.execute("SELECT id, title, stage_type FROM stages")
    stage_by_title = {}
    for r in cur.fetchall():
        key = str(r[1] or "").strip().lower()
        if key:
            stage_by_title[key] = {"id": r[0], "stage_type": r[2] or 0}
    cur.execute("SELECT id, email FROM users")
    user_by_email = {str(r[1] or "").strip().lower(): r[0] for r in cur.fetchall()}
    cur.execute("SELECT id, onlyoffice_id FROM contacts WHERE onlyoffice_id IS NOT NULL")
    contact_by_oo = {str(r[1]): r[0] for r in cur.fetchall()}
    print(f"[backfill-deals]   {len(stage_by_title)} stages, {len(user_by_email)} users, "
          f"{len(contact_by_oo)} contacts mapped")

    # OnlyOffice user UUID -> local user id (email is the stable join key)
    uu = urllib.request.Request(f"{args.portal.rstrip('/')}/api/2.0/people",
                                headers={"Authorization": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(uu, context=_ssl_ctx(), timeout=30) as resp:
            people = json.loads(resp.read()).get("response") or []
    except Exception:
        people = []
    oo_user_to_local = {}
    for p in people:
        if not isinstance(p, dict):
            continue
        em = str(p.get("email") or "").strip().lower()
        pid = p.get("id")
        if em and pid and em in user_by_email:
            oo_user_to_local[str(pid)] = user_by_email[em]
    print(f"[backfill-deals]   {len(oo_user_to_local)} OnlyOffice users mapped to local users")

    updated = 0
    unchanged = 0
    skipped_404 = 0
    failed = 0
    plan = []

    for row in candidates:
        (opp_id, title, oo_id, c_stage, c_stage_type, c_resp,
         c_acd, c_acv, c_bid, c_exp, c_contact) = row
        o = oo_get_opportunity(args.portal, token, oo_id)
        if not o:
            skipped_404 += 1
            continue

        # --- stage ---
        stage_data = o.get("stage") or {}
        stage_title = str(stage_data.get("title") or "").strip().lower()
        st = stage_by_title.get(stage_title)
        new_stage = st["id"] if st else None
        new_stage_type = st["stage_type"] if st else 0
        if stage_data:
            try:
                new_stage_type = int(stage_data.get("stageType")
                                      or stage_data.get("StageType")
                                      or new_stage_type)
            except (TypeError, ValueError):
                pass

        # --- responsible ---
        resp = o.get("responsible") or {}
        new_resp = oo_user_to_local.get(str(resp.get("id") or "")) if isinstance(resp, dict) else None

        # --- close date / value ---
        new_acd = c_acd
        acd = o.get("actualCloseDate")
        if acd and not _is_closure_sentinel(acd):
            try:
                new_acd = str(acd)[:10]
            except Exception:
                new_acd = c_acd
        new_acv = c_acv
        for k in ("actualCloseValue", "ActualCloseValue"):
            if o.get(k) is not None:
                try:
                    new_acv = float(o[k])
                except (TypeError, ValueError):
                    pass
                break

        # --- bid value ---
        new_bid = c_bid
        for k in ("bidValue", "BidValue", "value", "Value"):
            if o.get(k) is not None:
                try:
                    new_bid = float(o[k])
                except (TypeError, ValueError):
                    pass
                break

        # --- expected close date ---
        new_exp = c_exp
        for k in ("expectedCloseDate", "ExpectedCloseDate", "dueDate"):
            if o.get(k) and not _is_closure_sentinel(o[k]):
                try:
                    new_exp = str(o[k])[:10]
                except Exception:
                    new_exp = c_exp
                if new_exp:
                    break

        # --- contact ---
        cont = o.get("contact") or {}
        new_contact = c_contact
        if isinstance(cont, dict):
            cid = str(cont.get("id") or cont.get("ID") or "")
            if cid and cid in contact_by_oo:
                new_contact = contact_by_oo[cid]

        changes = []
        if new_stage is not None and new_stage != c_stage:
            changes.append(f"stage {c_stage} -> {new_stage} ({stage_title or '?'})")
        if new_stage_type != c_stage_type:
            changes.append(f"stage_type {c_stage_type} -> {new_stage_type}")
        if new_resp is not None and new_resp != c_resp:
            changes.append(f"responsible {c_resp} -> {new_resp}")
        if not _same_date(new_acd, c_acd):
            changes.append(f"close_date {c_acd} -> {new_acd}")
        if new_acv != c_acv:
            changes.append(f"close_value {c_acv} -> {new_acv}")
        if new_bid != c_bid:
            changes.append(f"bid {c_bid} -> {new_bid}")
        if not _same_date(new_exp, c_exp):
            changes.append(f"expected_close {c_exp} -> {new_exp}")
        if new_contact != c_contact:
            changes.append(f"contact {c_contact} -> {new_contact}")

        if not changes:
            unchanged += 1
            continue

        plan.append((opp_id, title, changes))
        if args.write:
            try:
                cur.execute(
                    """UPDATE opportunities
                       SET stage_id = COALESCE(%s, stage_id),
                           stage_type = CASE WHEN %s IS NULL THEN stage_type ELSE %s END,
                           responsible_user_id = COALESCE(%s, responsible_user_id),
                           actual_close_date = COALESCE(%s, actual_close_date),
                           actual_close_value = COALESCE(%s, actual_close_value),
                           bid_value = COALESCE(%s, bid_value),
                           expected_close_date = COALESCE(%s, expected_close_date),
                           contact_id = COALESCE(%s, contact_id)
                       WHERE id = %s""",
                    (new_stage, new_stage_type, new_stage_type, new_resp,
                     new_acd, new_acv, new_bid, new_exp, new_contact, opp_id),
                )
            except Exception as e:
                failed += 1
                print(f"  [error] opp {opp_id}: {e}")
                continue
        updated += 1

    if args.write:
        conn.commit()

    cur.execute("""
        SELECT count(*) FROM opportunities
        WHERE onlyoffice_id IS NOT NULL
          AND (stage_id IS NULL OR responsible_user_id IS NULL
               OR actual_close_date IS NULL OR bid_value IS NULL
               OR expected_close_date IS NULL OR contact_id IS NULL)
    """)
    remaining = cur.fetchone()[0]
    cur.close()
    conn.close()

    print()
    print("=== summary ===")
    print(f"  opportunities updated: {updated}")
    print(f"  unchanged:             {unchanged}")
    print(f"  skipped (404 in OO):   {skipped_404}")
    print(f"  failed:                {failed}")
    print(f"  still missing fields:  {remaining}")
    print()
    print("planned changes:")
    for opp_id, title, changes in plan[:20]:
        print(f"  [{opp_id}] {str(title)[:38]:<38} " + "; ".join(changes))
    if len(plan) > 20:
        print(f"  ... and {len(plan) - 20} more")
    if not args.write:
        print()
        print("=== dry-run only — pass --write to commit ===")


if __name__ == "__main__":
    main()
