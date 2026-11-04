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

import logging
import threading
import time
from datetime import datetime
from typing import Any

import db
import oauth_providers

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
    """True when the access token is expired or close enough to expiry."""
    if expires_at is None:
        # Unknown expiry — assume it needs a refresh so a stale token is
        # never used. (The refresh itself is a no-op when there is no
        # refresh token.)
        return True
    if not isinstance(expires_at, datetime):
        return True
    return expires_at.timestamp() <= _now_ts() + REFRESH_MARGIN_SECONDS


def _save_refreshed_token(account_id: int, new_access: str, new_refresh: str,
                          expires_at_ts: float, expected_access: str | None) -> bool:
    """Persist a refreshed token, but only if the row still holds the token
    we based the refresh on.

    Compare-and-swap on `oauth_access_token`: when another refresher (the
    scanner vs the server) already saved a newer token, our UPDATE matches
    no row and we return False instead of clobbering it.

    `expected_access` is None for a forced refresh (we do not know what the
    row currently holds) — in that case the swap is skipped and the last
    write wins, which is the pre-existing behaviour.
    """
    if expected_access is None:
        db.execute(
            "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, "
            "oauth_token_expires = to_timestamp(%s) WHERE id = %s",
            (new_access, new_refresh, expires_at_ts, account_id),
        )
        return True
    rowcount = db.execute(
        "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, "
        "oauth_token_expires = to_timestamp(%s) WHERE id = %s AND oauth_access_token = %s",
        (new_access, new_refresh, expires_at_ts, account_id, expected_access),
    )
    return rowcount > 0


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
            return None
        new_access = (token_data or {}).get("access_token", "")
        new_refresh = (token_data or {}).get("refresh_token") or current_refresh_token
        new_expires = _now_ts() + (token_data or {}).get("expires_in", 3600)
        if not new_access:
            logger.error("OAuth refresh for account %d returned no access token", account_id)
            return None
        try:
            saved = _save_refreshed_token(account_id, new_access, new_refresh,
                                          new_expires, expected_access)
        except Exception as e:
            logger.error("Failed to persist refreshed OAuth token for account %d: %s", account_id, e)
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
        # No compare-and-swap: we do not know what the row holds, and a
        # forced refresh is already a last resort.
        expected_access=None,
    )
    return refreshed


__all__ = [
    "REFRESH_MARGIN_SECONDS",
    "force_refresh_access_token",
    "get_valid_access_token",
    "refresh_access_token",
    "token_needs_refresh",
]
