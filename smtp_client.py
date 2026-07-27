"""SMTP client for Sietch CRM v3.0 — password reset emails and per-account sending."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("sietch.smtp")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "Sietch CRM")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"


def send_email(to_addr: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send email via external SMTP relay. Returns True on success, False on failure."""
    if not SMTP_HOST or not SMTP_USER:
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_USER}>"
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

    all_recipients = [to_addr]
    if cc_addr:
        all_recipients.extend([a.strip() for a in cc_addr.split(",") if a.strip()])
    if bcc_addr:
        all_recipients.extend([a.strip() for a in bcc_addr.split(",") if a.strip()])

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if use_tls:
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(from_addr, all_recipients, msg.as_string())
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        logger.error("SMTP auth failed for %s: %s", from_addr, e)
        return False, f"Authentication failed: {e}"
    except smtplib.SMTPConnectError as e:
        logger.error("SMTP connect failed for %s:%d: %s", smtp_host, smtp_port, e)
        return False, f"Connection failed: {e}"
    except Exception as e:
        logger.error("SMTP send failed for %s: %s", from_addr, e)
        return False, str(e)


def send_password_reset_email(to_addr: str, reset_url: str) -> bool:
    """Send a password reset email with the given link."""
    subject = "Sietch CRM — Password Reset"
    html_body = f"""
    <html>
    <body style="font-family: sans-serif; color: #333;">
        <h2>Password Reset</h2>
        <p>You requested a password reset for your Sietch CRM account.</p>
        <p>Click the link below to set a new password. This link expires in 1 hour.</p>
        <p><a href="{reset_url}" style="display:inline-block;padding:10px 20px;background:#6d4aff;color:#fff;text-decoration:none;border-radius:4px;">Reset Password</a></p>
        <p style="color:#888;font-size:12px;">If you did not request this, you can safely ignore this email.</p>
    </body>
    </html>
    """
    text_body = f"Sietch CRM — Password Reset\n\nClick the link to reset your password (expires in 1 hour):\n{reset_url}\n\nIf you did not request this, ignore this email."
    return send_email(to_addr, subject, html_body, text_body)
