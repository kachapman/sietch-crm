#!/usr/bin/env python3
"""Admin-safe: set/clear an API token's mail-send recipient lock (migration 054).

DRY RUN BY DEFAULT. Not applied to prod.

The per-user lock (scripts/set_mail_send_lock.py, migration 051) covers a
human's sends. A token can ALSO carry its own lock, independent of its owner,
so a long-lived acceptance key can only ever mail one address even though it
acts as an admin.

Select the token by id, prefix, or name (exactly one):

    python3 scripts/set_token_mail_send_lock.py --token-id 7
    python3 scripts/set_token_mail_send_lock.py --token-id 7 \
        --set crm@vanguardadj.online --apply
    python3 scripts/set_token_mail_send_lock.py --token-prefix sietch_ab12 --clear --apply
    python3 scripts/set_token_mail_send_lock.py --token-name "acceptance" --set crm@vanguardadj.online

Never prints a token secret: only id / name / prefix / current lock.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import db  # noqa: E402

COLUMNS = ("id, name, token_prefix, created_by, revoked_at, mail_send_locked_recipient")


def _row(args):
    if args.token_id is not None:
        return db.query_one(f"SELECT {COLUMNS} FROM api_tokens WHERE id = %s", (args.token_id,))
    if args.token_prefix:
        return db.query_one(
            f"SELECT {COLUMNS} FROM api_tokens WHERE token_prefix = %s ORDER BY id DESC LIMIT 1",
            (args.token_prefix,),
        )
    if args.token_name:
        return db.query_one(
            f"SELECT {COLUMNS} FROM api_tokens WHERE name = %s ORDER BY id DESC LIMIT 1",
            (args.token_name,),
        )
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--token-id", type=int)
    sel.add_argument("--token-prefix", help="token_prefix shown in the admin console")
    sel.add_argument("--token-name")
    ap.add_argument("--set", help="locked recipient address (e.g. crm@vanguardadj.online)")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    a = ap.parse_args(argv)
    if not a.set and not a.clear:
        ap.error("pass --set <address> or --clear")

    db.init_db()
    row = _row(a)
    if not row:
        print("token not found")
        return 2
    target = None if a.clear else (a.set or "").strip() or None
    state = "REVOKED" if row.get("revoked_at") else "active"
    print(
        f"token {row['id']} {row['name']!r} prefix={row['token_prefix']!r} "
        f"created_by={row['created_by']} ({state}): "
        f"current lock={row.get('mail_send_locked_recipient')!r} -> {target!r}"
    )
    if not a.apply:
        print("DRY RUN (pass --apply to write)")
        return 0
    db.execute(
        "UPDATE api_tokens SET mail_send_locked_recipient = %s WHERE id = %s",
        (target, row["id"]),
    )
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
