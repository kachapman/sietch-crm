"""Identity binding rules for mail OAuth tokens (one mailbox == one account).

WHY THIS MODULE EXISTS (live bug 2026-11-09)
--------------------------------------------
Outlook tokens were landing on the wrong mail account again: the settings
page for `estimates.masterx@outlook.com` (account 8) showed
"Connected as mdm.estimates@outlook.com", reconnecting refused with the
mismatch error, and every Outlook account ended up `auth_failed`.

The rules below are the single place that decides whether a freshly minted
token may be stored on a given mail account. `server.py` (connect callback,
Graph callback) and `mail_oauth.py` (refresh path used by the scanner, the
flag sync and the send path) all call them, so a token can never be saved
by one path that another path would reject.

Two independent things are checked:

1. **Identity** — the Microsoft/Google account that just signed in must be
   the mailbox the row represents. An identity we cannot read is treated as
   a refusal (fail closed), never as "probably fine".

2. **Grant exclusivity** — a refresh token is a grant for ONE mailbox.
   Microsoft rotates it on use, so two rows holding the same grant means
   the first refresh invalidates the second: exactly how "all Outlook
   accounts lost OAuth access" happens. A grant already attached to another
   account is refused.

No secrets are ever logged here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("sietch.mail_oauth_binding")

IDENTITY_OK = "ok"
IDENTITY_MISMATCH = "mismatch"
IDENTITY_UNVERIFIABLE = "unverifiable"

# Columns that hold a refresh token. Fixed allowlist: the value is
# interpolated into SQL, so it must never come from user input.
REFRESH_COLUMNS = {
    "exchange": "oauth_refresh_token",
    "graph": "oauth_graph_refresh_token",
}


def normalize_email(value: Any) -> str:
    """Normalize an email/UPN claim for comparison.

    Handles the MSA id_token quirk where `preferred_username` looks like
    `live.com#user@outlook.com`, plus stray whitespace/case. B2B guest
    tokens (`user_domain.com#EXT#@tenant.onmicrosoft.com`) are NOT
    rewritten: such a token is for a different (guest) identity and must
    not be treated as the personal mailbox.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "#" in text and "#ext#" not in text:
        text = text.rsplit("#", 1)[-1].strip()
    return text


def identity_state(token_email: Any, account_email: Any) -> tuple[str, str]:
    """Classify a token's signed-in identity against a mail account.

    Returns (state, detail):
      * (IDENTITY_OK, identity)            — may be stored on this account
      * (IDENTITY_MISMATCH, token_email)   — belongs to another mailbox
      * (IDENTITY_UNVERIFIABLE, reason)    — cannot be proven; refuse
    """
    account = normalize_email(account_email)
    identity = normalize_email(token_email)
    if not account:
        return IDENTITY_UNVERIFIABLE, "this mail account has no email address"
    if not identity:
        return IDENTITY_UNVERIFIABLE, (
            "the provider did not say which account signed in, so the token "
            "cannot be proven to belong to this mailbox"
        )
    if identity != account:
        return IDENTITY_MISMATCH, identity
    return IDENTITY_OK, identity


def mismatch_message(identity: str, account_email: str, kind: str = "exchange") -> str:
    """User-facing explanation, identical on connect and on refresh."""
    label = "Graph" if kind == "graph" else "OAuth"
    return (
        f"You signed in as {identity}, but this connects {account_email}. "
        f"Disconnect {label} for this mailbox first, then reconnect while "
        f"signed in as {account_email}."
    )


def unverifiable_message(account_email: str, kind: str = "exchange") -> str:
    label = "Graph" if kind == "graph" else "OAuth"
    return (
        "Could not verify which account signed in, so the connection was not "
        f"saved. Disconnect {label} for this mailbox and reconnect while "
        f"signed in as {account_email or 'the correct account'}."
    )


def refresh_token_to_store(new_refresh_token: Any, existing_refresh_token: Any) -> str:
    """Never blank an existing refresh token.

    A code exchange or refresh response that omits `refresh_token` (consent
    without offline_access, a tenant policy, a partial response) used to be
    written as an empty string, which silently destroyed the account's only
    means of renewing access — the account then reported `auth_failed`
    forever. Keep the stored value in that case.
    """
    new = str(new_refresh_token or "").strip()
    if new:
        return new
    return str(existing_refresh_token or "").strip()


def grant_owner(db: Any, refresh_token: Any, column: str,
                exclude_account_id: int | None = None) -> dict | None:
    """Return another mail account that already holds this exact grant.

    `db` is the project's db module (query_one). Returns the conflicting row
    ({id, email}) or None. Never selects the token itself back out.
    """
    if column not in REFRESH_COLUMNS.values():
        raise ValueError(f"refusing unknown refresh-token column {column!r}")
    token = str(refresh_token or "").strip()
    if not token:
        return None
    if exclude_account_id is None:
        row = db.query_one(
            f"SELECT id, email FROM mail_accounts WHERE {column} = %s ORDER BY id LIMIT 1",
            (token,),
        )
    else:
        row = db.query_one(
            f"SELECT id, email FROM mail_accounts WHERE {column} = %s AND id <> %s "
            f"ORDER BY id LIMIT 1",
            (token, int(exclude_account_id)),
        )
    return dict(row) if row else None


def grant_conflict_message(other: dict, kind: str = "exchange") -> str:
    label = "Graph" if kind == "graph" else "OAuth"
    other_id = other.get("id")
    other_email = other.get("email") or f"account {other_id}"
    return (
        f"This {label} grant is already connected to {other_email} (account {other_id}). "
        "A Microsoft grant belongs to exactly one mailbox — connecting it twice makes the "
        "accounts rotate each other's refresh token and both lose access. "
        f"Disconnect it from {other_email} first."
    )


__all__ = [
    "IDENTITY_MISMATCH",
    "IDENTITY_OK",
    "IDENTITY_UNVERIFIABLE",
    "REFRESH_COLUMNS",
    "grant_conflict_message",
    "grant_owner",
    "identity_state",
    "mismatch_message",
    "normalize_email",
    "refresh_token_to_store",
    "unverifiable_message",
]
