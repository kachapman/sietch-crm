"""Personal API tokens for Sietch CRM.

Default is full access: the token acts as the creating admin and may call
any /api/v2 method (GET/POST/PUT/PATCH/DELETE) that admin session auth
would allow. Optional restrictions are chosen at creation in the admin
console (read-only, method allowlist, resource allowlist, expiry).

Only a SHA-256 hash of the secret is stored; plaintext is shown once at
creation. Write requests authorized by a token are recorded in
api_token_audit_log (token id, method, path, time).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

import db

logger = logging.getLogger("api_tokens")

SCOPE_FULL = "full"
SCOPE_READ = "read"
SCOPES = frozenset({SCOPE_FULL, SCOPE_READ})
TOKEN_PREFIX = "sietch_"
LAST_USED_UPDATE_SEC = 60

# Resource keys Ken can optionally select at creation. Empty/omitted = all.
RESOURCE_PREFIXES: dict[str, tuple[str, ...]] = {
    "projects": ("/api/v2/projects",),
    "notes": ("/api/v2/notes",),
    "contacts": ("/api/v2/contacts",),
    "documents": ("/api/v2/documents",),
    "tasks": ("/api/v2/tasks",),
    "mail": ("/api/v2/mail",),
    "calendar": ("/api/v2/calendar",),
    "admin": ("/api/v2/admin",),
    "users": ("/api/v2/users",),
    "me": ("/api/v2/me", "/api/v2/my"),
    "history": ("/api/v2/history",),  # plus .../history under projects (see matcher)
    "reference": ("/api/v2/reference",),
}

ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _normalize_restrictions(raw: Any) -> dict:
    """Return a sanitized restrictions dict. Unknown keys dropped."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"restrictions must be a JSON object: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("restrictions must be an object")
    out: dict[str, Any] = {}
    methods = raw.get("methods")
    if methods is not None:
        if not isinstance(methods, (list, tuple)):
            raise ValueError("restrictions.methods must be a list")
        cleaned = []
        for m in methods:
            m = str(m).strip().upper()
            if m not in ALLOWED_METHODS:
                raise ValueError(f"unknown method in restrictions: {m}")
            if m not in cleaned:
                cleaned.append(m)
        out["methods"] = cleaned
    resources = raw.get("resources")
    if resources is not None:
        if not isinstance(resources, (list, tuple)):
            raise ValueError("restrictions.resources must be a list")
        cleaned_r = []
        for r in resources:
            key = str(r).strip().lower()
            if key not in RESOURCE_PREFIXES:
                raise ValueError(
                    f"unknown resource {key!r}; choose from: "
                    + ", ".join(sorted(RESOURCE_PREFIXES))
                )
            if key not in cleaned_r:
                cleaned_r.append(key)
        out["resources"] = cleaned_r
    return out


