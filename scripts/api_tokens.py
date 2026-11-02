#!/usr/bin/env python3
"""Manage personal read-only API tokens for Sietch CRM.

Tokens authorize `Authorization: Bearer <token>` on /api/v2 GET routes
(read-only; non-GET and auth/admin/user/settings routes are refused).
Only a SHA-256 hash is stored — the plaintext is shown once at creation.

Usage (run on the host / droplet with the CRM .env present):
    python3 scripts/api_tokens.py create --name "bulk pull" --admin me@example.com
    python3 scripts/api_tokens.py list
    python3 scripts/api_tokens.py revoke --id 3
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ── Load .env (same pattern as server.py) ────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()

sys.path.insert(0, str(ROOT))
import db  # noqa: E402
import api_tokens  # noqa: E402


def _admin_id(email: str) -> int:
    row = db.query_one(
        "SELECT id, is_admin FROM users WHERE email = %s AND is_active = TRUE",
        (email,),
    )
    if not row:
        print(f"Error: no active user found with email '{email}'", file=sys.stderr)
        sys.exit(1)
    if not row.get("is_admin"):
        print(f"Error: user '{email}' is not an admin (only admins own tokens)", file=sys.stderr)
        sys.exit(1)
    return row["id"]


def _cmd_create(args: argparse.Namespace) -> None:
    admin_id = _admin_id(args.admin)
    try:
        rec = api_tokens.create_api_token(args.name, admin_id, args.scope)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Token id:   {rec['id']}")
    print(f"Name:       {rec['name']}")
    print(f"Scope:      {rec['scope']}")
    print(f"Token:      {rec['token']}")
    print("Store this token now — it is shown only once and cannot be recovered.")
    print("Use as: curl -H \"Authorization: Bearer <token>\" https://<crm>/api/v2/projects")


def _cmd_list(args: argparse.Namespace) -> None:
    tokens = api_tokens.list_api_tokens(include_revoked=not args.active_only)
    if not tokens:
        print("No API tokens.")
        return
    print(f"{'ID':>4}  {'NAME':<24} {'PREFIX':<14} {'SCOPE':<6} {'CREATED_BY':<28} {'LAST_USED':<20} {'STATUS'}")
    for t in tokens:
        status = "REVOKED" if t["revoked"] else "active"
        print(f"{t['id']:>4}  {t['name']:<24} {t['token_prefix']:<14} {t['scope']:<6} "
              f"{t['created_by_email'] or t['created_by'] or '-':<28} {t['last_used_at'] or '-':<20} {status}")


def _cmd_revoke(args: argparse.Namespace) -> None:
    if api_tokens.revoke_api_token(args.id):
        print(f"Token {args.id} revoked.")
    else:
        print(f"Error: token {args.id} not found or already revoked.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Sietch CRM read-only API tokens")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Mint a new token (plaintext shown once)")
    p_create.add_argument("--name", required=True, help="Token label, e.g. 'bulk pull'")
    p_create.add_argument("--admin", required=True, help="Admin email that owns the token")
    p_create.add_argument("--scope", default="read", help="Token scope (only 'read' supported)")
    p_create.set_defaults(func=_cmd_create)

    p_list = sub.add_parser("list", help="List tokens (hashes never shown)")
    p_list.add_argument("--active-only", action="store_true", help="Hide revoked tokens")
    p_list.set_defaults(func=_cmd_list)

    p_revoke = sub.add_parser("revoke", help="Revoke a token by id")
    p_revoke.add_argument("--id", required=True, type=int, help="Token id from list")
    p_revoke.set_defaults(func=_cmd_revoke)

    args = parser.parse_args()
    db.init_db()
    args.func(args)


if __name__ == "__main__":
    main()
