"""One mailbox == one account: the credential identity gate (item 2, 2026-11-10).

WHY THIS MODULE EXISTS
----------------------
Every credential in the CRM lives in a column of a `mail_accounts` ROW:

    password_encrypted, smtp_password_encrypted,
    oauth_access_token, oauth_refresh_token, oauth_token_expires, oauth_identity,
    oauth_graph_access_token, oauth_graph_refresh_token,
    oauth_graph_token_expires, oauth_graph_identity, smtp_user

That row's `id` is the ONLY key that may be used to read or write them. Any
lookup by email / tenant / app / provider, or any "first account with flag X"
selection, is how one mailbox's credentials end up on another mailbox — the
2026-10-29 (Graph sent as another Outlook account) and 2026-11-09 (every
Outlook account lost OAuth) incidents.

This module is the single place that answers:

  * `credentials_for_account(id)` — read the credential columns, by id only.
  * `check_account(row)`          — do these credentials provably belong to
                                   this row's address? (ok / mismatch /
                                   unverified)
  * `sync_gate(row)`              — may the scanner authenticate as this
                                   mailbox? Returns (allowed, reason).
  * `system_account()`            — which account (by id) may send system
                                   mail, and only if it is identity-verified.

Nothing here logs or returns a secret value. Identity is compared with
`mail_oauth_binding.normalize_email` (handles the MSA
`live.com#user@outlook.com` quirk and rejects B2B guest UPNs).

Refusals are persisted with `mail_oauth.record_auth_error`, which writes
`mail_accounts.auth_error`/`auth_error_at` — the field the accounts API
exposes as `authError` and the UI shows as "needs reconnecting".
"""

from __future__ import annotations

import logging
from typing import Any

import mail_oauth_binding

logger = logging.getLogger("sietch.mail_identity")

# Same three states as the token-binding rules. Kept as aliases so callers
# only need one import.
IDENTITY_OK = mail_oauth_binding.IDENTITY_OK
IDENTITY_MISMATCH = mail_oauth_binding.IDENTITY_MISMATCH
IDENTITY_UNVERIFIABLE = mail_oauth_binding.IDENTITY_UNVERIFIABLE

normalize_email = mail_oauth_binding.normalize_email

# The credential columns. Fixed allowlist: used for the per-account read and
# asserted by tests/test_mail_credential_isolation.py.
CREDENTIAL_COLUMNS = (
    "password_encrypted",
    "smtp_password_encrypted",
    "smtp_user",
    "oauth_provider",
    "oauth_access_token",
    "oauth_refresh_token",
    "oauth_token_expires",
    "oauth_identity",
    "oauth_graph_access_token",
    "oauth_graph_refresh_token",
    "oauth_graph_token_expires",
    "oauth_graph_identity",
    "smtp_auth_disabled",
)

_CREDENTIAL_SELECT = ", ".join(CREDENTIAL_COLUMNS)

# A system-mail account may be pinned explicitly with this system_settings key
# (JSON value: the account id, or {"account_id": N}). Optional: without it the
# single is_crm_mail account is used.
SYSTEM_ACCOUNT_SETTING = "system_mail_account_id"


def _db(db_mod: Any = None):
    if db_mod is not None:
        return db_mod
    import db as _db_mod
    return _db_mod


# ── per-account credential access ───────────────────────────────────────────

def credentials_for_account(account_id: Any, db_mod: Any = None) -> dict | None:
    """Read one account's credential columns BY ID.

    This is the only credential reader in the codebase. `account_id` is
    coerced to int; anything else (an email, a tenant, a provider name) is
    rejected rather than silently matching a different row.
    """
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        raise ValueError(
            "mail credentials are keyed by mail_accounts.id; "
            f"refusing non-integer account key {account_id!r}")
    row = _db(db_mod).query_one(
        f"SELECT id, {_CREDENTIAL_SELECT} FROM mail_accounts WHERE id = %s",
        (aid,),
    )
    return dict(row) if row else None


def account_row(account_id: Any, db_mod: Any = None) -> dict | None:
    """One account row (all columns), by id. Never by email."""
    try:
        aid = int(account_id)
    except (TypeError, ValueError):
        raise ValueError(f"mail accounts are keyed by id; refusing {account_id!r}")
    row = _db(db_mod).query_one("SELECT * FROM mail_accounts WHERE id = %s", (aid,))
    return dict(row) if row else None


# ── identity classification ─────────────────────────────────────────────────

