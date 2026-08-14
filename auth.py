"""Authentication, sessions, and password reset for Sietch CRM v3.0."""

from __future__ import annotations

import hashlib
import secrets
import time

from db import query, execute, insert_returning

SESSION_DURATION = 86400 * 7   # 7 days
RESET_DURATION = 3600          # 1 hour


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """PBKDF2 password hashing. Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return dk.hex(), salt.hex()


def verify_password(password: str, stored_hash: str, salt_hex: str) -> bool:
    """Verify a password against stored hash + salt."""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return secrets.compare_digest(dk.hex(), stored_hash)


def create_session(user_id: int, ip_address: str = "") -> str:
    """Create a new session and return the token."""
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + SESSION_DURATION
    execute(
        "INSERT INTO sessions (token, user_id, expires_at, ip_address) VALUES (%s, %s, to_timestamp(%s), %s)",
        (token, user_id, expires, ip_address),
    )
    return token


def get_session_user(token: str) -> dict | None:
    """Look up the user for a valid session token. Returns dict or None."""
    row = query(
        """SELECT u.id, u.email, u.display_name, u.first_name, u.last_name,
                  u.is_admin, u.must_change_password, u.role, u.twofa_method
           FROM sessions s JOIN users u ON s.user_id = u.id
           WHERE s.token = %s AND s.expires_at > NOW() AND u.is_active = TRUE""",
        (token,),
        fetch="one",
    )
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "display_name": row[2],
        "first_name": row[3],
        "last_name": row[4],
        "is_admin": row[5],
        "must_change_password": row[6],
        "role": row[7] or "member",
        "twofa_method": row[8],
    }


def destroy_session(token: str) -> None:
    """Delete a session."""
    execute("DELETE FROM sessions WHERE token = %s", (token,))


def destroy_all_user_sessions(user_id: int) -> None:
    """Delete all sessions for a user (force re-login everywhere)."""
    execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


def create_reset_token(user_id: int) -> str:
    """Create a password reset token and return it."""
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + RESET_DURATION
    execute(
        "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (%s, %s, to_timestamp(%s))",
        (token, user_id, expires),
    )
    return token


def verify_reset_token(token: str) -> int | None:
    """Verify a reset token and return user_id, or None if invalid/expired."""
    row = query(
        """SELECT user_id FROM password_reset_tokens
           WHERE token = %s AND expires_at > NOW() AND used_at IS NULL""",
        (token,),
        fetch="one",
    )
    return row[0] if row else None


def consume_reset_token(token: str) -> None:
    """Mark a reset token as used."""
    execute(
        "UPDATE password_reset_tokens SET used_at = NOW() WHERE token = %s",
        (token,),
    )


def set_password(user_id: int, password: str) -> None:
    """Set a new password for a user and clear must_change_password."""
    pw_hash, salt = hash_password(password)
    execute(
        "UPDATE users SET password_hash = %s, password_salt = %s, must_change_password = FALSE WHERE id = %s",
        (pw_hash, salt, user_id),
    )
    destroy_all_user_sessions(user_id)


def authenticate_user(email: str, password: str) -> dict | None:
    """Authenticate with email + password. Returns user dict or None."""
    row = query(
        """SELECT id, email, display_name, first_name, last_name,
                  is_admin, password_hash, password_salt, must_change_password,
                  role, twofa_method
           FROM users WHERE email = %s AND is_active = TRUE""",
        (email.lower().strip(),),
        fetch="one",
    )
    if not row:
        return None
    user_id, email_val, display_name, first_name, last_name, is_admin, pw_hash, pw_salt, must_change, role, twofa_method = row
    if not pw_hash or not pw_salt:
        return None
    if not verify_password(password, pw_hash, pw_salt):
        return None
    execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user_id,))
    return {
        "id": user_id,
        "email": email_val,
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "is_admin": is_admin,
        "must_change_password": must_change,
        "role": role or "member",
        "twofa_method": twofa_method,
    }


def get_user_by_email(email: str) -> dict | None:
    """Look up a user by email. Returns dict or None."""
    row = query(
        """SELECT id, email, display_name, is_admin, is_active
           FROM users WHERE email = %s""",
        (email.lower().strip(),),
        fetch="one",
    )
    if not row:
        return None
    return {"id": row[0], "email": row[1], "display_name": row[2], "is_admin": row[3], "is_active": row[4]}


# ── 2FA helpers ─────────────────────────────────────────────────────────────

TWOF_CODE_LENGTH = 6
TWOF_EXPIRY_SECONDS = 300  # 5 minutes


def generate_2fa_code() -> str:
    """Generate a random 6-digit code."""
    return str(secrets.randbelow(900000) + 100000)


def create_2fa_challenge(user_id: int, method: str) -> tuple[str, str]:
    """Create a 2FA challenge. Returns (token, code)."""
    token = secrets.token_urlsafe(32)
    code = generate_2fa_code()
    execute(
        """INSERT INTO twofa_challenges (token, user_id, code, method, expires_at)
           VALUES (%s, %s, %s, %s, to_timestamp(%s))""",
        (token, user_id, code, method, int(time.time()) + TWOF_EXPIRY_SECONDS),
    )
    return token, code


def verify_2fa_challenge(token: str, code: str) -> int | None:
    """Verify a 2FA challenge. Returns user_id if valid, None otherwise.
    Deletes the challenge on success."""
    row = query(
        """SELECT user_id FROM twofa_challenges
           WHERE token = %s AND code = %s AND expires_at > NOW()""",
        (token, code),
        fetch="one",
    )
    if not row:
        return None
    user_id = row[0]
    execute("DELETE FROM twofa_challenges WHERE token = %s", (token,))
    return user_id


def resend_2fa_challenge(token: str) -> tuple[str, str] | None:
    """Regenerate a 2FA code for an existing challenge. Returns (new_token, new_code) or None."""
    row = query(
        """SELECT user_id, method FROM twofa_challenges WHERE token = %s AND expires_at > NOW()""",
        (token,),
        fetch="one",
    )
    if not row:
        return None
    user_id, method = row
    # Delete old challenge
    execute("DELETE FROM twofa_challenges WHERE token = %s", (token,))
    # Create new one
    return create_2fa_challenge(user_id, method)


def cleanup_expired_2fa_challenges() -> int:
    """Delete expired 2FA challenges. Returns count deleted."""
    result = execute("DELETE FROM twofa_challenges WHERE expires_at < NOW()")
    return result if result else 0


def set_2fa_method(user_id: int, method: str | None) -> None:
    """Set or clear a user's 2FA method."""
    execute("UPDATE users SET twofa_method = %s WHERE id = %s", (method, user_id))
