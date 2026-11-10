#!/usr/bin/env python3
"""Admin-safe: set/clear a user's mail-send recipient lock (migration 051).

DRY RUN BY DEFAULT. Not applied to prod.

    python3 scripts/set_mail_send_lock.py --user-email a@b.c
    python3 scripts/set_mail_send_lock.py --user-email a@b.c --set crm@vanguardadj.online --apply
    python3 scripts/set_mail_send_lock.py --user-email a@b.c --clear --apply
"""
import argparse, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import db

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-email", required=True)
    ap.add_argument("--set")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    db.init_db()
    u = db.query_one("SELECT id, email, mail_send_locked_recipient FROM users WHERE email = %s", (a.user_email,))
    if not u:
        print("user not found"); return 2
    target = None if a.clear else a.set
    print(f"user {u['id']} {u['email']}: current lock={u.get('mail_send_locked_recipient')!r} -> {target!r}")
    if not a.apply:
        print("DRY RUN (pass --apply to write)"); return 0
    db.execute("UPDATE users SET mail_send_locked_recipient = %s WHERE id = %s", (target, u["id"]))
    print("applied"); return 0

if __name__ == "__main__":
    raise SystemExit(main())
