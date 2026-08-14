#!/usr/bin/env python3
"""CLI password recovery for Sietch CRM.

Usage:
    python3 scripts/recover_password.py --email user@example.com
    python3 scripts/recover_password.py --email user@example.com --password MyNewPass123
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
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

# ── Password hashing (matches auth.py) ───────────────────────────────────────


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """PBKDF2 password hashing. Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return dk.hex(), salt.hex()


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset a user's password")
    parser.add_argument("--email", required=True, help="User email address")
    parser.add_argument("--password", default=None, help="New password (random 12-char if omitted)")
    args = parser.parse_args()

    import psycopg2

    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "sietch_crm"),
        user=os.environ.get("DB_USER", "sietch"),
        password=os.environ.get("DB_PASSWORD", ""),
    )
    conn.autocommit = True
    cur = conn.cursor()

    # Look up user
    cur.execute("SELECT id, email FROM users WHERE email = %s", (args.email,))
    row = cur.fetchone()
    if not row:
        print(f"Error: no user found with email '{args.email}'", file=sys.stderr)
        sys.exit(1)

    user_id, email = row

    # Hash new password
    new_password = args.password or secrets.token_urlsafe(9)[:12]
    pw_hash, pw_salt = hash_password(new_password)

    # Update password + force change on next login
    cur.execute(
        "UPDATE users SET password_hash = %s, password_salt = %s, must_change_password = TRUE WHERE id = %s",
        (pw_hash, pw_salt, user_id),
    )

    # Destroy all active sessions for this user
    cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    deleted = cur.rowcount

    cur.close()
    conn.close()

    print(f"User: {email}")
    print(f"Temp password: {new_password}")
    print(f"Password has been set. User must change it on first login.")
    if deleted:
        print(f"({deleted} active session(s) destroyed.)")


if __name__ == "__main__":
    main()
