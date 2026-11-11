"""SMTP client for Sietch CRM v3.0 — password reset emails and per-account sending."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from email.utils import formataddr, formatdate, make_msgid

logger = logging.getLogger("sietch.smtp")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Sietch CRM")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

# Headers that MUST survive a switch to multipart/mixed. Only From/To/Subject/Cc
# used to be copied onto the wrapper, so Date and Message-ID (when present)
# were silently dropped from the message that actually went on the wire.
_WRAPPER_HEADERS = ("From", "To", "Subject", "Cc", "Date", "Message-ID")


def message_domain(sender: str) -> str | None:
    """The domain of the address that is actually sending, or None.

    Used for `Message-ID`. There is deliberately NO default account: the
    domain comes from the From/SMTP login the caller passes, and a value with
    no "@" yields None (the caller then uses the local hostname, which is a
    Message-ID domain fallback, never a different mailbox).
    """
    addr = (sender or "").strip()
    if "@" not in addr:
        return None
    # A display-name form ("Name <user@example.com>") still resolves.
    addr = addr.rsplit("<", 1)[-1] if "<" in addr else addr
    domain = addr.rsplit("@", 1)[-1].strip().strip(">").strip()
    return domain or None


def ensure_message_headers(msg, sender: str) -> str:
    """Add `Date` and `Message-ID` to `msg` unless already present.

    Why (live bug 2026-11-11): SMTP sends from Sietch had NO Date and NO
    Message-ID. Bluehost stored them that way, imap_tools reported
    `date=1900-01-01`, and the scanner's 90-day retention guard then treated
    brand-new mail as ancient and skipped the import. A missing Message-ID
    also breaks threading and de-duplication for the receiving side.

    Existing headers are never overwritten. Returns the Message-ID on the
    message (callers persist it on the sent row).
    """
    if not msg.get("Date"):
        msg["Date"] = formatdate(localtime=True)
    existing = msg.get("Message-ID")
    if existing:
        return str(existing).strip()
    domain = message_domain(sender)
    msgid = make_msgid(domain=domain) if domain else make_msgid()
    msg["Message-ID"] = msgid
    return msgid


def _headers_for_wrapper(msg) -> dict[str, str]:
    """The headers copied onto a multipart/mixed wrapper (only those set)."""
    return {name: str(msg[name]) for name in _WRAPPER_HEADERS if msg.get(name)}


def send_email(to_addr: str, subject: str, html_body: str, text_body: str | None = None,
                from_name_override: str | None = None) -> bool:
    """Send email via external SMTP relay. Returns True on success, False on failure."""
    if not SMTP_HOST or not SMTP_USER:
        return False

    msg = MIMEMultipart("alternative")
    from_name = from_name_override or SMTP_FROM_NAME
    msg["From"] = f"{from_name} <{SMTP_USER}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    # Date + Message-ID are built from the address that actually sends
    # (SMTP_USER), never from a default/fallback account.
    ensure_message_headers(msg, SMTP_USER)

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_USE_TLS:
                server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception:
        return False


def send_email_from_account(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_name: str | None,
    from_addr: str,
    to_addr: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    cc_addr: str | None = None,
    bcc_addr: str | None = None,
    use_tls: bool = True,
    attachments: list[dict] | None = None,
    oauth_provider: str | None = None,
    oauth_access_token: str | None = None,
    result_meta: dict | None = None,
) -> tuple[bool, str | None]:
    """Send email via per-account SMTP settings. Returns (success, error_message).

    From identity rules (2026-11-10):
      * `from_name` / `from_addr` are the selected mail_accounts row's
        `smtp_from_name` / `email` (mail_send passes nothing else). The
        system-mail callers pass the CRM brand name deliberately — they are
        not a user-facing compose path.
      * a blank `from_name` produces `From: <address>` and nothing else — no
        borrowed display name.
      * the envelope sender equals the SMTP login, and the header From must
        equal that same mailbox: an account authenticating as a different
        address would be sending as the wrong mailbox, so it is refused.

    Every message leaves with a `Date` and a `Message-ID` (2026-11-11). The
    generated Message-ID uses the sending address's own domain and is written
    to `result_meta["message_id"]` on success (when a dict is passed) so the
    caller can persist it on the mail_outgoing row. The return shape stays
    (success, error) — existing callers are unaffected.
    """
    if not smtp_host or not smtp_user:
        return False, "SMTP not configured for this account"

    sender = (from_addr or "").strip()
    login = (smtp_user or "").strip()
    if not sender:
        return False, "Refusing to send: this mail account has no From address. Nothing was sent."
    if login and login.lower() != sender.lower():
        logger.warning("Refusing SMTP send: From %s but authenticating as %s", sender, login)
        return False, (
            f"Refusing to send: the From address is {sender} but this account "
            f"authenticates as {login}. Nothing was sent."
        )

    msg = MIMEMultipart("alternative")
    # formataddr quotes/encodes display names correctly ("Est, LLC" would
    # otherwise produce a malformed header).
    name = (from_name or "").strip()
    msg["From"] = formataddr((name, sender)) if name else sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    if cc_addr:
        msg["Cc"] = cc_addr
    # Date + Message-ID (2026-11-11): the missing headers were the cause of
    # the 1900-01-01 imports. Domain is the sending address's own.
    msgid = ensure_message_headers(msg, sender)

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    if attachments:
        # Switch to multipart/mixed so we can attach binary files alongside the alternative part.
        # Date and Message-ID must be copied too — only From/To/Subject/Cc were
        # copied before, so they were lost from the wire message whenever an
        # attachment was present.
        mixed = MIMEMultipart("mixed")
        for header_name, header_value in _headers_for_wrapper(msg).items():
            mixed[header_name] = header_value
        mixed.attach(msg)
        for att in attachments:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(att["content"])
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{att["filename"]}"')
            mixed.attach(part)
        msg = mixed

    all_recipients = [to_addr]
    if cc_addr:
        all_recipients.extend([a.strip() for a in cc_addr.split(",") if a.strip()])
    if bcc_addr:
        all_recipients.extend([a.strip() for a in bcc_addr.split(",") if a.strip()])

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if use_tls:
                server.starttls()
                server.ehlo()
            if oauth_provider and oauth_access_token:
                auth_str = f"user={smtp_user}\x01auth=Bearer {oauth_access_token}\x01\x01"
                server.auth("XOAUTH2", lambda x=None: auth_str)
            else:
                server.login(smtp_user, smtp_password)
            server.sendmail(sender, all_recipients, msg.as_string())
        if result_meta is not None:
            result_meta["message_id"] = msgid
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        logger.error("SMTP auth failed for %s: %s", from_addr, e)
        err_str = str(e)
        if "SmtpClientAuthentication is disabled" in err_str or "5.7.139" in err_str:
            return False, (
                "Microsoft has disabled SMTP sending for this Outlook account. "
                "Contact Microsoft Support and ask them to enable 'Authenticated SMTP' for this mailbox. "
                "Until then, use a different email provider or the Outlook web app."
            )
        return False, f"Authentication failed: {e}"
    except smtplib.SMTPConnectError as e:
        logger.error("SMTP connect failed for %s:%d: %s", smtp_host, smtp_port, e)
        return False, f"Connection failed: {e}"
    except Exception as e:
        logger.error("SMTP send failed for %s: %s", from_addr, e)
        return False, str(e)


def send_password_reset_email(to_addr: str, reset_url: str, smtp_config: dict | None = None) -> bool:
    """Send a password reset email as PLAIN TEXT only (no HTML part).

    Deliverability choice, Oct 2026: the previous multipart/alternative
    template (styled HTML + remote logo <img>) was consistently
    spam-foldered. This sends a single text/plain part: no HTML, no
    remote images, no tracking-adjacent markup. The reset URL is on its
    own line as bare text plus an explicit copy-paste instruction, so
    the message stays usable even in clients that don't linkify.
    """
    subject = "Your Sietch CRM password reset link"
    text_body = (
        "Hello,\n"
        "\n"
        "You requested a password reset for your Sietch CRM account.\n"
        "\n"
        "Open this link to set a new password (expires in 1 hour):\n"
        "\n"
        f"{reset_url}\n"
        "\n"
        "If the link above is not clickable, copy it and paste it into\n"
        "your browser's address bar.\n"
        "\n"
        "If you did not request this, ignore this message. Your password\n"
        "will not be changed unless you open the link above.\n"
        "\n"
        "-- Sietch CRM\n"
    )
    if smtp_config:
        use_oauth = bool(smtp_config.get("oauth_provider") and smtp_config.get("oauth_access_token")
                         and not smtp_config.get("smtp_password_encrypted"))
        ok, err = send_email_from_account(
            smtp_host=smtp_config.get("smtp_host") or "",
            smtp_port=int(smtp_config.get("smtp_port") or 587),
            smtp_user=smtp_config.get("smtp_user") or smtp_config.get("email", ""),
            smtp_password=smtp_config.get("smtp_password_encrypted") or "",
            from_name="Sietch CRM",
            from_addr=smtp_config.get("email", ""),
            to_addr=to_addr,
            subject=subject,
            html_body="",
            text_body=text_body,
            oauth_provider=smtp_config["oauth_provider"] if use_oauth else None,
            oauth_access_token=smtp_config["oauth_access_token"] if use_oauth else None,
        )
        if not ok:
            logger.warning("Password reset email to %s failed: %s", to_addr, err)
        return ok
    return send_email(to_addr, subject, "", text_body)
