"""Shared OAuth access-token refresh for mail accounts.

Stdlib-only apart from `db` (imported lazily) so it can be used by BOTH
`server.py` (IMAP flag sync, send path) and `scanner/mail_scanner.py`
(scan loop) without either of them owning the refresh logic.

Why this module exists (live bug 2026-11-03): the scanner refreshed OAuth
tokens on its own cycle and the server used the stored token as-is. When the
stored token expired, "mark as read" in the mail modal silently did nothing
in Outlook. Two refresh paths also raced each other: whichever saved last
won, and the loser kept using a token that had already been replaced.

Concurrency: the save is a compare-and-swap on the token that was read
(`UPDATE ... WHERE id = %s AND oauth_access_token = %s`). Only the refresher
whose read is still current wins; every other refresher sees rowcount == 0
and re-reads the row instead of overwriting a newer token. This works
across processes, so it does not depend on an in-process lock.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

import db
import oauth_providers
import mail_dates
import mail_oauth_binding

logger = logging.getLogger("sietch.mail_oauth")

# Refresh a token when it expires within this many seconds (matches the
# scanner's original 300s safety margin).
REFRESH_MARGIN_SECONDS = 300

# Serializes refreshes inside one process. The compare-and-swap in
# _save_refreshed_token() is what actually protects against a cross-process
# race; this lock just avoids two threads here minting two tokens needlessly.
_refresh_lock = threading.Lock()


def _now_ts() -> float:
    return time.time()


def token_needs_refresh(expires_at: datetime | None) -> bool:
    """True when the access token is expired or close enough to expiry.

    `mail_accounts.oauth_token_expires` is a plain TIMESTAMP (naive UTC), so
    `expires_at.timestamp()` would interpret it in the PROCESS timezone. That
    is UTC inside the production container but not on a host running with
    TZ=America/New_York, where the same row would look 4-5 hours off and be
    refreshed far too early (or used long after it expired). Normalize to UTC
    first (live bug 2026-11-09, same class as the mail display bug).
    """
    if expires_at is None:
        # Unknown expiry — assume it needs a refresh so a stale token is
        # never used. (The refresh itself is a no-op when there is no
        # refresh token.)
        return True
    if not isinstance(expires_at, datetime):
        return True
    expires = mail_dates.to_utc(expires_at)
    return expires.timestamp() <= _now_ts() + REFRESH_MARGIN_SECONDS


def _save_refreshed_token(account_id: int, new_access: str, new_refresh: str,
                          expires_at_ts: float, expected_access: str | None,
                          identity: str | None = None) -> bool:
    """Persist a refreshed token, but only if the row still holds the token
    we based the refresh on.

    Compare-and-swap on `oauth_access_token`: when another refresher (the
    scanner vs the server) already saved a newer token, our UPDATE matches
    no row and we return False instead of clobbering it.

    `expected_access` is None for a forced refresh (we do not know what the
    row currently holds) — in that case the swap is skipped and the last
    write wins, which is the pre-existing behaviour.

    `identity` is the signed-in mailbox reported by the refresh response.
    It is written with COALESCE semantics so a provider that omits the claim
    cannot erase a previously verified identity.
    """
    if expected_access is None:
        db.execute(
            "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, "
            "oauth_token_expires = to_timestamp(%s), "
            "oauth_identity = COALESCE(%s, oauth_identity), "
            "oauth_last_error = NULL, oauth_last_error_at = NULL, "
            "auth_error = NULL, auth_error_at = NULL WHERE id = %s",
            (new_access, new_refresh, expires_at_ts, identity or None, account_id),
        )
        return True
    # NOTE (2026-11-10): the CAS write also clears auth_error/auth_error_at.
    # A refresh that just proved this mailbox's own grant works means any
    # persisted auth refusal is stale; leaving it behind kept the red
    # "needs reconnecting" banner after a successful reconnect.
    rowcount = db.execute(
        "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, "
        "oauth_token_expires = to_timestamp(%s), "
        "oauth_identity = COALESCE(%s, oauth_identity), "
        "oauth_last_error = NULL, oauth_last_error_at = NULL, "
        "auth_error = NULL, auth_error_at = NULL "
        "WHERE id = %s AND oauth_access_token = %s",
        (new_access, new_refresh, expires_at_ts, identity or None, account_id, expected_access),
    )
    return rowcount > 0


def _notify_admins_auth_error(account_id: int, message: str) -> None:
    """One notification row per account per day when auth breaks (best effort)."""
    try:
        row = db.query_one("SELECT email FROM mail_accounts WHERE id = %s", (account_id,))
        label = (row or {}).get("email") or f"account {account_id}"
        dedupe = f"mail-auth-error:{account_id}:{time.strftime('%Y-%m-%d', time.gmtime())}"
        existing = db.query_one(
            "SELECT id FROM notifications WHERE dedupe_key = %s LIMIT 1", (dedupe,))
        if existing:
            return
        admins = db.query_dicts("SELECT id FROM users WHERE is_admin = TRUE AND is_active = TRUE")
        for admin in admins:
            db.execute(
                """INSERT INTO notifications
                     (user_id, type, message, payload, dedupe_key)
                   VALUES (%s, 'mail_auth_error', %s, %s::jsonb, %s)
                   ON CONFLICT DO NOTHING""",
                (admin["id"],
                 f"Mail account {label} needs reconnecting: {str(message)[:180]}",
                 json.dumps({"account_id": account_id}),
                 f"{dedupe}:{admin['id']}"),
            )
    except Exception:
        logger.debug("Could not notify admins about mail auth error", exc_info=True)


def record_auth_error(account_id: int, message: str) -> None:
    """Persist a send/sync auth failure and notify admins on the transition.

    The timestamp is stamped ONCE per distinct message. The old code wrote
    `auth_error_at = NOW()` on every call, and the scanner calls this every
    poll cycle while an account is unverified — so a two-minute-old refusal
    always looked brand new (live bug 2026-11-10).
    """
    try:
        row = db.query_one("SELECT auth_error FROM mail_accounts WHERE id = %s", (account_id,))
        current = (row or {}).get("auth_error")
        was_set = bool(current)
        text = str(message)[:300]
        if current != text:
            db.execute(
                "UPDATE mail_accounts SET auth_error = %s, auth_error_at = NOW() WHERE id = %s",
                (text, account_id),
            )
        if not was_set:
            _notify_admins_auth_error(account_id, message)
    except Exception:
        logger.debug("Could not persist auth_error for account %s", account_id, exc_info=True)


def clear_auth_error(account_id: int) -> bool:
    """Clear a persisted auth refusal once the account is provably healthy.

    Only touches the column when it is currently set, and only the caller that
    can prove every credential family belongs to this mailbox may call it
    (the scanner does, after `mail_identity.check_account` returns OK and the
    sync gate passes). Returns True when a stale value was cleared.
    """
    try:
        rowcount = db.execute(
            "UPDATE mail_accounts SET auth_error = NULL, auth_error_at = NULL "
            "WHERE id = %s AND auth_error IS NOT NULL",
            (account_id,),
        )
        return bool(rowcount)
    except Exception:
        logger.debug("Could not clear auth_error for account %s", account_id, exc_info=True)
        return False


def record_refresh_error(account_id: int, message: str) -> None:
    """Persist why a refresh failed so settings/the diagnostic can show it.

    There was no IMAP-side error column before migration 047, so a dead
    Exchange grant only ever existed as a log line and as the `auth_failed`
    heuristic in the scanner status.
    """
    try:
        db.execute(
            "UPDATE mail_accounts SET oauth_last_error = %s, oauth_last_error_at = NOW() "
            "WHERE id = %s",
            (str(message)[:300], account_id),
        )
    except Exception:
        logger.debug("Could not persist oauth_last_error for account %s", account_id, exc_info=True)
    record_auth_error(account_id, message)


def refresh_access_token(account_id: int, provider_name: str, current_refresh_token: str,
                         expires_at: datetime | None, force: bool = False,
                         expected_access: str | None = None) -> tuple[str, str] | None:
    """Refresh the OAuth access token for a mail account.

    Returns (new_access_token, new_refresh_token) on success, else None.

    `force=True` refreshes even when the token is still valid (used after an
    IMAP authentication failure).

    `expected_access` is the access token the caller read. When given, the
    save is a compare-and-swap against it, so a concurrent refresh by the
    scanner cannot be overwritten. When it is None the swap is skipped.

    The refreshed token's identity is checked against the account's email
    before it is stored (live bug 2026-11-09). A refresh token is bound to
    the mailbox that granted it, so the id_token in the refresh response is
    authoritative — if it names another mailbox, or cannot be read at all,
    the token is refused and the reason is persisted for the UI.
    """
    if not force and not token_needs_refresh(expires_at):
        return None
    if not current_refresh_token:
        logger.warning("No refresh token for account %d (%s), cannot refresh", account_id, provider_name)
        return None
    prov = oauth_providers.get_provider(provider_name)
    if not prov:
        logger.warning("Unknown provider %s for account %d", provider_name, account_id)
        return None
    with _refresh_lock:
        try:
            token_data = prov.refresh_token(current_refresh_token)
        except Exception as e:
            logger.error("Failed to refresh OAuth token for account %d: %s", account_id, e)
            record_refresh_error(account_id, f"Token refresh failed: {e}")
            return None
        new_access = (token_data or {}).get("access_token", "")
        # Never blank the stored refresh token: a response without one used
        # to wipe the account's only means of renewal.
        new_refresh = mail_oauth_binding.refresh_token_to_store(
            (token_data or {}).get("refresh_token"), current_refresh_token)
        new_expires = _now_ts() + (token_data or {}).get("expires_in", 3600)
        if not new_access:
            logger.error("OAuth refresh for account %d returned no access token", account_id)
            record_refresh_error(account_id, "Token refresh returned no access token")
            return None
        identity = ""
        try:
            acct = db.query_one(
                "SELECT email, oauth_identity FROM mail_accounts WHERE id = %s", (account_id,))
            identity = (token_data or {}).get("email") or ""
            # Only providers that report the signed-in mailbox on refresh can
            # be checked here; Google's refresh response carries no identity.
            reports_identity = bool(getattr(prov, "reports_identity_on_refresh", False))
            if acct and (reports_identity or identity):
                state_kind, detail = mail_oauth_binding.identity_state(
                    identity, acct.get("email"))
                if state_kind == mail_oauth_binding.IDENTITY_MISMATCH:
                    err = mail_oauth_binding.mismatch_message(detail, acct.get("email") or "")
                    logger.warning(
                        "OAuth refresh identity refused for account %d: %s", account_id, err)
                    record_refresh_error(account_id, err)
                    return None
                if state_kind == mail_oauth_binding.IDENTITY_UNVERIFIABLE:
                    # Unreadable identity: refuse to attach an unverified
                    # grant to a row that was never verified. A row that WAS
                    # verified at connect time (oauth_identity == email) may
                    # keep refreshing — the grant is bound to that mailbox by
                    # the provider, and refusing would break a healthy
                    # account on a token response that simply omits the
                    # id_token.
                    already_verified = (
                        mail_oauth_binding.normalize_email(acct.get("oauth_identity"))
                        and mail_oauth_binding.normalize_email(acct.get("oauth_identity"))
                        == mail_oauth_binding.normalize_email(acct.get("email"))
                    )
                    if not already_verified:
                        err = (
                            "The refreshed token did not report which mailbox it belongs to, so "
                            f"it cannot be verified for {acct.get('email')}. Disconnect OAuth for "
                            "this account and reconnect while signed in as that mailbox."
                        )
                        logger.warning(
                            "OAuth refresh identity refused for account %d: %s", account_id, err)
                        record_refresh_error(account_id, err)
                        return None
                    logger.info(
                        "OAuth refresh for account %d carried no identity; keeping the "
                        "previously verified identity", account_id)
        except Exception:
            logger.exception("OAuth identity check failed for account %d", account_id)
            return None
        try:
            saved = _save_refreshed_token(account_id, new_access, new_refresh,
                                          new_expires, expected_access,
                                          identity=identity or None)
        except Exception as e:
            logger.error("Failed to persist refreshed OAuth token for account %d: %s", account_id, e)
            record_refresh_error(account_id, f"Could not save refreshed token: {e}")
            return None
        if not saved:
            # Another refresher already stored a newer token — keep theirs.
            logger.info(
                "OAuth token for account %d was refreshed concurrently; keeping the newer token",
                account_id,
            )
            return None
        return new_access, new_refresh


def get_valid_access_token(account_id: int) -> tuple[str, str]:
    """Return (access_token, refresh_token) for an account, refreshing if needed.

    This is the single entry point used by server.py's IMAP path. The token
    is guaranteed to be usable NOW — a stale token is the reason "mark as
    read" did nothing in Outlook.
    """
    acct = db.query_one(
        "SELECT email, oauth_provider, oauth_access_token, oauth_refresh_token, "
        "oauth_token_expires FROM mail_accounts WHERE id = %s",
        (account_id,),
    )
    if not acct:
        return "", ""
    provider = acct.get("oauth_provider")
    access = acct.get("oauth_access_token") or ""
    if not provider:
        return access, ""
    refresh = acct.get("oauth_refresh_token") or ""
    expires = acct.get("oauth_token_expires")
    if not token_needs_refresh(expires):
        return access, refresh
    if not refresh:
        logger.error(
            "Account %d (%s) has an expired OAuth token and no refresh token",
            account_id, acct.get("email") or "?",
        )
        return access, refresh
    refreshed = refresh_access_token(
        account_id, provider, refresh, expires,
        expected_access=access or None,
    )
    if refreshed:
        return refreshed[0], refreshed[1]
    # The refresh failed, or another thread refreshed first. Re-read the row
    # so we use whatever token is actually stored now.
    fresh = db.query_one(
        "SELECT oauth_access_token, oauth_refresh_token FROM mail_accounts WHERE id = %s",
        (account_id,),
    )
    if fresh:
        return (fresh.get("oauth_access_token") or access), (fresh.get("oauth_refresh_token") or refresh)
    return access, refresh


def force_refresh_access_token(account_id: int) -> tuple[str, str] | None:
    """Force a fresh access token for an account (used after an auth failure).

    Returns (access_token, refresh_token) on success, else None.
    """
    acct = db.query_one(
        "SELECT oauth_provider, oauth_refresh_token, oauth_access_token "
        "FROM mail_accounts WHERE id = %s",
        (account_id,),
    )
    if not acct or not acct.get("oauth_provider") or not acct.get("oauth_refresh_token"):
        return None
    refreshed = refresh_access_token(
        account_id,
        acct["oauth_provider"],
        acct["oauth_refresh_token"],
        None,
        force=True,
        # Compare-and-swap against the token we just read, so a forced
        # refresh cannot overwrite a token another refresher stored in the
        # meantime (same guard as the normal path).
        expected_access=acct.get("oauth_access_token") or None,
    )
    if refreshed:
        return refreshed
    # The swap failed — another refresher won. Use the token they stored.
    fresh = db.query_one(
        "SELECT oauth_access_token, oauth_refresh_token FROM mail_accounts WHERE id = %s",
        (account_id,),
    )
    if fresh and fresh.get("oauth_access_token"):
        return (fresh["oauth_access_token"], fresh.get("oauth_refresh_token") or acct["oauth_refresh_token"])
    return None


__all__ = [
    "REFRESH_MARGIN_SECONDS",
    "force_refresh_access_token",
    "get_valid_access_token",
    "record_auth_error",
    "record_refresh_error",
    "refresh_access_token",
    "token_needs_refresh",
]
