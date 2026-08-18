"""
Notification Dispatcher — sends Telegram notifications for CRM events.

Runs as a background thread inside server.py. Polls the notifications table
for unsent Telegram notifications, looks up employee chat IDs, and sends
formatted messages via the Telegram Bot API.
"""

import logging
import os
import threading
import time
from html import escape as _esc
from typing import Any

import httpx

import db
import crm_bot_store
import infra_log

logger = logging.getLogger("notification_dispatcher")

# ── Config ──────────────────────────────────────────────────────────────────

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://127.0.0.1:8766")

def _get_dashboard_url() -> str:
    """Read DASHBOARD_URL lazily to pick up .env changes."""
    return os.getenv("DASHBOARD_URL", DASHBOARD_URL)
TELEGRAM_NOTIFY_ENABLED = os.getenv("TELEGRAM_NOTIFY_ENABLED", "true").lower() == "true"
POLL_INTERVAL = int(os.getenv("TELEGRAM_NOTIFY_POLL_INTERVAL", "5"))
BATCH_SIZE = int(os.getenv("TELEGRAM_NOTIFY_BATCH_SIZE", "10"))
PORTAL = "sietch"  # TODO: make configurable per deployment

# Bot token: DB-first with env fallback (60s cache)
_token_cache: str = ""
_token_cache_ts: float = 0


def _get_bot_token() -> str:
    """Return bot token from bot_config DB table (cached 60s), falling back to env."""
    global _token_cache, _token_cache_ts
    now = time.time()
    if _token_cache and now - _token_cache_ts < 60:
        return _token_cache
    try:
        row = db.query_one("SELECT value FROM bot_config WHERE key = 'settings'")
        if row and row.get("value"):
            import json as _json
            cfg = row["value"] if isinstance(row["value"], dict) else _json.loads(str(row["value"]))
            token = cfg.get("token", "")
            if token:
                _token_cache = token
                _token_cache_ts = now
                return token
    except Exception:
        pass
    fallback = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _token_cache = fallback
    _token_cache_ts = now
    return fallback


def _truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _escape_html(text: str) -> str:
    return _esc(str(text))


# ── Notification formatting ─────────────────────────────────────────────────

def _format_note_tagged(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    snippet = _escape_html(payload.get("snippet") or "")
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"<b>{_escape_html(actor_name)} tagged you in a note</b>\n"
        f"<b>Project:</b> <a href=\"{link}\">{_escape_html(project_title)}</a>\n"
        f"───────────────────────\n"
        + (f"<blockquote>{_truncate(snippet)}</blockquote>\n" if snippet else "\n")
        + "───────────────────────\n"
        f"<code>Reply to add a note.</code>"
    )


def _format_task_assigned(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    task_title = _escape_html(payload.get("task_title") or "")
    due_date = payload.get("due_date") or "No due date"
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\u2705 <b>Task assigned to you</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"Task: {task_title}\n"
        f"Due: {due_date}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
        + f"\u21a9\ufe0f Reply to add a note"
    )


def _format_task_reminder(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    task_title = _escape_html(payload.get("taskTitle") or notification.get("message") or "Task reminder")
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\u23f0 <b>Task reminder</b>\n\n"
        f"{task_title}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
        + f"\u21a9\ufe0f Reply to add a note"
    )


def _format_calendar_reminder(notification: dict) -> str:
    payload = notification.get("payload") or {}
    summary = _escape_html(payload.get("summary") or notification.get("message") or "Calendar event")
    return (
        f"\U0001f4c5 <b>Calendar reminder</b>\n\n"
        f"{summary}\n\n"
        f"Check your calendar for details."
    )


def _format_document_uploaded(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    filename = _escape_html(payload.get("filename") or notification.get("message") or "a file")
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\U0001f4c4 <b>Document uploaded</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"By: {_escape_html(actor_name)}\n"
        f"File: {filename}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
    )


def _format_photo_uploaded(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    filename = _escape_html(payload.get("filename") or notification.get("message") or "a photo")
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\U0001f4f7 <b>Photo uploaded</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"By: {_escape_html(actor_name)}\n"
        f"File: {filename}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
    )


def _format_task_added(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    task_title = _escape_html(payload.get("task_title") or "")
    due_date = payload.get("due_date") or "No due date"
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\U0001f4cb <b>New task added</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"Task: {task_title}\n"
        f"Due: {due_date}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
    )


def _format_task_completed(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    task_title = _escape_html(payload.get("task_title") or notification.get("message") or "Task")
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\u2705 <b>Task completed</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"Task: {task_title}\n"
        f"By: {_escape_html(actor_name)}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
    )