def _parse_expires_at(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def create_api_token(
    name: str,
    created_by: int,
    scope: str = SCOPE_FULL,
    *,
    restrictions: Any = None,
    expires_at: Any = None,
) -> dict:
    """Mint a token. Returns the record INCLUDING the plaintext (show once)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    scope = (scope or SCOPE_FULL).strip().lower()
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (supported: full, read)")
    restrictions_obj = _normalize_restrictions(restrictions)
    # read scope implies GET-only even if methods restriction omitted
    if scope == SCOPE_READ and "methods" not in restrictions_obj:
        restrictions_obj = {**restrictions_obj, "methods": ["GET", "HEAD", "OPTIONS"]}
    expires = _parse_expires_at(expires_at)
    if expires is not None and expires <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")

    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = db.query_one(
        """INSERT INTO api_tokens
             (name, token_hash, token_prefix, scope, created_by, expires_at, restrictions)
           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
           RETURNING id, name, token_prefix, scope, created_by, created_at,
                     expires_at, restrictions""",
        (
            name,
            _hash_token(plaintext),
            plaintext[:12],
            scope,
            created_by,
            expires,
            json.dumps(restrictions_obj),
        ),
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "token": plaintext,
        "token_prefix": row["token_prefix"],
        "scope": row["scope"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
        "restrictions": row.get("restrictions") or {},
    }


def list_api_tokens(include_revoked: bool = True) -> list[dict]:
    """List tokens for the admin console. Hashes are never returned."""
    sql = """SELECT t.id, t.name, t.token_prefix, t.scope, t.created_by,
                    u.email AS created_by_email,
                    t.created_at, t.last_used_at, t.revoked_at,
                    t.expires_at, t.restrictions
             FROM api_tokens t
             LEFT JOIN users u ON u.id = t.created_by"""
    if not include_revoked:
        sql += " WHERE t.revoked_at IS NULL"
    sql += " ORDER BY t.id"
    out = []
    for r in db.query_dicts(sql):
        restrictions = r.get("restrictions") or {}
        if isinstance(restrictions, str):
            try:
                restrictions = json.loads(restrictions)
            except json.JSONDecodeError:
                restrictions = {}
        out.append({
            "id": r["id"],
            "name": r["name"],
            "token_prefix": r["token_prefix"],
            "scope": r["scope"],
            "created_by": r["created_by"],
            "created_by_email": r.get("created_by_email"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "last_used_at": r["last_used_at"].isoformat() if r.get("last_used_at") else None,
            "revoked_at": r["revoked_at"].isoformat() if r.get("revoked_at") else None,
            "revoked": r.get("revoked_at") is not None,
            "expires_at": r["expires_at"].isoformat() if r.get("expires_at") else None,
            "restrictions": restrictions,
        })
    return out


def revoke_api_token(token_id: int) -> bool:
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

    Returns a session-shaped user dict tagged with api_token_id/name/scope/
    restrictions, or None for unknown/revoked/expired tokens.
    """
    if not plaintext or not isinstance(plaintext, str):
        return None
    row = db.query_one(
        """SELECT t.id AS token_id, t.name AS token_name, t.scope,
                  t.last_used_at, t.expires_at, t.restrictions,
                  u.id, u.email, u.display_name, u.first_name, u.last_name,
                  u.is_admin, u.must_change_password, u.role, u.twofa_method
           FROM api_tokens t
           JOIN users u ON u.id = t.created_by
           WHERE t.token_hash = %s AND t.revoked_at IS NULL
             AND u.is_active = TRUE""",
        (_hash_token(plaintext.strip()),),
    )
    if not row:
        return None
    expires = row.get("expires_at")
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return None
    restrictions = row.get("restrictions") or {}
    if isinstance(restrictions, str):
        try:
            restrictions = json.loads(restrictions)
        except json.JSONDecodeError:
            restrictions = {}
    try:
        db.execute(
            """UPDATE api_tokens SET last_used_at = NOW() WHERE id = %s
               AND (last_used_at IS NULL OR last_used_at < NOW() - make_interval(secs => %s))""",
            (row["token_id"], LAST_USED_UPDATE_SEC),
        )
    except Exception as exc:
        logger.debug("api_tokens last_used_at update failed: %s", exc)
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "is_admin": row["is_admin"],
        "must_change_password": row["must_change_password"],
        "role": row["role"] or "member",
        "twofa_method": row["twofa_method"],
        "api_token_id": row["token_id"],
        "api_token_name": row["token_name"],
        "api_token_scope": row["scope"] or SCOPE_FULL,
        "api_token_restrictions": restrictions,
    }


def request_allowed(user: dict, method: str, api_path: str) -> tuple[bool, str]:
    """Check optional restrictions for an already-verified token user.

    Full scope with empty restrictions → allow. Read scope / method /
    resource allowlists are enforced here. Returns (ok, error_message).
    """
    method = (method or "GET").upper()
    scope = (user.get("api_token_scope") or SCOPE_FULL).lower()
    restrictions = user.get("api_token_restrictions") or {}

    allowed_methods = restrictions.get("methods")
    if scope == SCOPE_READ and not allowed_methods:
        allowed_methods = ["GET", "HEAD", "OPTIONS"]
    if allowed_methods:
        if method not in {m.upper() for m in allowed_methods}:
            return False, f"API token does not allow {method}"
    elif scope == SCOPE_READ and method not in ("GET", "HEAD", "OPTIONS"):
        return False, "API token is read-only (GET only)"

    resources = restrictions.get("resources")
    if resources:
        if not _path_matches_resources(api_path, resources):
            return False, "API token cannot access this resource"

    return True, ""


def _path_matches_resources(api_path: str, resources: list) -> bool:
    path = api_path or ""
    for key in resources:
        prefixes = RESOURCE_PREFIXES.get(str(key).lower(), ())
        for prefix in prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                return True
        if str(key).lower() == "history" and "/history" in path:
            return True
        # project-nested documents/notes/tasks/photos often live under /projects/{id}/…
        if str(key).lower() == "documents" and "/documents" in path:
            return True
        if str(key).lower() == "notes" and ("/notes" in path or path.startswith("/api/v2/notes")):
            return True
        if str(key).lower() == "tasks" and "/tasks" in path:
            return True
    return False


def log_token_write(token_id: int, method: str, path: str) -> None:
    """Append a write audit row. Failures never break the request."""
    if (method or "").upper() not in WRITE_METHODS:
        return
    try:
        db.execute(
            """INSERT INTO api_token_audit_log (token_id, method, path)
               VALUES (%s, %s, %s)""",
            (int(token_id), (method or "").upper(), (path or "")[:2000]),
        )
    except Exception as exc:
        logger.debug("api_token_audit_log insert failed: %s", exc)
