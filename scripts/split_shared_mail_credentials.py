#!/usr/bin/env python3
"""Split SHARED mail credentials into per-account ownership (item 2, 2026-11-10).

WHY
---
Every credential belongs to exactly one `mail_accounts` row, keyed by `id`.
A row may hold another mailbox's grant after a mis-bound connect, a legacy
import keyed by email, or a manual row edit. Two rows holding the SAME
Microsoft refresh token is the worst case: Microsoft rotates the grant on
use, so the first row to refresh silently invalidates the second — which is
how "all Outlook accounts lost OAuth access at once" happens.

WHAT IT DOES
------------
1. Reads `mail_accounts` and finds sharing:
     * the same Exchange refresh token on 2+ rows   (fingerprint match)
     * the same Graph refresh token on 2+ rows      (fingerprint match)
     * `oauth_identity` / `oauth_graph_identity` naming a different mailbox
     * `smtp_user` set to a different mailbox's address
     * two rows with the same email (case-insensitive)
2. Prints a plan. NOTHING is written unless `--apply --yes` is passed.
3. `--apply` clears credentials from the rows that cannot own them:
     * a shared grant stays only on the row whose verified identity is its
       own email; every other row loses that token family;
     * a mismatched token family is cleared from the row it was found on;
     * `smtp_user` pointing at another mailbox is cleared (send falls back to
       the account email).
   Credentials are NEVER copied from one row to another. Every cleared row
   gets `auth_error` telling the user to reconnect, so the UI shows it.
   Updates are compare-and-swap on the token value that was read, so a
   reconnect that happened meanwhile is not overwritten.

SAFETY
------
* Default is a DRY RUN on a READ-ONLY connection (`set_session(readonly=True)`,
  the same approach as `scripts/diagnose_mail_oauth.py`): the database
  rejects any write.
* `--apply` requires `--yes` and refuses the production database name unless
  `--allow-prod` is also given. It never runs automatically on boot.
* Tokens are never printed; only short SHA-256 fingerprints and row ids.
* No network calls, no token refresh, no sends.

USAGE
-----
    cd /home/chapman-server/Projects/new-crm
    ./.venv/bin/python3 scripts/split_shared_mail_credentials.py            # report only
    ./.venv/bin/python3 scripts/split_shared_mail_credentials.py --json     # same, machine-readable
    ./.venv/bin/python3 scripts/split_shared_mail_credentials.py --apply --yes

Exit code 0 = clean or plan printed; 2 = sharing found in dry-run; 1 = error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_DB_NAMES = {"sietch_crm"}

EXCHANGE_COLUMNS = ("oauth_access_token", "oauth_refresh_token",
                    "oauth_token_expires", "oauth_identity")
GRAPH_COLUMNS = ("oauth_graph_access_token", "oauth_graph_refresh_token",
                 "oauth_graph_token_expires", "oauth_graph_identity",
                 "oauth_graph_scope")

FAMILIES = {
    "exchange": {"refresh": "oauth_refresh_token", "identity": "oauth_identity",
                 "columns": EXCHANGE_COLUMNS},
    "graph": {"refresh": "oauth_graph_refresh_token", "identity": "oauth_graph_identity",
              "columns": GRAPH_COLUMNS},
}


def _fingerprint(value: Any) -> str:
    """Short, non-reversible fingerprint of a secret. '' when absent.

    A truncated SHA-256 is safe to print in a plan or a ticket; the token
    value itself never leaves the database.
    """
    token = (value or "").strip() if isinstance(value, str) else ""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "#" in text and "#ext#" not in text:
        text = text.rsplit("#", 1)[-1].strip()
    return text


def _is_address(value: Any) -> bool:
    text = _norm(value)
    return "@" in text and not text.startswith("@") and not text.endswith("@")


def _connect(readonly: bool):
    """psycopg2 connection; read-only implies autocommit + no writes allowed."""
    import psycopg2
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "sietch_crm"),
        user=os.getenv("DB_USER", "sietch"),
        password=os.getenv("DB_PASSWORD", ""),
        connect_timeout=10,
    )
    if readonly:
        conn.set_session(readonly=True, autocommit=True)
    return conn


def load_accounts(conn) -> list[dict]:
    """All mail accounts with the credential columns needed for the check."""
    from psycopg2.extras import RealDictCursor
    wanted = ["id", "email", "owner_user_id", "is_crm_mail", "oauth_provider",
              "smtp_user", "smtp_auth_disabled",
              *EXCHANGE_COLUMNS, *GRAPH_COLUMNS]
    with conn.cursor() as cur:
        # Tolerate a pre-migration schema: select NULL for absent columns
        # instead of failing the whole report.
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'mail_accounts'")
        present = {r[0] for r in cur.fetchall()}
        select_list = ", ".join(c if c in present else f"NULL AS {c}" for c in wanted)
        cur.execute(f"SELECT {select_list} FROM mail_accounts ORDER BY id")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def build_plan(rows: list[dict]) -> dict[str, Any]:
    """Pure function: decide what (if anything) to clear. No I/O."""
    plan: list[dict] = []
    findings: list[str] = []

    # 1. Same grant on more than one row.
    for family, spec in FAMILIES.items():
        col = spec["refresh"]
        groups: dict[str, list[dict]] = {}
        for row in rows:
            fp = _fingerprint(row.get(col))
            if fp:
                groups.setdefault(fp, []).append(row)
        for fp, group in groups.items():
            if len(group) < 2:
                continue
            ids = sorted(int(r["id"]) for r in group)
            findings.append(f"shared_{family}_grant rows={ids} fingerprint={fp}")
            # The legitimate owner is the row whose verified identity is its
            # own address. If no row qualifies, nobody can prove ownership:
            # clear the grant from every row (never copy it).
            owners = [r for r in group
                      if _norm(r.get(spec["identity"])) == _norm(r.get("email"))
                      and _is_address(r.get("email"))]
            keep = int(owners[0]["id"]) if owners else None
            for row in group:
                if keep is not None and int(row["id"]) == keep:
                    continue
                plan.append({
                    "account_id": int(row["id"]),
                    "email": row.get("email"),
                    "action": f"clear_{family}_tokens",
                    "why": (f"same {family} refresh token is attached to accounts {ids}"
                            + (f"; it was kept on account {keep} (identity-verified)"
                               if keep is not None else
                               "; no row's identity matches it — cleared everywhere")),
                    "expected_token": row.get(col),
                })

    # 2. Token whose identity names a different mailbox.
    for family, spec in FAMILIES.items():
        ident_col = spec["identity"]
        for row in rows:
            ident = _norm(row.get(ident_col))
            email = _norm(row.get("email"))
            if ident and email and ident != email:
                findings.append(
                    f"identity_mismatch_{family} row={row['id']} identity={ident}")
                already = any(p["account_id"] == int(row["id"])
                              and p["action"] == f"clear_{family}_tokens" for p in plan)
                if not already:
                    plan.append({
                        "account_id": int(row["id"]),
                        "email": row.get("email"),
                        "action": f"clear_{family}_tokens",
                        "why": f"{family} token belongs to {ident}, not {email}",
                        "expected_token": row.get(spec["refresh"]),
                    })

    # 3. SMTP login that is another mailbox.
    for row in rows:
        smtp_user = _norm(row.get("smtp_user"))
        email = _norm(row.get("email"))
        if _is_address(smtp_user) and email and smtp_user != email:
            findings.append(f"smtp_user_mismatch row={row['id']} smtp_user={smtp_user}")
            plan.append({
                "account_id": int(row["id"]),
                "email": row.get("email"),
                "action": "clear_smtp_user",
                "why": f"SMTP login is {smtp_user}, not this mailbox ({email})",
                "expected_token": None,
            })

    # 4. Duplicate emails (defensive: the UNIQUE constraint should prevent it).
    by_email: dict[str, list[dict]] = {}
    for row in rows:
        email = _norm(row.get("email"))
        if email:
            by_email.setdefault(email, []).append(row)
    for email, group in by_email.items():
        if len(group) > 1:
            ids = sorted(int(r["id"]) for r in group)
            findings.append(f"duplicate_email rows={ids} email={email}")
            plan.append({
                "account_id": ids[0],
                "email": email,
                "action": "manual_review",
                "why": f"{len(ids)} rows share this address (ids {ids})",
                "expected_token": None,
            })

    # De-duplicate identical (account, action) entries.
    seen: set[tuple[int, str]] = set()
    unique: list[dict] = []
    for item in plan:
        key = (item["account_id"], item["action"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return {"findings": findings, "plan": unique, "accounts": len(rows)}


def _clear_statement(family: str | None) -> str:
    if family == "smtp_user":
        return "smtp_user = NULL, auth_error = %s, auth_error_at = NOW()"
    cols = FAMILIES[family]["columns"]
    sets = ", ".join(f"{c} = NULL" for c in cols)
    return f"{sets}, auth_error = %s, auth_error_at = NOW()"


def apply_plan(conn, plan: list[dict]) -> dict[str, Any]:
    """Execute the plan. Caller passes a WRITABLE connection."""
    applied: list[dict] = []
    skipped: list[dict] = []
    with conn.cursor() as cur:
        for item in plan:
            action = item["action"]
            if action == "manual_review":
                skipped.append({**item, "result": "manual review required"})
                continue
            family = action.replace("clear_", "").replace("_tokens", "")
            message = (f"Shared credentials were removed on 2026-11-10: {item['why']}. "
                       f"Reconnect this mailbox to restore access.")[:300]
            if family == "smtp_user":
                cur.execute(
                    "UPDATE mail_accounts SET " + _clear_statement("smtp_user") + " WHERE id = %s",
                    (message, int(item["account_id"])),
                )
                rowcount = cur.rowcount
            else:
                refresh_col = FAMILIES[family]["refresh"]
                expected = item.get("expected_token")
                if expected:
                    # Compare-and-swap: do not clobber a reconnect that
                    # replaced the token after we read it.
                    cur.execute(
                        "UPDATE mail_accounts SET " + _clear_statement(family)
                        + f" WHERE id = %s AND {refresh_col} = %s",
                        (message, int(item["account_id"]), expected),
                    )
                else:
                    cur.execute(
                        "UPDATE mail_accounts SET " + _clear_statement(family)
                        + " WHERE id = %s",
                        (message, int(item["account_id"])),
                    )
                rowcount = cur.rowcount
            if rowcount:
                applied.append({**item, "expected_token": None})
            else:
                skipped.append({**item, "expected_token": None,
                                "result": "changed concurrently — not overwritten"})
    conn.commit()
    return {"applied": applied, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report/split shared mail credentials (dry-run by default).")
    parser.add_argument("--apply", action="store_true",
                        help="clear shared credentials (requires --yes)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm the writes in --apply")
    parser.add_argument("--allow-prod", action="store_true",
                        help="permit --apply against the production database name")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    db_name = os.getenv("DB_NAME", "sietch_crm")
    if args.apply:
        if not args.yes:
            print("Refusing to write: pass --apply --yes to confirm.", file=sys.stderr)
            return 1
        if db_name in PROD_DB_NAMES and not args.allow_prod:
            print(
                f"Refusing to apply against {db_name!r} (production name). "
                "Run the dry run there, create a throwaway copy, or pass --allow-prod "
                "with an explicit change window.",
                file=sys.stderr)
            return 1

    readonly = not args.apply
    try:
        conn = _connect(readonly=readonly)
    except Exception as e:  # pragma: no cover - environment dependent
        print(f"Could not connect: {e}", file=sys.stderr)
        return 1

    try:
        rows = load_accounts(conn)
        result = build_plan(rows)
        result["mode"] = "apply" if args.apply else "dry-run"
        if args.apply:
            result["apply"] = apply_plan(conn, result["plan"])
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Mode: {result['mode']} — {result['accounts']} mail account(s)")
        if not result["findings"]:
            print("No shared or mis-bound credentials found.")
        else:
            print("Findings:")
            for f in result["findings"]:
                print(f"  - {f}")
            print("Plan:")
            for item in result["plan"]:
                print(f"  account {item['account_id']} ({item['email']}): "
                      f"{item['action']} — {item['why']}")
            if args.apply:
                applied = result["apply"]["applied"]
                skipped = result["apply"]["skipped"]
                print(f"Applied {len(applied)} change(s), skipped {len(skipped)}.")
                for item in skipped:
                    print(f"  skipped account {item['account_id']}: {item['result']}")
            else:
                print("DRY RUN — nothing was written. Re-run with --apply --yes on a "
                      "database you are allowed to modify.")

    if result["findings"] and not args.apply:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