def _looks_like_address(value: Any) -> bool:
    text = normalize_email(value)
    return "@" in text and not text.startswith("@") and not text.endswith("@")


def check_account(row: dict | None,
                  families: tuple[str, ...] = ("exchange", "graph", "smtp")
                  ) -> tuple[str, str]:
    """Classify whether a mail account's credentials belong to its address.

    Returns (state, detail) where detail is either the verified identity
    (IDENTITY_OK) or a human-readable reason (mismatch: the OTHER mailbox;
    unverifiable: why it cannot be proven).

    Rules (each requested credential family is checked independently and must
    agree with `mail_accounts.email`):

      * `exchange` — `oauth_identity` (IMAP/SMTP OAuth; written only by the
        identity-verified connect callback and refresh helper).
      * `graph` — `oauth_graph_identity` (Graph send; same rule).
      * `smtp` — `smtp_user`, when it is an address, must be this mailbox.

    `families` matters: the scanner's sync gate only cares about `exchange`
    (the IMAP credential), because a mis-bound Graph token must not stop a
    healthy mailbox from *receiving* mail — the send path refuses it instead.

    A credential family that exists without a recorded identity is
    UNVERIFIABLE (fail closed), never "probably fine".
    """
    row = row or {}
    email = normalize_email(row.get("email"))
    if not _looks_like_address(email):
        return IDENTITY_UNVERIFIABLE, "this mail account has no usable email address"

    provider = (row.get("oauth_provider") or "").strip().lower()
    exchange_tokens = bool(row.get("oauth_access_token")
                           or row.get("oauth_refresh_token"))
    exchange_ident = normalize_email(row.get("oauth_identity"))
    graph_ident = normalize_email(row.get("oauth_graph_identity"))
    graph_tokens = bool(row.get("oauth_graph_access_token")
                        or row.get("oauth_graph_refresh_token"))
    smtp_user = normalize_email(row.get("smtp_user"))

    # A provable mismatch outranks everything else.
    if "exchange" in families and exchange_ident and exchange_ident != email:
        return IDENTITY_MISMATCH, exchange_ident
    if "graph" in families and graph_ident and graph_ident != email:
        return IDENTITY_MISMATCH, graph_ident
    if ("smtp" in families and _looks_like_address(smtp_user)
            and smtp_user != email):
        return IDENTITY_MISMATCH, smtp_user

    if "exchange" in families and provider and exchange_tokens and not exchange_ident:
        return IDENTITY_UNVERIFIABLE, (
            "the OAuth connection for this mailbox was never identity-verified")
    if "graph" in families and graph_tokens and not graph_ident:
        return IDENTITY_UNVERIFIABLE, (
            "the Graph connection for this mailbox was never identity-verified")
    return IDENTITY_OK, email


def identity_message(row: dict, state: str, detail: str) -> str:
    """The refusal text persisted to `auth_error` and shown in the UI."""
    email = (row or {}).get("email") or "this mailbox"
    if state == IDENTITY_MISMATCH:
        return (
            f"Wrong mailbox's credentials: this account is connected as {detail}, "
            f"not {email}. Disconnect and reconnect while signed in as {email}."
        )
    return (
        f"Credentials for {email} could not be verified ({detail}). "
        f"Disconnect and reconnect while signed in as {email}."
    )


def record_identity_refusal(account_id: Any, message: str) -> None:
    """Persist an identity refusal so the UI shows it, and notify admins.

    Best effort: a missing `auth_error` column (unmigrated DB) must never
    break the caller's refusal path.
    """
    try:
        import mail_oauth
        mail_oauth.record_auth_error(int(account_id), message)
    except Exception:
        logger.debug("Could not persist identity refusal for account %s",
                     account_id, exc_info=True)


def refuse(account_id: Any, row: dict, state: str, detail: str,
           where: str = "sync") -> str:
    """Record + return the refusal reason (never raises)."""
    message = identity_message(row, state, detail)
    logger.warning("Refusing %s for mail account %s: %s", where, account_id, message)
    record_identity_refusal(account_id, message)
    return message


# ── sync gate ───────────────────────────────────────────────────────────────

