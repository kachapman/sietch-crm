"""Personal read-only API tokens for Sietch CRM.

Lets an admin mint long-lived Bearer tokens for bulk-read jobs (e.g. a pull
script that archives every deal + file). Tokens are read-only by design:
server.py rejects any non-GET request carrying one and blocks them from
auth / user-management / token-management / settings / 2FA routes entirely.

Only a SHA-256 hash of the secret is stored; the plaintext is shown once at
creation and cannot be recovered afterwards. `last_used_at` is refreshed at
most once a minute (a single conditional UPDATE) so bulk pulls don't pay a
write per request.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

import db

logger = logging.getLogger("api_tokens")

SCOPE_READ = "read"
TOKEN_PREFIX = "sietch_"
LAST_USED_UPDATE_SEC = 60


def _hash_token(plaintext: str) -> str:
    """SHA-256 hex of the full plaintext token (what is stored)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def create_api_token(name: str, created_by: int, scope: str = SCOPE_READ) -> dict:
    """Mint a token. Returns the record INCLUDING the plaintext (show once).

    Raises ValueError for a blank name or an unknown scope — the only
    scope today is "read".
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    if scope != SCOPE_READ:
        raise ValueError(f"unknown scope: {scope!r} (only 'read' is supported)")
    # 32 random bytes (url-safe ~43 chars) + identifying prefix. The prefix
    # lets logs/admin listings show which token was used without exposing
    # anything verifiable; verification always uses the full hash.
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = db.query_one(
        """INSERT INTO api_tokens (name, token_hash, token_prefix, scope, created_by)
           VALUES (%s, %s, %s, %s, %s)
           RETURNING id, name, token_prefix, scope, created_by, created_at""",
        (name, _hash_token(plaintext), plaintext[:12], scope, created_by),
    )
    return {
        "id": row["id"], "name": row["name"], "token": plaintext,
        "token_prefix": row["token_prefix"], "scope": row["scope"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def list_api_tokens(include_revoked: bool = True) -> list[dict]:
    """List tokens for the admin console. Hashes are never returned."""
    sql = """SELECT t.id, t.name, t.token_prefix, t.scope, t.created_by,
                    u.email AS created_by_email,
                    t.created_at, t.last_used_at, t.revoked_at
             FROM api_tokens t
             LEFT JOIN users u ON u.id = t.created_by"""
    if not include_revoked:
        sql += " WHERE t.revoked_at IS NULL"
    sql += " ORDER BY t.id"
    out = []
    for r in db.query_dicts(sql):
        out.append({
            "id": r["id"], "name": r["name"], "token_prefix": r["token_prefix"],
            "scope": r["scope"], "created_by": r["created_by"],
            "created_by_email": r.get("created_by_email"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "last_used_at": r["last_used_at"].isoformat() if r.get("last_used_at") else None,
            "revoked_at": r["revoked_at"].isoformat() if r.get("revoked_at") else None,
            "revoked": r.get("revoked_at") is not None,
        })
    return out


def revoke_api_token(token_id: int) -> bool:
    """Revoke a token. True if a live token was revoked, False otherwise."""
    try:
        tid = int(token_id)
    except (TypeError, ValueError):
        return False
    return db.execute(
        "UPDATE api_tokens SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL",
        (tid,),
    ) > 0


def verify_api_token(plaintext: str) -> dict | None:
    """Validate a presented Bearer token.

    Returns a session-shaped user dict (the creating admin's identity, so
    per-user visibility rules behave exactly like the UI) tagged with
    api_token_id/name, or None for unknown/revoked/wrong-scope tokens.
    Inactive creator accounts fail closed. `last_used_at` is refreshed at
    most once a minute; a failed bookkeeping write never fails auth.
    """
    if not plaintext or not isinstance(plaintext, str):
        return None
    row = db.query_one(
        """SELECT t.id AS token_id, t.name AS token_name, t.scope,
                  t.last_used_at,
                  u.id, u.email, u.display_name, u.first_name, u.last_name,
                  u.is_admin, u.must_change_password, u.role, u.twofa_method
           FROM api_tokens t
           JOIN users u ON u.id = t.created_by
           WHERE t.token_hash = %s AND t.revoked_at IS NULL
             AND u.is_active = TRUE""",
        (_hash_token(plaintext.strip()),),
    )
    if not row or row.get("scope") != SCOPE_READ:
        return None
    try:
        db.execute(
            """UPDATE api_tokens SET last_used_at = NOW() WHERE id = %s
               AND (last_used_at IS NULL OR last_used_at < NOW() - make_interval(secs => %s))""",
            (row["token_id"], LAST_USED_UPDATE_SEC),
        )
    except Exception as exc:  # bookkeeping must never break a read
        logger.debug("api_tokens last_used_at update failed: %s", exc)
    return {
        "id": row["id"], "email": row["email"],
        "display_name": row["display_name"], "first_name": row["first_name"],
        "last_name": row["last_name"], "is_admin": row["is_admin"],
        "must_change_password": row["must_change_password"],
        "role": row["role"] or "member", "twofa_method": row["twofa_method"],
        "api_token_id": row["token_id"], "api_token_name": row["token_name"],
    }
