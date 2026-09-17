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


def try_send_for_account(
    acct: dict,
    *,
    to_addr: str,
    subject: str,
    html_body: str = "",
    text_body: str | None = None,
    cc_addr: str | None = None,
    bcc_addr: str | None = None,
    from_name: str | None = None,
    attachments: list[dict] | None = None,
    refresh_graph_token: callable | None = None,
) -> tuple[bool, str | None, str]:
    """Single source of truth for sending email from a mail_accounts row.

    Args:
        acct: dict from `SELECT * FROM mail_accounts WHERE id = %s` (raw row)
        refresh_graph_token: optional callable `(account_id) -> (token, error)`.
            Used to obtain a fresh Graph access token; the caller can persist
            updates via the helper returned value. If None, the Graph path
            is skipped (callers without DB write access pass None).
    """
    from smtp_client import send_email_from_account

    oauth_provider = acct.get("oauth_provider")
    oauth_access_token = acct.get("oauth_access_token")
    smtp_password = acct.get("smtp_password_encrypted") or ""
    use_oauth_smtp = bool(oauth_provider and oauth_access_token and not smtp_password)
    account_id = acct.get("id")
    if use_oauth_smtp:
        expires = acct.get("oauth_token_expires")
        if expires and isinstance(expires, datetime) and expires.timestamp() < datetime.now(timezone.utc).timestamp():
            try:
                import oauth_providers as op_mod
                prov = op_mod.get_provider(oauth_provider)
                if prov and acct.get("oauth_refresh_token"):
                    tok = prov.refresh_token(acct["oauth_refresh_token"])
                    oauth_access_token = tok.get("access_token", oauth_access_token)
                    new_expires = time.time() + tok.get("expires_in", 3600)
                    new_refresh = tok.get("refresh_token", acct["oauth_refresh_token"])
                    _persist_exchange_refresh(account_id, oauth_access_token, new_refresh, new_expires)
            except Exception:
                logger.exception("Failed to refresh OAuth token for send")

    # Step 1: try Graph if this is a Microsoft OAuth account with a token.
    graph_token, graph_err = (None, None)
    if oauth_provider == "microsoft" and refresh_graph_token is not None:
        graph_token, graph_err = refresh_graph_token(account_id)
        if graph_token:
            from mail_graph_client import send_email_via_graph
            try:
                ok, err = send_email_via_graph(
                    graph_access_token=graph_token,
                    from_addr=acct.get("email", ""),
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
                logger.warning("Graph send failed for account=%s, falling back to SMTP: %s", account_id, err)
                graph_err = err
            except Exception as e:
                logger.exception("Graph send threw for account=%s", account_id)
                graph_err = str(e)

    # Step 2: SMTP path (works for Google OAuth, app passwords, Microsoft
    # accounts without a Graph token, and any account with manual SMTP).
    smtp_host = acct.get("smtp_host") or ""
    if not smtp_host:
        return False, graph_err or "SMTP not configured for this account", "graph" if graph_token else "smtp_password"
    try:
        ok, err = send_email_from_account(
            smtp_host=smtp_host,
            smtp_port=int(acct.get("smtp_port") or 587),
            smtp_user=acct.get("smtp_user") or acct.get("email", ""),
            smtp_password=smtp_password,
            from_name=from_name or acct.get("smtp_from_name"),
            from_addr=acct.get("email", ""),
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

    # Step 3: if SMTP failed with the canonical "SMTP AUTH disabled" error
    # AND a Graph token is available, retry Graph. Covers the case where the
    # routing above skipped Graph (e.g. the token expired between step 1 and
    # step 2).
    if err and ("5.7.139" in err or "SmtpClientAuthentication is disabled" in err):
        if oauth_provider == "microsoft" and refresh_graph_token is not None:
            retry_token, _retry_err = refresh_graph_token(account_id)
            if retry_token:
                from mail_graph_client import send_email_via_graph
                try:
                    ok2, err2 = send_email_via_graph(
                        graph_access_token=retry_token,
                        from_addr=acct.get("email", ""),
                        to_addr=to_addr,
                        subject=subject,
                        html_body=html_body,
                        text_body=text_body,
                        cc_addr=cc_addr,
                        bcc_addr=bcc_addr,
                        attachments=attachments,
                    )
                    if ok2:
                        return True, None, "graph"
                    err = err2 or err
                except Exception as e:
                    logger.exception("Graph retry after SMTP 5.7.139 threw")
                    err = str(e) or err
    return False, err or graph_err, "graph" if graph_token else ("smtp_oauth" if use_oauth_smtp else "smtp_password")


def _persist_exchange_refresh(account_id: int, access_token: str, refresh_token: str, expires_ts: float) -> None:
    """Persist a refreshed Exchange-audience OAuth token to the DB.
    Best-effort: silently logs and continues on failure (the caller will
    still try to send, just with the old token)."""
    try:
        import db as db_mod
        db_mod.execute(
            "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, oauth_token_expires = to_timestamp(%s) WHERE id = %s",
            (access_token, refresh_token, expires_ts, account_id),
        )
    except Exception:
        logger.exception("Failed to persist refreshed Exchange OAuth token for account %d", account_id)
