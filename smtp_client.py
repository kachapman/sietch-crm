"""SMTP client for Sietch CRM v3.0 — password reset emails and per-account sending."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

logger = logging.getLogger("sietch.smtp")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Sietch CRM")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"


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

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
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
) -> tuple[bool, str | None]:
    """Send email via per-account SMTP settings. Returns (success, error_message)."""
    if not smtp_host or not smtp_user:
        return False, "SMTP not configured for this account"

    msg = MIMEMultipart("alternative")
    display_from = f"{from_name} <{from_addr}>" if from_name else from_addr
    msg["From"] = display_from
    msg["To"] = to_addr
    msg["Subject"] = subject
    if cc_addr:
        msg["Cc"] = cc_addr

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    if attachments:
        # Switch to multipart/mixed so we can attach binary files alongside the alternative part
        mixed = MIMEMultipart("mixed")
        mixed["From"] = msg["From"]
        mixed["To"] = msg["To"]
        mixed["Subject"] = msg["Subject"]
        if cc_addr:
            mixed["Cc"] = msg["Cc"]
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
            server.sendmail(from_addr, all_recipients, msg.as_string())
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
    """Send a password reset email. If smtp_config provided, use CRM mail account; else fallback to env vars."""
    subject = "Your Sietch CRM password reset link"
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f5f5f5;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:40px 20px;">
<tr><td align="center">
<table width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;">
<tr><td style="padding:32px 40px;text-align:center;border-bottom:1px solid #eeeeee;">
  <img src="https://dashboard.publicadjustermidwest.com/assets/sietch-logo-2-nobg2.png" alt="Sietch CRM" height="40" style="display:block;margin:0 auto 16px;">
  <h2 style="margin:0;font-size:20px;color:#333333;">Password Reset</h2>
</td></tr>
<tr><td style="padding:32px 40px;">
  <p style="margin:0 0 16px;font-size:15px;color:#555555;">You requested a password reset for your Sietch CRM account.</p>
  <p style="margin:0 0 24px;font-size:15px;color:#555555;">Use the link below to set a new password. This link expires in 1 hour.</p>
  <p style="margin:0 0 24px;font-size:15px;word-break:break-all;"><a href="{reset_url}" style="color:#6d4aff;">{reset_url}</a></p>
  <p style="margin:0;font-size:13px;color:#999999;">If you did not request this, you can safely ignore this email. Your password will not be changed unless you click the link above.</p>
</td></tr>
<tr><td style="padding:20px 40px;background:#f9f9f9;text-align:center;border-top:1px solid #eeeeee;">
  <p style="margin:0;font-size:12px;color:#aaaaaa;">Sietch CRM</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""
    text_body = f"Password Reset\n\nYou requested a password reset for your Sietch CRM account.\n\nUse the link below to set a new password (expires in 1 hour):\n{reset_url}\n\nIf you did not request this, ignore this email. Your password will not be changed unless you click the link."
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
            html_body=html_body,
            text_body=text_body,
            oauth_provider=smtp_config["oauth_provider"] if use_oauth else None,
            oauth_access_token=smtp_config["oauth_access_token"] if use_oauth else None,
        )
        if not ok:
            logger.warning("Password reset email to %s failed: %s", to_addr, err)
        return ok
    return send_email(to_addr, subject, html_body, text_body)
