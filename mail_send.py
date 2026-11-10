"""Shared mail send routing — used by both server.py (manual compose) and
scanner/mail_scanner.py (custom "Reply to Email" behavior).

Routing:
  1. Microsoft OAuth with a Graph token → try Graph first.
  2. Fall back to SMTP (existing XOAUTH2 / app-password / manual paths).
  3. If SMTP returns 5.7.139 (SMTP AUTH disabled) and a Graph token is
     available, retry Graph.

Returns (success, error_message, send_method). send_method is one of:
  'graph', 'smtp_oauth', 'smtp_password'. Reserved for future analytics.

Caller is responsible for token refresh (Microsoft Exchange token is
refreshed inline here; Graph token is refreshed via
`refresh_graph_token` passed in as a callback). Persistence of refreshed
tokens is handled inside the helper for clarity at the call site.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sietch.mailsend")


def _looks_like_smtp_auth_disabled(err: str | None) -> bool:
    """True when an SMTP error means Microsoft disabled SMTP AUTH (5.7.139).

    Matches both the raw server code and our friendly rewrite of it, so the
    memory flag is set no matter which layer produced the message.
    """
    if not err:
        return False
    return ("5.7.139" in err
            or "SmtpClientAuthentication is disabled" in err
            or "has disabled SMTP sending for this Outlook account" in err)


def _set_smtp_auth_disabled(account_id: Any, value: bool) -> None:
    """Persist/clear the per-account SMTP-AUTH-disabled memory. Best-effort."""
    if account_id is None:
        return
    try:
        import db as db_mod
        db_mod.execute(
            "UPDATE mail_accounts SET smtp_auth_disabled = %s WHERE id = %s",
            (bool(value), account_id),
        )
    except Exception:
        logger.exception("Failed to persist smtp_auth_disabled=%s for account %s", value, account_id)


def _graph_identity_state(acct: dict) -> tuple[str, str]:
    """Classify a mail account's Graph identity for send-time safety.

    Returns (state, detail) where state is:
      - "ok"         the Graph token is verified and belongs to this mailbox
      - "mismatch"   the token provably belongs to a DIFFERENT mailbox
      - "unverified" the token's identity could not be determined

    Graph derives the sender from the access token, NOT from any From
    address we pass ("from_addr is informational only — Graph derives the
    sender from the access token"). So the only thing that stops a send
    going out as the wrong Outlook account is knowing which mailbox the
    token belongs to. Live incident 2026-10-29: the Aplus account held an
    MDM token (the connect-time identity check silently no-opped because
    the Graph scope had no `openid`), and every Graph send went out as
    "MDM Construction" while the CRM recorded "aplus.estimates@outlook.com".
    """
    acct_email = (acct.get("email") or "").strip().lower()
    identity = (acct.get("oauth_graph_identity") or "").strip().lower()
    if not acct_email:
        return "unverified", "this mail account has no email address"
    if not identity:
        return "unverified", (
            "the Graph connection is not identity-verified — reconnect Graph "
            "for this mailbox so we can confirm which Outlook account sends"
        )
    if identity != acct_email:
        return "mismatch", identity
    return "ok", identity


def try_send_for_account(
    acct: dict,
    *,
    to_addr: str,
    subject: str,
    html_body: str = "",
    text_body: str | None = None,
    cc_addr: str | None = None,
    bcc_addr: str | None = None,
    attachments: list[dict] | None = None,
    refresh_graph_token: callable | None = None,
    result_meta: dict | None = None,
) -> tuple[bool, str | None, str]:
    """Single source of truth for sending email from a mail_accounts row.

    Args:
        acct: dict from `SELECT * FROM mail_accounts WHERE id = %s` (raw row)
        refresh_graph_token: optional callable `(account_id) -> (token, error)`.
            Used to obtain a fresh Graph access token; the caller can persist
            updates via the helper returned value. If None, the Graph path
            is skipped (callers without DB write access pass None).

    The From name/address is taken ONLY from this account row: `email` for the
    address and `smtp_from_name` for the display name. There is deliberately
    no `from_name` parameter — a caller (or a global default) must never be
    able to put another name on the envelope. Blank `smtp_from_name` means
    "address only", not "borrow display_name or a brand name".
    """
    from smtp_client import send_email_from_account

    meta = result_meta if result_meta is not None else {}
    account_from_addr = (acct.get("email") or "").strip()
    account_from_name = (acct.get("smtp_from_name") or "").strip()
    oauth_provider = acct.get("oauth_provider")
    oauth_access_token = acct.get("oauth_access_token")
    smtp_password = acct.get("smtp_password_encrypted") or ""
    use_oauth_smtp = bool(oauth_provider and oauth_access_token and not smtp_password)
    account_id = acct.get("id")
    if use_oauth_smtp:
        expires = acct.get("oauth_token_expires")
        if expires and isinstance(expires, datetime) and expires.timestamp() < datetime.now(timezone.utc).timestamp():
            # Route through the shared CAS refresher (2026-11-10): the old
            # inline refresh wrote both tokens with no compare-and-swap, so it
            # could clobber a token the scanner had just rotated — the "tokens
            # dropped" failure. mail_oauth also records auth_error on failure.
            import mail_oauth
            refreshed = mail_oauth.refresh_access_token(
                account_id, oauth_provider, acct.get("oauth_refresh_token") or "",
                expires, force=True,
                expected_access=acct.get("oauth_access_token") or None,
            )
            if refreshed:
                oauth_access_token = refreshed[0]
            else:
                return False, (
                    "OAuth token refresh failed — reconnect this mail account. "
                    "Nothing was sent."
                ), "smtp_oauth"

    # Step 1: try Graph if this is a Microsoft OAuth account with a token.
    graph_token, graph_err = (None, None)
    if oauth_provider == "microsoft" and refresh_graph_token is not None:
        # SEND-TIME IDENTITY GUARD (live bug 2026-10-29).
        # Graph picks the sender from the access token, so a token that
        # belongs to another mailbox sends as THAT mailbox no matter what we
        # pass as from_addr. Refuse rather than send from the wrong account.
        # Checked BEFORE the send so nothing goes out.
        #
        # The guard is unconditional for Microsoft accounts: if the Graph
        # connection is not identity-verified we must not use the Graph path
        # at all (a live check on 2026-10-29 showed an account with no Graph
        # token still reaching the Graph/SMTP attempts before failing).
        state, detail = _graph_identity_state(acct)
        if state == "mismatch":
            logger.warning(
                "Refusing Graph send for account=%s: token belongs to %s, mailbox is %s",
                account_id, detail, acct.get("email"))
            return False, (
                f"Refusing to send: this mailbox's Graph connection belongs to {detail}, "
                f"not {acct.get('email')}. Disconnect Graph for this account and reconnect "
                f"while signed in as {acct.get('email')}. Nothing was sent."
            ), "graph"
        if state == "unverified" and (acct.get("oauth_graph_access_token")
                                      or acct.get("oauth_graph_refresh_token")):
            # A Graph token exists but we cannot prove which mailbox it
            # belongs to → refuse (this is the exact state that sent Aplus
            # mail as MDM).
            logger.warning(
                "Refusing Graph send for account=%s: identity unverified (%s)",
                account_id, detail)
            return False, (
                f"Refusing to send: {detail} ({acct.get('email')}). "
                "Disconnect Graph for this account and reconnect while signed in as "
                f"{acct.get('email')}. Nothing was sent."
            ), "graph"
        graph_token, graph_err = refresh_graph_token(account_id)
        has_graph_connection = bool(
            acct.get("oauth_graph_access_token") or acct.get("oauth_graph_refresh_token"))
        if not graph_token and graph_err and has_graph_connection:
            # The account HAS a Graph connection but the token could not be
            # minted/refreshed. Refuse; never fall back to SMTP or another
            # credential (2026-11-10, f07a125). An account with no Graph
            # connection at all is not a failed Graph send: the caller reports
            # that as (None, None) and SMTP/XOAUTH2 is its only path.
            return False, graph_err, "graph"
        if graph_token:
            # The refresher may have rotated the token; re-check identity
            # against the freshly minted token's verified identity.
            if oauth_provider == "microsoft":
                state, detail = _graph_identity_state(acct)
                if state == "mismatch":
                    logger.warning(
                        "Refusing Graph send for account=%s: refreshed token belongs to %s",
                        account_id, detail)
                    return False, (
                        f"Refusing to send: the refreshed Graph token belongs to {detail}, "
                        f"not {acct.get('email')}. Disconnect Graph for this account and "
                        f"reconnect while signed in as {acct.get('email')}. Nothing was sent."
                    ), "graph"
            from mail_graph_client import send_email_via_graph
            try:
                ok, err = send_email_via_graph(
                    graph_access_token=graph_token,
                    from_addr=account_from_addr,
                    from_name=account_from_name,
                    # The explicit From must BE the mailbox the token belongs
                    # to; send_email_via_graph refuses anything else.
                    verified_identity=acct.get("oauth_graph_identity"),
                    to_addr=to_addr,
                    subject=subject,
                    html_body=html_body,
                    text_body=text_body,
                    cc_addr=cc_addr,
                    bcc_addr=bcc_addr,
                    attachments=attachments,
                )
                if ok:
                    return True, None, "graph"
                # NO FALLBACK (2026-11-10, Ken): Graph is this account's
                # configured method. A failure is returned to the caller — we
                # never switch to SMTP or to another credential.
                logger.error("Graph send failed for account=%s: %s (no fallback)", account_id, err)
                return False, err, "graph"
            except Exception as e:
                logger.exception("Graph send threw for account=%s", account_id)
                graph_err = str(e)

    # Step 2: SMTP path (works for Google OAuth, app passwords, Microsoft
    # accounts without a Graph token, and any account with manual SMTP).
    # KNOWN-DEAD SKIP (2026-10-22): a Microsoft mailbox that previously failed
    # with 5.7.139 has SMTP AUTH disabled server-side. SMTP "success" (250) is
    # not proof of delivery for these mailboxes, so the dead path is never
    # retried silently — fail loudly unless Graph already delivered above.
    if oauth_provider == "microsoft" and acct.get("smtp_auth_disabled"):
        return False, (
            "Microsoft disabled SMTP sending for this Outlook mailbox (previously observed), "
            f"and Graph send is unavailable ({graph_err or 'no Graph token connected'}). "
            "Nothing was sent — reconnect Graph from mail account settings."
        ), "graph" if graph_token else "smtp_password"
    # SMTP SPOOFING GUARD (live bug 2026-10-29): the sender of an SMTP
    # message is the authenticated smtp_user, not the From header we set.
    # If they differ, the mail goes out as smtp_user while we record the
    # account email — the same class of lie as the Graph bug. Refuse.
    smtp_user = (acct.get("smtp_user") or "").strip().lower()
    acct_email = (acct.get("email") or "").strip().lower()
    if smtp_user and acct_email and smtp_user != acct_email:
        logger.warning("Refusing SMTP send for account=%s: smtp_user=%s != account email=%s",
                       account_id, smtp_user, acct_email)
        return False, (
            f"Refusing to send: this account authenticates as {smtp_user} but is listed as "
            f"{acct.get('email')}. Fix the account's SMTP user, or reconnect Graph for it. "
            "Nothing was sent."
        ), "smtp_password"
    # OAuth-exchange identity gate (2026-11-10): an XOAUTH2 SMTP login is the
    # token's mailbox, even when smtp_user is empty. The token must have been
    # verified as THIS account's address, or we would send as another mailbox.
    if use_oauth_smtp:
        exch_ident = (acct.get("oauth_identity") or "").strip().lower()
        if not exch_ident:
            logger.warning(
                "Refusing SMTP send for account=%s: OAuth token identity unverified", account_id)
            return False, (
                "Refusing to send: this mailbox's OAuth connection was never identity-verified. "
                f"Disconnect and reconnect OAuth for {acct.get('email')}. Nothing was sent."
            ), "smtp_oauth"
        if acct_email and exch_ident != acct_email:
            logger.warning(
                "Refusing SMTP send for account=%s: OAuth token belongs to %s, mailbox is %s",
                account_id, exch_ident, acct_email)
            return False, (
                f"Refusing to send: this mailbox's OAuth token belongs to {exch_ident}, not "
                f"{acct.get('email')}. Disconnect and reconnect while signed in as "
                f"{acct.get('email')}. Nothing was sent."
            ), "smtp_oauth"
    smtp_host = acct.get("smtp_host") or ""
    if not smtp_host:
        return False, graph_err or "SMTP not configured for this account", "graph" if graph_token else "smtp_password"
    try:
        ok, err = send_email_from_account(
            smtp_host=smtp_host,
            smtp_port=int(acct.get("smtp_port") or 587),
            smtp_user=acct.get("smtp_user") or acct.get("email", ""),
            smtp_password=smtp_password,
            # From name AND address come from this account row only (2026-11-10).
            # Never a caller override, a global default, or display_name.
            from_name=account_from_name or None,
            from_addr=account_from_addr,
            to_addr=to_addr,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            cc_addr=cc_addr,
            bcc_addr=bcc_addr,
            use_tls=bool(acct.get("smtp_use_tls")),
            attachments=attachments,
            oauth_provider=oauth_provider if use_oauth_smtp else None,
            oauth_access_token=oauth_access_token if use_oauth_smtp else None,
        )
    except Exception as e:
        logger.exception("SMTP send threw for account=%s", account_id)
        return False, str(e), "graph" if graph_token else "smtp_password"
    if ok:
        return True, None, "smtp_oauth" if use_oauth_smtp else "smtp_password"

    # MEMORIZE A DEAD SMTP AUTH (restored 2026-11-10): the 5.7.139 detection
    # and the known-dead skip above still exist, but the fallback rewrite
    # (f07a125) dropped the write, leaving `_set_smtp_auth_disabled` dead and
    # every send retrying an SMTP path Microsoft has disabled for the
    # mailbox. Record the flag so later sends skip it; the Graph retry that
    # used to accompany this write was deliberately removed (no method
    # switch), so a failure is still returned to the caller.
    if err and oauth_provider == "microsoft" and _looks_like_smtp_auth_disabled(err):
        _set_smtp_auth_disabled(account_id, True)

    return False, err or graph_err, "graph" if graph_token else ("smtp_oauth" if use_oauth_smtp else "smtp_password")
