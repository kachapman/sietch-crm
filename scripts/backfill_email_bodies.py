#!/usr/bin/env python3
"""Backfill missing email bodies into history_events from OnlyOffice's
mail_mail Elasticsearch index.

The CRM history event content stores subject / from / chain_id / message_id
but NOT the body. For the Mail Message events whose body_html/text is NULL we
look up the mail_mail doc in OnlyOffice ES and write the decoded HTML body
back into the event content.

Flow (all on the Sietch droplet):
  1. Read the list of body-missing events from the local Sietch Postgres.
  2. Copy the id list to the OnlyOffice droplet over SSH and run
     oo_fetch_bodies.py there, which bulk-queries ES and streams JSON lines back.
  3. Merge each body into history_events.content (JSON merge, all other fields
     preserved).

DRY-RUN BY DEFAULT. --write commits.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

import psycopg2

OO_HOST = "office.publicadjustermidwest.com"
REMOTE_SCRIPT = "/tmp/oo_fetch_bodies.py"


def fetch_bodies(ssh_key: str, ids: list[int], offsets: int = 4,
                 batch: int = 400) -> dict[int, dict]:
    """Return {message_id: {"body":..., "hit_id":...}} for resolvable ids."""
    resolved: dict[int, dict] = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i: i + batch]
        # Push the chunk to the OnlyOffice droplet, run the fetcher there,
        # and read its JSON lines back over the same SSH connection.
        remote_cmd = (
            f'cat > {REMOTE_SCRIPT} << "SCRIPT_EOF"\n'
            f'{_REMOTE_SCRIPT_BODY}\n'
            f'SCRIPT_EOF\n'
            f'docker cp {REMOTE_SCRIPT} onlyoffice-community-server:{REMOTE_SCRIPT} >/dev/null 2>&1\n'
            f'docker exec onlyoffice-community-server python3 {REMOTE_SCRIPT} '
            f'--ids {",".join(str(x) for x in chunk)} --offsets {offsets} 2>/dev/null'
        )
        ssh_cmd = ["ssh", "-i", ssh_key, "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
                   f"root@{OO_HOST}", remote_cmd]
        try:
            proc = subprocess.run(ssh_cmd, capture_output=True, timeout=900)
            out = proc.stdout.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [ssh error on chunk {i}] {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for line in out.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("body") and rec.get("message_id") is not None:
                resolved[int(rec["message_id"])] = {
                    "body": rec["body"],
                    "hit_id": rec.get("hit_id"),
                }
    return resolved


# The fetcher script that runs on the OnlyOffice droplet. Embedded here so the
# Sietch droplet can push it without needing a second copy to keep in sync.
_REMOTE_SCRIPT_BODY = r'''
import argparse, base64, json, subprocess, sys, urllib.request
ES = "http://onlyoffice-elasticsearch:9200"
ES_CONTAINER = "onlyoffice-elasticsearch"

def es_fetch(ids):
    if not ids:
        return {}
    payload = json.dumps({
        "query": {"ids": {"values": ids}},
        "_source": ["id", "document.data"],
        "size": len(ids),
    }).encode()
    raw = None
    try:
        req = urllib.request.Request(
            f"{ES}/mail_mail/_search?size={len(ids)}&_source=id,document.data",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"# direct es failed ({e})", file=sys.stderr)
        cmd = ["docker","exec","-i",ES_CONTAINER,"curl","-fsS","-m","120",
               f"{ES}/mail_mail/_search?size={len(ids)}&_source=id,document.data",
               "-H","Content-Type: application/json","-d","@-"]
        try:
            raw = subprocess.run(cmd, input=payload, capture_output=True, timeout=180).stdout
        except Exception as e2:
            print(f"# es bulk failed: {e2}", file=sys.stderr)
            return {}
    if not raw:
        return {}
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"# parse failed: {e}", file=sys.stderr)
        return {}
    res = {}
    for h in d.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        doc = src.get("document") or {}
        r = doc.get("data") or ""
        if not r:
            continue
        try:
            res[int(src.get("id"))] = base64.b64decode(r).decode("utf-8", errors="replace")
        except Exception:
            continue
    return res

ap = argparse.ArgumentParser()
ap.add_argument("--ids", default="")
ap.add_argument("--offsets", type=int, default=4)
a = ap.parse_args()
ids = [int(x) for x in a.ids.split(",") if x.strip().isdigit()]
if not ids:
    sys.exit(0)
wanted = set()
for mid in ids:
    wanted.add(mid)
    for off in range(1, a.offsets + 1):
        if mid - off > 0:
            wanted.add(mid - off)
        wanted.add(mid + off)
found = es_fetch(sorted(wanted))
print(f"# resolved {len(found)} docs for {len(ids)} ids", file=sys.stderr)
for mid in ids:
    hit = None
    for cand in [mid] + [c for off in range(1, a.offsets+1) for c in (mid-off, mid+off)]:
        if cand in found:
            hit = cand
            break
    if hit is None:
        print(json.dumps({"message_id": mid, "error": "not_found"}), flush=True)
    else:
        print(json.dumps({"message_id": mid, "hit_id": hit, "body": found[hit]}), flush=True)
'''


def strip_html(html: str) -> str:
    body = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()[:4000]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oo-host", default=OO_HOST)
    ap.add_argument("--ssh-key", default="/app/data/keys/id_ed25519_sietch_to_oo")
    ap.add_argument("--offsets", type=int, default=4)
    ap.add_argument("--batch", type=int, default=400, help="ids per ES bulk query")
    ap.add_argument("--limit", type=int, default=0, help="max events (0 = all)")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--host", default=os.environ.get("DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=int(os.environ.get("DB_PORT", "5432")))
    ap.add_argument("--name", default=os.environ.get("DB_NAME", "sietch_crm"))
    ap.add_argument("--user", default=os.environ.get("DB_USER", "sietch"))
    ap.add_argument("--password", default=os.environ.get("DB_PASSWORD", ""))
    ap.add_argument("--commit-every", type=int, default=200, help="commit every N rows")
    args = ap.parse_args()

    print(f"[backfill] oo={args.oo_host} write={args.write} offsets={args.offsets}")
    print(f"[backfill] db={args.host}:{args.port}/{args.name} as {args.user}")

    conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.name,
                            user=args.user, password=args.password)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("""
        SELECT id, opportunity_id,
               (content::json->>'message_id') AS message_id,
               content::text
        FROM history_events
        WHERE category_id = 9
          AND content::json->>'body_html' IS NULL
          AND content::json->>'body_text' IS NULL
          AND content::json->>'message_id' IS NOT NULL
          AND (content::json->>'message_id') ~ '^[0-9]+$'
        ORDER BY id
    """)
    candidates = cur.fetchall()
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"[backfill] {len(candidates)} body-missing Mail Message events")

    # Collect the distinct message_ids we need.
    msg_ids = sorted({int(row[2]) for row in candidates})
    print(f"[backfill] fetching {len(msg_ids)} distinct message_ids from ES...")

    t0 = time.time()
    bodies = fetch_bodies(args.ssh_key, msg_ids,
                          offsets=args.offsets, batch=args.batch)
    print(f"[backfill] ES returned bodies for {len(bodies)}/{len(msg_ids)} ids "
          f"in {time.time()-t0:.0f}s")

    resolved = 0
    unresolved = 0
    examples = []

    for he_id, opp_id, message_id, content_text in candidates:
        mid = int(message_id)
        rec = bodies.get(mid)
        if not rec:
            unresolved += 1
            if len(examples) < 8:
                examples.append(("unresolved", he_id, mid))
            continue
        try:
            j = json.loads(content_text)
        except Exception:
            j = {}
        if not isinstance(j, dict):
            j = {}
        j["body_html"] = rec["body"]
        j["body_text"] = strip_html(rec["body"])
        j["_body_source"] = "es_mail_mail"
        j["_body_hit_id"] = rec.get("hit_id")
        j["_body_backfilled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if args.write:
            cur.execute(
                "UPDATE history_events SET content = %s::jsonb WHERE id = %s",
                (json.dumps(j, ensure_ascii=False), he_id),
            )
        resolved += 1
        if len(examples) < 8:
            examples.append(("resolved", he_id, rec.get("hit_id"), len(rec["body"])))
        if resolved % args.commit_every == 0 and args.write:
            conn.commit()

    if args.write:
        conn.commit()

    cur.execute("""
        SELECT count(*) FROM history_events
        WHERE category_id = 9
          AND content::json->>'body_html' IS NULL
          AND content::json->>'body_text' IS NULL
    """)
    remaining = cur.fetchone()[0]
    cur.close()
    conn.close()

    print()
    print("=== summary ===")
    print(f"  resolved:   {resolved}")
    print(f"  unresolved: {unresolved}")
    print(f"  remaining missing-body events in DB: {remaining}")
    if not args.write:
        print()
        print("=== dry-run only — pass --write to commit ===")
    print()
    print("examples:")
    for ex in examples[:8]:
        print("  ", ex)


if __name__ == "__main__":
    main()