def sync_gate(row: dict | None, *, login_user: Any = None) -> tuple[bool, str]:
    """May the scanner authenticate as this mailbox? (allowed, reason)

    Two independent checks:

      1. The IMAP login user must be this account's own address. The scanner
         already builds it from `mail_accounts.email`; asserting it here means
         a future caller cannot pass another account's mailbox while using
         this row's credentials.
      2. Every stored credential family must be identity-verified for that
         address (see `check_account`).
    """
    row = row or {}
    email = normalize_email(row.get("email"))
    if login_user is not None and normalize_email(login_user) != email:
        return False, (
            "the IMAP login user is not this account's address "
            f"({normalize_email(login_user) or 'empty'} != {email or 'empty'})")
    # Only the IMAP credential family gates a sync. A mis-bound Graph token
    # stops *sending* (refused by the send path) but must not stop receiving.
    state, detail = check_account(row, families=("exchange",))
    if state == IDENTITY_OK:
        return True, ""
    return False, identity_message(row, state, detail)


def sync_gate_by_id(account_id: Any, db_mod: Any = None) -> tuple[bool, str]:
    """`sync_gate` for handlers that only have an account id."""
    row = account_row(account_id, db_mod)
    if not row:
        return False, "mail account not found"
    return sync_gate(row)


# ── system mail (password resets, notifications, automation) ────────────────

def _configured_system_account_id(db_mod: Any) -> int | None:
    try:
        row = db_mod.query_one(
            "SELECT value FROM system_settings WHERE key = %s",
            (SYSTEM_ACCOUNT_SETTING,),
        )
    except Exception:
        return None
    raw = (row or {}).get("value")
    if raw is None:
        return None
    if isinstance(raw, (int, str)):
        value = raw
    elif isinstance(raw, dict):
        value = raw.get("account_id")
    else:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s setting", SYSTEM_ACCOUNT_SETTING)
        return None


def system_account(db_mod: Any = None, *, refresh: bool = True) -> dict | None:
    """The ONE account allowed to send system mail, or None.

    Selection is by id: an explicit `system_mail_account_id` setting wins;
    otherwise the lowest-id `is_crm_mail` account (the historic choice) is
    used and a warning is logged when more than one exists, because picking
    "whichever row sorts first" is not a decision.

    The chosen row must pass `check_account`. If it does not, this returns
    None and records the refusal — the caller must NOT fall back to another
    account's credentials or to env SMTP.
    """
    db_mod = _db(db_mod)
    row = None
    pinned = _configured_system_account_id(db_mod)
    if pinned is not None:
        row = account_row(pinned, db_mod)
        if not row:
            logger.warning(
                "system_mail_account_id=%s does not exist; falling back to the CRM inbox",
                pinned)
    if row is None:
        rows = db_mod.query_dicts(
            "SELECT * FROM mail_accounts WHERE is_crm_mail = TRUE "
            "AND smtp_host IS NOT NULL ORDER BY id"
        ) or []
        if not rows:
            return None
        row = dict(rows[0])
        if len(rows) > 1:
            logger.warning(
                "Multiple CRM inboxes (%s) could send system mail; using account %s. "
                "Pin one with system_settings.%s.",
                ", ".join(str(r.get("id")) for r in rows), row.get("id"),
                SYSTEM_ACCOUNT_SETTING)
    state, detail = check_account(row)
    if state != IDENTITY_OK:
        refuse(row.get("id"), row, state, detail, where="system mail")
        return None
    if refresh and row.get("oauth_provider") and row.get("oauth_refresh_token"):
        try:
            import mail_oauth
            access, refresh_token = mail_oauth.get_valid_access_token(row["id"])
            if access:
                row["oauth_access_token"] = access
                row["oauth_refresh_token"] = refresh_token or row.get("oauth_refresh_token")
        except Exception:
            logger.debug("System mail token refresh failed for account %s",
                         row.get("id"), exc_info=True)
    return row


def system_account_id(db_mod: Any = None) -> int | None:
    """The id selected by `system_account`, without loading/refreshing tokens."""
    try:
        db_mod = _db(db_mod)
        pinned = _configured_system_account_id(db_mod)
        if pinned is not None:
            return pinned
        row = db_mod.query_one(
            "SELECT id FROM mail_accounts WHERE is_crm_mail = TRUE "
            "AND smtp_host IS NOT NULL ORDER BY id LIMIT 1"
        )
        return int(row["id"]) if row else None
    except Exception:
        return None


__all__ = [
    "CREDENTIAL_COLUMNS",
    "IDENTITY_MISMATCH",
    "IDENTITY_OK",
    "IDENTITY_UNVERIFIABLE",
    "SYSTEM_ACCOUNT_SETTING",
    "account_row",
    "check_account",
    "credentials_for_account",
    "identity_message",
    "normalize_email",
    "record_identity_refusal",
    "refuse",
    "sync_gate",
    "sync_gate_by_id",
    "system_account",
    "system_account_id",
]
