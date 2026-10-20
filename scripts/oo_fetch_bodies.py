#!/usr/bin/env python3
"""Fetch email bodies from OnlyOffice's mail_mail ES index.

Runs ON the OnlyOffice droplet (inside the communityserver container, where ES
is reachable) and streams JSON lines to stdout:
    {"message_id": <int>, "body": "<html>", "hit_id": <int>}   per resolved doc
    {"message_id": <int>, "error": "..."}                        per unresolved

The Sietch droplet invokes this over SSH and consumes stdout. Nothing is
written to the OnlyOffice droplet.

Usage:
    python3 oo_fetch_bodies.py --ids 20244,19551,19692
    python3 oo_fetch_bodies.py --ids-file /tmp/ids.txt
    python3 oo_fetch_bodies.py --ids-file /tmp/ids.txt --offsets 4
"""
import argparse
import base64
import json
import subprocess
import sys
import urllib.request

ES = "http://onlyoffice-elasticsearch:9200"
ES_CONTAINER = "onlyoffice-elasticsearch"


def es_fetch(ids: list[int]) -> dict[int, str]:
    """Return {id: body_html} for the ids that exist, using ONE bulk query."""
    if not ids:
        return {}
    payload = json.dumps({
        "query": {"ids": {"values": ids}},
        "_source": ["id", "document.data"],
        "size": len(ids),
    }).encode()
    # Prefer a direct ES call (works when running inside the CS container on the
    # docker network). Fall back to shelling out through the ES container.
    raw = None
    try:
        req = urllib.request.Request(
            f"{ES}/mail_mail/_search?size={len(ids)}&_source=id,document.data",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"# direct es failed ({e}); trying docker exec", file=sys.stderr)
        cmd = [
            "docker", "exec", "-i", ES_CONTAINER,
            "curl", "-fsS", "-m", "120",
            f"{ES}/mail_mail/_search?size={len(ids)}&_source=id,document.data",
            "-H", "Content-Type: application/json",
            "-d", "@-",
        ]
        try:
            raw = subprocess.run(cmd, input=payload, capture_output=True,
                                 timeout=180).stdout
        except Exception as e2:
            print(f"# es bulk failed: {e2}", file=sys.stderr)
            return {}
    if not raw:
        return {}
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"# es bulk parse failed: {e}", file=sys.stderr)
        return {}
    res: dict[int, str] = {}
    for h in d.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        doc = src.get("document") or {}
        raw_doc = doc.get("data") or ""
        if not raw_doc:
            continue
        try:
            res[int(src.get("id"))] = base64.b64decode(raw_doc).decode("utf-8", errors="replace")
        except Exception:
            continue
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="comma-separated ids")
    ap.add_argument("--ids-file", default="", help="file with one id per line")
    ap.add_argument("--offsets", type=int, default=4,
                    help="also try message_id ±1..±N")
    args = ap.parse_args()

    ids: list[int] = []
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()]
    if args.ids_file:
        with open(args.ids_file) as fh:
            for line in fh:
                line = line.strip()
                if line.isdigit():
                    ids.append(int(line))
    if not ids:
        print("# no ids given", file=sys.stderr)
        return

    # Build the candidate set: each id plus its offsets
    wanted: set[int] = set()
    for mid in ids:
        wanted.add(mid)
        for off in range(1, args.offsets + 1):
            if mid - off > 0:
                wanted.add(mid - off)
            wanted.add(mid + off)

    found = es_fetch(sorted(wanted))
    print(f"# resolved {len(found)} docs for {len(ids)} requested ids", file=sys.stderr)

    for mid in ids:
        hit = None
        for cand in [mid] + [c for off in range(1, args.offsets + 1)
                              for c in (mid - off, mid + off)]:
            if cand in found:
                hit = cand
                break
        if hit is None:
            print(json.dumps({"message_id": mid, "error": "not_found"}), flush=True)
        else:
            print(json.dumps({"message_id": mid, "hit_id": hit,
                              "body": found[hit]}), flush=True)


if __name__ == "__main__":
    main()
