#!/usr/bin/env python3
"""READ-ONLY diagnostic: which identity is attached to which mail account?

WHY
---
Live bug 2026-11-09: the settings page for estimates.masterx@outlook.com
(account 8) showed "Connected as mdm.estimates@outlook.com via Microsoft
Graph", reconnecting refused with the mismatch error, and every Outlook
account went `auth_failed`. Before changing anything we need to see the
actual row-level mapping.

WHAT IT DOES
------------
SELECT-only. It never refreshes, rotates, revokes or prints a token. For
every mail account it prints:

  id, email, provider
  oauth_identity          (Exchange/IMAP token's signed-in mailbox)
  oauth_graph_identity    (Graph token's signed-in mailbox)
  token expiry + whether a refresh token is on file (yes/no, never the value)
  last persisted OAuth/Graph error + when it happened
  smtp_user / smtp_auth_disabled (an SMTP send uses smtp_user, not From)
  a SHORT SHA-256 fingerprint of each refresh token

The fingerprint is the point: if two accounts show the SAME fingerprint they
hold the same Microsoft grant, and Microsoft rotates a refresh token on use —
so the first account to refresh silently invalidates the other one. That is
how "all Outlook accounts lost access at once" happens. A truncated hash of
a secret is not reversible and is safe to paste into a ticket.

Exit code is 0 even when problems are found (it is a report, not a gate);
`--json` emits machine-readable output.

USAGE (run on the CRM host, read-only, does not touch live tokens)
-----------------------------------------------------------------
    cd /home/chapman-server/Projects/new-crm
    ./.venv/bin/python3 scripts/diagnose_mail_oauth.py
    ./.venv/bin/python3 scripts/diagnose_mail_oauth.py --json
    ./.venv/bin/python3 scripts/diagnose_mail_oauth.py --account 8 --account 9
    ./.venv/bin/python3 scripts/diagnose_mail_oauth.py --check-client-secret

`--check-client-secret` performs ONE client_credentials token request against
Microsoft to prove whether OAUTH_MICROSOFT_CLIENT_SECRET is still valid (an
expired secret breaks every account at once). It prints only the AADSTS error
code / HTTP status, never a token, and does not touch any mailbox grant.

Never run this against a database you are not allowed to read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fingerprint(value: str | None) -> str:
    """Short, non-reversible fingerprint of a secret. '' when absent."""
    token = (value or "").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _ts(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _norm(value) -> str:
    text = str(value or "").strip().lower()
    if "#" in text and "#ext#" not in text:
        text = text.rsplit("#", 1)[-1].strip()
    return text


def _readonly_conn():
    """Open a psycopg2 connection that CANNOT write.

    Deliberately not `db.init_db()`: that also runs the migration runner, and
    this script must stay read-only. `set_session(readonly=True)` makes the
    database itself reject any write from this connection.
    """
    import psycopg2

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "sietch_crm"),
        user=os.getenv("DB_USER", "sietch"),
        password=os.getenv("DB_PASSWORD", ""),
        connect_timeout=10,
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


def load_accounts(account_ids: list[int] | None) -> list[dict]:
    """Read the mapping. Tolerates a pre-migration-047 schema.

    Ken may run this on production BEFORE deploying the fix, so every column
    added by migration 047 is selected as `NULL AS ...` when it does not exist
    yet instead of failing the whole report.
    """
    from psycopg2.extras import RealDictCursor

    wanted = [
        "id", "email", "owner_user_id", "is_crm_mail", "sync_enabled", "oauth_provider",
        "oauth_identity", "oauth_token_expires", "oauth_last_error", "oauth_last_error_at",
        "oauth_access_token", "oauth_refresh_token",
        "oauth_graph_identity", "oauth_graph_token_expires",
        "oauth_graph_last_error", "oauth_graph_last_error_at",
        "oauth_graph_access_token", "oauth_graph_refresh_token",
        "smtp_user", "smtp_auth_disabled",
    ]
    conn = _readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'mail_accounts'")
            present = {r[0] for r in cur.fetchall()}
        select_list = ", ".join(
            (col if col in present else f"NULL AS {col}") for col in wanted)
        sql = f"SELECT {select_list} FROM mail_accounts"
        params: tuple = ()
        if account_ids:
            sql += " WHERE id = ANY(%s)"
            params = (list(account_ids),)
        sql += " ORDER BY id"
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or None)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def analyze(rows: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    accounts: list[dict] = []
    fingerprints: dict[str, list[int]] = {}

    for r in rows:
        email = (r.get("email") or "").strip()
        oauth_identity = (r.get("oauth_identity") or "").strip()
        graph_identity = (r.get("oauth_graph_identity") or "").strip()
        exch_fp = _fingerprint(r.get("oauth_refresh_token"))
        graph_fp = _fingerprint(r.get("oauth_graph_refresh_token"))
        for fp, label in ((exch_fp, "exchange"), (graph_fp, "graph")):
            if fp:
                fingerprints.setdefault(f"{label}:{fp}", []).append(r["id"])

        def _expired(value) -> bool | None:
            if not isinstance(value, datetime):
                return None
            dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return dt <= now

        problems: list[str] = []
        if not email:
            problems.append("blank email address (row created by a failed OAuth flow)")
        if oauth_identity and _norm(oauth_identity) != _norm(email):
            problems.append(
                f"Exchange token belongs to {oauth_identity}, not {email} — WRONG ACCOUNT")
        if graph_identity and _norm(graph_identity) != _norm(email):
            problems.append(
                f"Graph token belongs to {graph_identity}, not {email} — WRONG ACCOUNT")
        if r.get("oauth_provider") and not oauth_identity:
            problems.append("Exchange identity not recorded (connected before migration 047)")
        if r.get("oauth_graph_access_token") and not graph_identity:
            problems.append("Graph token has no verified identity")
        if r.get("oauth_provider") and _expired(r.get("oauth_token_expires")) and not exch_fp:
            problems.append("Exchange token expired and no refresh token on file → auth_failed")
        if (r.get("oauth_graph_access_token")
                and _expired(r.get("oauth_graph_token_expires")) and not graph_fp):
            problems.append("Graph token expired and no refresh token on file")
        if r.get("oauth_provider") and r.get("smtp_user") and _norm(r.get("smtp_user")) != _norm(email):
            problems.append(
                f"smtp_user is {r.get('smtp_user')} — SMTP would authenticate as another mailbox")
        if r.get("oauth_last_error"):
            problems.append(f"last Exchange refresh error: {r['oauth_last_error']}")
        if r.get("oauth_graph_last_error"):
            problems.append(f"last Graph refresh error: {r['oauth_graph_last_error']}")

        accounts.append({
            "id": r["id"],
            "email": email,
            "owner_user_id": r.get("owner_user_id"),
            "is_crm_mail": bool(r.get("is_crm_mail")),
            "sync_enabled": bool(r.get("sync_enabled")),
            "provider": r.get("oauth_provider") or "password",
            "oauth_identity": oauth_identity,
            "graph_identity": graph_identity,
            "exchange_expires": _ts(r.get("oauth_token_expires")),
            "exchange_expired": _expired(r.get("oauth_token_expires")),
            "has_exchange_refresh": bool(exch_fp),
            "exchange_refresh_fingerprint": exch_fp,
            "graph_expires": _ts(r.get("oauth_graph_token_expires")),
            "graph_expired": _expired(r.get("oauth_graph_token_expires")),
            "has_graph_refresh": bool(graph_fp),
            "graph_refresh_fingerprint": graph_fp,
            "oauth_last_error": r.get("oauth_last_error") or "",
            "oauth_last_error_at": _ts(r.get("oauth_last_error_at")),
            "graph_last_error": r.get("oauth_graph_last_error") or "",
            "graph_last_error_at": _ts(r.get("oauth_graph_last_error_at")),
            "smtp_user": r.get("smtp_user") or "",
            "smtp_auth_disabled": bool(r.get("smtp_auth_disabled")),
            "problems": problems,
        })

    shared = {fp: ids for fp, ids in fingerprints.items() if len(ids) > 1}
    for fp, ids in shared.items():
        for acct in accounts:
            if acct["id"] in ids:
                acct["problems"].append(
                    "SHARED GRANT: the same refresh token is stored on accounts "
                    + ", ".join(str(i) for i in ids)
                    + " — the next refresh on one of them invalidates the others")

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "read_only": True,
        "accounts": accounts,
        "shared_grants": shared,
        "problem_accounts": [a["id"] for a in accounts if a["problems"]],
    }


def check_client_secret() -> dict:
    """Prove whether the OAuth client secret is still valid.

    A client_credentials request uses the APP identity only: it neither
    consumes nor rotates any mailbox refresh token. Only the outcome/error
    code is reported.
    """
    client_id = os.environ.get("OAUTH_MICROSOFT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OAUTH_MICROSOFT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return {"checked": False, "reason": "OAUTH_MICROSOFT_CLIENT_ID/SECRET not set"}
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token", data=body)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read(1)
            return {"checked": True, "ok": True, "http": resp.status,
                    "verdict": "client secret is valid"}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        code = ""
        try:
            code = json.loads(raw).get("error", "")
            desc = json.loads(raw).get("error_description", "")
            aadsts = next((part for part in desc.replace(",", " ").split()
                           if part.startswith("AADSTS")), "")
        except Exception:
            aadsts = ""
        verdict = {
            "invalid_client": "client secret is EXPIRED or wrong (AADSTS7000222 / 7000215) "
                              "— every mailbox refresh fails at once",
            "unauthorized_client": "app cannot use client_credentials (expected for a "
                                   "delegated-only app; not proof of a bad secret)",
        }.get(code, f"token endpoint rejected the request ({code or 'unknown'})")
        return {"checked": True, "ok": False, "http": e.code, "error": code,
                "aadsts": aadsts, "verdict": verdict}
    except Exception as e:
        return {"checked": False, "reason": f"network error: {e}"}


def render(report: dict, secret: dict | None) -> str:
    lines: list[str] = []
    lines.append("MAIL OAUTH ACCOUNT BINDING — READ-ONLY DIAGNOSTIC")
    lines.append(f"generated: {report['generated_at']}   (no token values are printed)")
    lines.append("")
    header = f"{'id':>4}  {'email':38} {'provider':9} {'exchange identity':38} {'graph identity':38} refresh  expires"
    lines.append(header)
    lines.append("-" * len(header))
    for a in report["accounts"]:
        refresh = ("E:" + ("y" if a["has_exchange_refresh"] else "-")) + \
                  (" G:" + ("y" if a["has_graph_refresh"] else "-"))
        expires = a["exchange_expires"] or a["graph_expires"] or ""
        lines.append(
            f"{a['id']:>4}  {(a['email'] or '(blank)'):38.38} {a['provider']:9} "
            f"{(a['oauth_identity'] or '-'):38.38} {(a['graph_identity'] or '-'):38.38} "
            f"{refresh:8} {expires}"
        )
    lines.append("")
    for a in report["accounts"]:
        if a["exchange_refresh_fingerprint"] or a["graph_refresh_fingerprint"]:
            lines.append(
                f"  account {a['id']} token fingerprints: exchange="
                f"{a['exchange_refresh_fingerprint'] or '-'} graph="
                f"{a['graph_refresh_fingerprint'] or '-'}")
    lines.append("")
    if report["shared_grants"]:
        lines.append("!! SHARED GRANTS (same refresh token on several accounts):")
        for fp, ids in report["shared_grants"].items():
            lines.append(f"   {fp} → accounts {', '.join(str(i) for i in ids)}")
    else:
        lines.append("shared grants: none (every account holds its own grant)")
    lines.append("")
    if report["problem_accounts"]:
        lines.append("!! PROBLEMS")
        for a in report["accounts"]:
            if not a["problems"]:
                continue
            lines.append(f"  account {a['id']} ({a['email'] or 'blank'}):")
            for p in a["problems"]:
                lines.append(f"    - {p}")
    else:
        lines.append("problems: none")
    if secret is not None:
        lines.append("")
        lines.append(f"client secret check: {json.dumps(secret)}")
    lines.append("")
    lines.append("Recovery order (per affected account, one at a time):")
    lines.append("  1. Settings → Disconnect OAuth (Exchange) and Disconnect Graph.")
    lines.append("  2. Reconnect OAuth while signed in as THAT mailbox (check the identity")
    lines.append("     shown in the account picker before approving).")
    lines.append("  3. Reconnect Graph the same way. The status line must read")
    lines.append("     'Connected as <that mailbox>' — anything else means stop.")
    lines.append("  4. Only then reconnect the next account.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only report of which OAuth identity is attached to each mail account.")
    parser.add_argument("--account", action="append", type=int, default=None,
                        help="limit to an account id (repeatable)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--check-client-secret", action="store_true",
                        help="also verify the Microsoft app secret (no mailbox grant is touched)")
    args = parser.parse_args(argv)

    try:
        rows = load_accounts(args.account)
    except Exception as e:
        print(f"ERROR: could not read mail_accounts: {e}", file=sys.stderr)
        return 2

    report = analyze(rows)
    secret = check_client_secret() if args.check_client_secret else None
    if args.json:
        payload = dict(report)
        if secret is not None:
            payload["client_secret_check"] = secret
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render(report, secret))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