def _format_project_updated(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    changes = _escape_html(payload.get("changes") or notification.get("message") or "details updated")
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\U0001f504 <b>Project updated</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"By: {_escape_html(actor_name)}\n"
        f"Changes: {changes}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
    )


def _format_project_created(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""
    return (
        f"\U0001f195 <b>New project created</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"By: {_escape_html(actor_name)}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
    )


def _format_notification(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str | None:
    ntype = notification.get("type")
    if ntype == "note_tagged":
        return _format_note_tagged(notification, project_title, actor_name, project_id)
    elif ntype == "task_assigned":
        return _format_task_assigned(notification, project_title, actor_name, project_id)
    elif ntype == "task_reminder":
        return _format_task_reminder(notification, project_title, actor_name, project_id)
    elif ntype == "calendar_reminder":
        return _format_calendar_reminder(notification)
    elif ntype in ("document_uploaded", "document.uploaded"):
        return _format_document_uploaded(notification, project_title, actor_name, project_id)
    elif ntype in ("photo_uploaded", "photo.uploaded"):
        return _format_photo_uploaded(notification, project_title, actor_name, project_id)
    elif ntype in ("task_added", "task.added"):
        return _format_task_added(notification, project_title, actor_name, project_id)
    elif ntype in ("task_completed", "task.completed"):
        return _format_task_completed(notification, project_title, actor_name, project_id)
    elif ntype in ("project_updated", "project.updated"):
        return _format_project_updated(notification, project_title, actor_name, project_id)
    elif ntype in ("project_created", "project.created"):
        return _format_project_created(notification, project_title, actor_name, project_id)
    return None


# ── Email formatting ───────────────────────────────────────────────────────

_branding_cache: dict[str, Any] | None = None
_branding_cache_ts: float = 0

def _get_branding() -> dict[str, Any]:
    """Fetch branding row, cached for 60s."""
    global _branding_cache, _branding_cache_ts
    now = time.time()
    if _branding_cache and now - _branding_cache_ts < 60:
        return _branding_cache
    try:
        row = db.query_one(
            "SELECT company_name, logo_path, primary_color, email_header_color, "
            "email_footer_text, email_from_name FROM branding ORDER BY id LIMIT 1"
        )
        if row:
            _branding_cache = {
                "company_name": row.get("company_name"),
                "logo_path": row.get("logo_path"),
                "primary_color": row.get("primary_color"),
                "email_header_color": row.get("email_header_color"),
                "email_footer_text": row.get("email_footer_text"),
                "email_from_name": row.get("email_from_name"),
            }
            _branding_cache_ts = now
            return _branding_cache
    except Exception:
        logger.exception("Failed to load branding for email")
    return {}


def _format_email_notification(notification: dict, project_title: str, actor_name: str,
                                project_id: int | None = None) -> tuple[str | None, str | None]:
    """Build branded HTML email from notification. Returns (subject, html) or (None, None)."""
    branding = _get_branding()
    header_color = branding.get("email_header_color") or branding.get("primary_color") or "#3b82f6"
    company = branding.get("company_name") or "Sietch CRM"
    footer = branding.get("email_footer_text") or "Sietch CRM — Confidential"
    link = f"{_get_dashboard_url()}/project/{project_id}" if project_id else ""

    ntype = notification.get("type")
    if ntype == "note_tagged":
        subject = f"{actor_name} tagged you on {project_title}"
        body = notification.get("message") or "You were tagged in a note."
    elif ntype == "task_assigned":
        payload = notification.get("payload") or {}
        subject = f"Task assigned: {payload.get('task_title', '')}"
        body = f"You've been assigned a task on {project_title}."
        if payload.get("due_date"):
            body += f" Due: {payload['due_date']}."
    elif ntype == "task_reminder":
        payload = notification.get("payload") or {}
        subject = f"Reminder: {payload.get('taskTitle', 'Task')}"
        body = payload.get("taskTitle") or notification.get("message") or "Task reminder."
    elif ntype == "email_linked":
        subject = f"New email linked to {project_title}"
        body = notification.get("message") or "A new email was linked to this project."
    elif ntype == "deal_created":
        subject = notification.get("message") or "New project created"
        body = notification.get("message") or "A new project was auto-created from an email."
    elif ntype in ("document_uploaded", "document.uploaded"):
        payload = notification.get("payload") or {}
        subject = f"Document uploaded to {project_title}"
        body = f"{actor_name} uploaded a file ({payload.get('filename', 'document')}) to {project_title}."
    elif ntype in ("photo_uploaded", "photo.uploaded"):
        payload = notification.get("payload") or {}
        subject = f"Photo uploaded to {project_title}"
        body = f"{actor_name} uploaded a photo ({payload.get('filename', 'image')}) to {project_title}."
    elif ntype in ("task_added", "task.added"):
        payload = notification.get("payload") or {}
        subject = f"New task on {project_title}"
        body = f"A new task was added to {project_title}: {payload.get('task_title', '')}."
        if payload.get("due_date"):
            body += f" Due: {payload['due_date']}."
    elif ntype in ("task_completed", "task.completed"):
        subject = f"Task completed on {project_title}"
        body = f"{actor_name} completed a task on {project_title}."
    elif ntype in ("project_updated", "project.updated"):
        subject = f"Project updated: {project_title}"
        body = f"{actor_name} updated {project_title}."
    elif ntype in ("project_created", "project.created"):
        subject = f"New project: {project_title}"
        body = f"{actor_name} created a new project: {project_title}."
    else:
        return None, None

    cta = ""
    if link:
        cta = (f'<p style="text-align:center;margin:24px 0;">'
               f'<a href="{link}" style="display:inline-block;padding:12px 24px;'
               f'background:{header_color};color:#fff;text-decoration:none;'
               f'border-radius:6px;font-weight:600;">View Project</a></p>')

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="background:{header_color};padding:24px;text-align:center;">
  <h1 style="color:#fff;margin:0;font-size:20px;">{company}</h1>
</div>
<div style="padding:32px 24px;max-width:600px;margin:0 auto;">
  <h2 style="color:#1a1a1a;margin:0 0 16px;">{subject}</h2>
  <p style="color:#333;font-size:15px;line-height:1.6;">{body}</p>
  {cta}
</div>
<div style="padding:16px 24px;text-align:center;color:#999;font-size:12px;border-top:1px solid #eee;">
  {footer}
</div>
</body>
</html>"""

    return subject, html


def _get_user_email(user_id: int) -> str | None:
    """Look up a user's email address."""
    try:
        row = db.query_one("SELECT email FROM users WHERE id = %s AND is_active = TRUE", (user_id,))
        return row["email"] if row else None
    except Exception:
        return None


def _get_email_pref(user_id: int) -> str | None:
    """Get email notification preference for a user. Returns 'immediate', 'disabled', or None."""
    try:
        row = db.query_one(
            "SELECT enabled FROM notification_preferences WHERE user_id = %s AND notification_type = 'email_digest'",
            (user_id,),
        )
        if row is None:
            return "disabled"  # default
        val = row["enabled"]
        if val is True or val == "true" or val == "immediate":
            return "immediate"
        return "disabled"
    except Exception:
        return None


def _mark_email_sent(notification_id: int) -> None:
    """Mark a notification as sent via email."""
    try:
        db.execute(
            "UPDATE notifications SET email_sent = TRUE, email_sent_at = NOW() WHERE id = %s",
            (notification_id,),
        )
    except Exception:
        logger.exception("Failed to mark notification %s as email_sent", notification_id)


# ── Telegram API ────────────────────────────────────────────────────────────

def _send_telegram_message(chat_id: int, text: str, reply_to_message_id: int | None = None) -> int | None:
    """Send a Telegram message. Returns message_id on success, None on failure."""
    token = _get_bot_token()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set, cannot send notification")
        return None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload)
            data = resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            else:
                logger.error("Telegram API error: %s", data.get("description", resp.text))
                return None
    except Exception:
        logger.exception("Failed to send Telegram message to chat %s", chat_id)
        return None


# ── Employee lookup ─────────────────────────────────────────────────────────

def _get_employee_chat_id(user_id: int) -> int | None:
    """Look up an employee's Telegram chat_id by their CRM user_id."""
    mappings = crm_bot_store.list_mappings(PORTAL)
    for m in mappings:
        if m.get("employee") and m.get("userId") == user_id and m.get("chatId"):
            return m["chatId"]
    return None


# ── Main dispatcher ─────────────────────────────────────────────────────────

def _process_pending_notifications() -> int:
    """Process unsent notifications for both Telegram and email channels.
    Returns number of notifications sent (across both channels)."""
    from smtp_client import send_email

    try:
        # Find notifications that need at least one channel sent
        rows = db.query_dicts(
            """SELECT n.id, n.user_id, n.type, n.message, n.payload,
                      n.opportunity_id, n.actor_user_id,
                      n.telegram_sent, n.email_sent,
                      u.display_name AS actor_name,
                      o.title AS project_title
               FROM notifications n
               LEFT JOIN users u ON u.id = n.actor_user_id
               LEFT JOIN opportunities o ON o.id = n.opportunity_id
               WHERE (n.telegram_sent IS NOT TRUE OR n.email_sent IS NOT TRUE)
               ORDER BY n.created_at ASC
               LIMIT %s""",
            (BATCH_SIZE,),
        )
    except Exception:
        logger.exception("Failed to query pending notifications")
        return 0

    sent_count = 0
    for row in rows:
        notification_id = row["id"]
        user_id = row["user_id"]
        project_id = row.get("opportunity_id")
        project_title = row.get("project_title") or "Unknown Project"
        actor_name = row.get("actor_name") or "Someone"

        # ── Telegram channel ──
        if not row.get("telegram_sent"):
            _process_telegram(notification_id, user_id, row, project_title, actor_name, project_id)
            if row.get("telegram_sent"):
                sent_count += 1

        # ── Email channel ──
        if not row.get("email_sent"):
            _process_email(notification_id, user_id, row, project_title, actor_name, project_id)
            if row.get("email_sent"):
                sent_count += 1

    return sent_count


def _process_telegram(notification_id: int, user_id: int, row: dict,
                       project_title: str, actor_name: str, project_id: int | None) -> None:
    """Handle Telegram delivery for a single notification."""
    ntype = row.get("type", "")
    if not _user_telegram_enabled(user_id, ntype):
        _mark_telegram_sent(notification_id)
        return

    if _is_customer(user_id):
        _mark_telegram_sent(notification_id)
        return

    chat_id = _get_employee_chat_id(user_id)
    if not chat_id:
        _mark_telegram_sent(notification_id)
        return

    text = _format_notification(row, project_title, actor_name, project_id)
    if not text:
        _mark_telegram_sent(notification_id)
        return

    message_id = _send_telegram_message(chat_id, text)
    if message_id:
        _log_telegram_message(notification_id, chat_id, message_id)
        _mark_telegram_sent(notification_id)
    else:
        logger.warning("Failed to send notification %s to chat %s", notification_id, chat_id)


def _process_email(notification_id: int, user_id: int, row: dict,
                    project_title: str, actor_name: str, project_id: int | None) -> None:
    """Handle email delivery for a single notification."""
    email_pref = _get_email_pref(user_id)
    if email_pref != "immediate":
        _mark_email_sent(notification_id)
        return

    user_email = _get_user_email(user_id)
    if not user_email:
        _mark_email_sent(notification_id)
        return

    subject, html = _format_email_notification(row, project_title, actor_name, project_id)
    if not subject or not html:
        _mark_email_sent(notification_id)
        return

    branding = _get_branding()
    from_name = branding.get("email_from_name")

    # Try CRM mail account first, fall back to env vars
    smtp_acct = _get_crm_smtp_account()
    if smtp_acct:
        from smtp_client import send_email_from_account
        use_oauth = bool(smtp_acct.get("oauth_provider") and smtp_acct.get("oauth_access_token")
                         and not smtp_acct.get("smtp_password_encrypted"))
        success, err = send_email_from_account(
            smtp_host=smtp_acct.get("smtp_host") or "",
            smtp_port=int(smtp_acct.get("smtp_port") or 587),
            smtp_user=smtp_acct.get("smtp_user") or smtp_acct.get("email", ""),
            smtp_password=smtp_acct.get("smtp_password_encrypted") or "",
            from_name=from_name or "Sietch CRM",
            from_addr=smtp_acct.get("email", ""),
            to_addr=user_email,
            subject=subject,
            html_body=html,
            oauth_provider=smtp_acct["oauth_provider"] if use_oauth else None,
            oauth_access_token=smtp_acct["oauth_access_token"] if use_oauth else None,
        )
        if not success:
            logger.warning("CRM SMTP error for notification %s to %s: %s", notification_id, user_email, err)
    else:
        from smtp_client import send_email as _send_email
        success = _send_email(user_email, subject, html, from_name_override=from_name)

    if success:
        _mark_email_sent(notification_id)
    else:
        logger.warning("Failed to send email notification %s to %s", notification_id, user_email)


def _get_crm_smtp_account() -> dict | None:
    """Find CRM mail account for system notification emails."""
    try:
        from db import query_one
        row = query_one(
            """SELECT email, smtp_host, smtp_port, smtp_user, smtp_password_encrypted,
                      oauth_provider, oauth_access_token, oauth_refresh_token, oauth_token_expires
               FROM mail_accounts WHERE is_crm_mail = TRUE AND smtp_host IS NOT NULL
               ORDER BY id LIMIT 1"""
        )
        if not row:
            return None
        acct = dict(row)
        # Refresh OAuth token if expired
        if acct.get("oauth_provider") and acct.get("oauth_refresh_token"):
            from datetime import datetime, timezone
            expires = acct.get("oauth_token_expires")
            if expires and expires < datetime.now(timezone.utc):
                import oauth_providers
                prov = oauth_providers.get_provider(acct["oauth_provider"])
                tok = prov.refresh_token(acct["oauth_refresh_token"])
                if tok and tok.get("access_token"):
                    acct["oauth_access_token"] = tok["access_token"]
                    new_refresh = tok.get("refresh_token", acct["oauth_refresh_token"])
                    new_expires = tok.get("expires_in", 3600)
                    import time as _time
                    from db import execute
                    execute(
                        "UPDATE mail_accounts SET oauth_access_token = %s, oauth_refresh_token = %s, "
                        "oauth_token_expires = to_timestamp(%s) WHERE email = %s",
                        (tok["access_token"], new_refresh, int(_time.time()) + new_expires, acct["email"]),
                    )
        return acct
    except Exception:
        logger.debug("Could not load CRM mail account for notifications", exc_info=True)
        return None


def _user_telegram_enabled(user_id: int, notification_type: str = "") -> bool:
    """Check if user has Telegram notifications enabled in their preferences.
    Also checks per-type preferences (e.g., telegram_note_tagged) if notification_type is provided."""
    try:
        row = db.query_one(
            "SELECT enabled FROM notification_preferences WHERE user_id = %s AND notification_type = 'telegram'",
            (user_id,),
        )
        if row is not None and not row["enabled"]:
            return False
        # Check per-type override
        if notification_type:
            type_row = db.query_one(
                "SELECT enabled FROM notification_preferences WHERE user_id = %s AND notification_type = %s",
                (user_id, f"telegram_{notification_type}"),
            )
            if type_row is not None and not type_row["enabled"]:
                return False
        return True
    except Exception:
        return True


def _is_customer(user_id: int) -> bool:
    """Check if user is a customer (not an employee) via crm_bot_store."""
    mappings = crm_bot_store.list_mappings(PORTAL)
    for m in mappings:
        if m.get("userId") == user_id:
            return not m.get("employee", False)
    return False  # Not found in mappings — assume not customer


def _mark_telegram_sent(notification_id: int) -> None:
    try:
        db.execute(
            "UPDATE notifications SET telegram_sent = TRUE, telegram_sent_at = NOW() WHERE id = %s",
            (notification_id,),
        )
    except Exception:
        logger.exception("Failed to mark notification %s as sent", notification_id)


def _log_telegram_message(notification_id: int, chat_id: int, message_id: int) -> None:
    try:
        db.execute(
            "INSERT INTO telegram_notification_log (notification_id, chat_id, message_id) VALUES (%s, %s, %s)",
            (notification_id, chat_id, message_id),
        )
    except Exception:
        logger.exception("Failed to log Telegram message for notification %s", notification_id)


# ── Background thread ───────────────────────────────────────────────────────

def _dispatcher_loop(stop_event: threading.Event) -> None:
    """Main dispatcher loop — polls for pending notifications."""
    logger.info("Notification dispatcher started (interval=%ds, batch=%d)", POLL_INTERVAL, BATCH_SIZE)
    while not stop_event.is_set():
        try:
            sent = _process_pending_notifications()
            if sent:
                logger.info("Sent %d Telegram notification(s)", sent)
        except Exception:
            logger.exception("Dispatcher loop error")
            infra_log.log_infra_event("error", "Notification dispatcher loop error", source="dispatcher")
        stop_event.wait(POLL_INTERVAL)
    logger.info("Notification dispatcher stopped")


def start_dispatcher() -> threading.Event:
    """Start the notification dispatcher background thread. Returns stop event."""
    stop_event = threading.Event()
    if not TELEGRAM_NOTIFY_ENABLED:
        logger.info("Telegram notifications disabled (TELEGRAM_NOTIFY_ENABLED=false)")
        return stop_event
    t = threading.Thread(target=_dispatcher_loop, args=(stop_event,), daemon=True, name="telegram-notifier")
    t.start()
    return stop_event


def stop_dispatcher(stop_event: threading.Event) -> None:
    """Signal the dispatcher to stop."""
    stop_event.set()
