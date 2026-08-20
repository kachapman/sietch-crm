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

def _resolve_actor(notification: dict, actor_name: str) -> str:
    """Get actor name — prefer payload.actor_override, fall back to DB actor."""
    return (notification.get("payload") or {}).get("actor_override") or actor_name or "Someone"


def _project_link(project_title: str, project_id: int | None, project_number: int | None) -> str:
    """Build project link line."""
    link = f"{_get_dashboard_url()}/project/{project_number or project_id}" if (project_number or project_id) else ""
    if project_title and link:
        return f"<b>Project:</b> <a href=\"{link}\">{_escape_html(project_title)}</a>"
    elif project_title:
        return f"<b>Project:</b> {_escape_html(project_title)}"
    return ""


def _build_tg(header_emoji: str, header_text: str, project_title: str, project_id, project_number, content_lines: list[str], cta_reply: bool = False) -> str:
    """Build a Telegram notification in the established format."""
    lines = [f"{header_emoji} <b>{header_text}</b>", ""]
    proj_line = _project_link(project_title, project_id, project_number)
    if proj_line:
        lines.append(proj_line)
    lines.append("───────────────────────")
    lines.extend(content_lines)
    lines.append("───────────────────────")
    if cta_reply:
        lines.append("<code>Reply to add a note.</code>")
    else:
        link = f"{_get_dashboard_url()}/project/{project_number or project_id}" if (project_number or project_id) else ""
        if link:
            lines.append(f"\U0001f517 <a href=\"{link}\">View project</a>")
    return "\n".join(lines)


def _format_note_tagged(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    actor = _resolve_actor(notification, actor_name)
    snippet = _escape_html((notification.get("payload") or {}).get("snippet") or "")
    content = []
    if snippet:
        content.append(f"<blockquote>{_truncate(snippet)}</blockquote>")
    return _build_tg("\U0001f4cb", f"{actor} tagged you in a note", project_title, project_id, project_number, content, cta_reply=True)


def _format_task_assigned(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    payload = notification.get("payload") or {}
    task_title = _escape_html(payload.get("task_title") or "")
    due_date = payload.get("due_date") or "No due date"
    return _build_tg("\u2705", "Task assigned to you", project_title, project_id, project_number, [
        f"Task: {task_title}",
        f"Due: {due_date}",
    ])


def _format_task_reminder(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    task_title = _escape_html((notification.get("payload") or {}).get("taskTitle") or notification.get("message") or "Task reminder")
    return _build_tg("\u23f0", "Task reminder", project_title, project_id, project_number, [task_title])


def _format_calendar_reminder(notification: dict) -> str:
    summary = _escape_html((notification.get("payload") or {}).get("summary") or notification.get("message") or "Calendar event")
    return _build_tg("\U0001f4c5", "Calendar reminder", "", None, None, [summary, "", "Check your calendar for details."])


def _format_document_uploaded(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    actor = _resolve_actor(notification, actor_name)
    filename = _escape_html((notification.get("payload") or {}).get("filename") or notification.get("message") or "a file")
    return _build_tg("\U0001f4c4", "Document uploaded", project_title, project_id, project_number, [
        f"File: {filename}",
        f"By: {actor}",
    ])


def _format_photo_uploaded(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    actor = _resolve_actor(notification, actor_name)
    filename = _escape_html((notification.get("payload") or {}).get("filename") or notification.get("message") or "a photo")
    return _build_tg("\U0001f4f7", "Photo uploaded", project_title, project_id, project_number, [
        f"File: {filename}",
        f"By: {actor}",
    ])


def _format_task_added(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    payload = notification.get("payload") or {}
    task_title = _escape_html(payload.get("task_title") or "")
    due_date = payload.get("due_date") or "No due date"
    return _build_tg("\U0001f4cb", "New task added", project_title, project_id, project_number, [
        f"Task: {task_title}",
        f"Due: {due_date}",
    ])


def _format_task_completed(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    actor = _resolve_actor(notification, actor_name)
    task_title = _escape_html((notification.get("payload") or {}).get("task_title") or notification.get("message") or "Task")
    return _build_tg("\u2705", "Task completed", project_title, project_id, project_number, [
        f"Task: {task_title}",
        f"By: {actor}",
    ])


def _format_project_updated(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    actor = _resolve_actor(notification, actor_name)
    changes = _escape_html((notification.get("payload") or {}).get("changes") or notification.get("message") or "details updated")
    return _build_tg("\U0001f504", "Project updated", project_title, project_id, project_number, [
        f"Changes: {changes}",
        f"By: {actor}",
    ])


def _format_project_created(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str:
    actor = _resolve_actor(notification, actor_name)
    return _build_tg("\U0001f195", "New project created", project_title, project_id, project_number, [
        f"By: {actor}",
    ])


def _format_notification(notification: dict, project_title: str, actor_name: str, project_id: int | None = None, project_number: int | None = None) -> str | None:
    ntype = notification.get("type")
    if ntype == "note_tagged":
        return _format_note_tagged(notification, project_title, actor_name, project_id, project_number)
    elif ntype == "task_assigned":
        return _format_task_assigned(notification, project_title, actor_name, project_id, project_number)
    elif ntype == "task_reminder":
        return _format_task_reminder(notification, project_title, actor_name, project_id, project_number)
    elif ntype == "calendar_reminder":
        return _format_calendar_reminder(notification)
    elif ntype in ("document_uploaded", "document.uploaded"):
        return _format_document_uploaded(notification, project_title, actor_name, project_id, project_number)
    elif ntype in ("photo_uploaded", "photo.uploaded"):
        return _format_photo_uploaded(notification, project_title, actor_name, project_id, project_number)
    elif ntype in ("task_added", "task.added"):
        return _format_task_added(notification, project_title, actor_name, project_id, project_number)
    elif ntype in ("task_completed", "task.completed"):
        return _format_task_completed(notification, project_title, actor_name, project_id, project_number)
    elif ntype in ("project_updated", "project.updated"):
        return _format_project_updated(notification, project_title, actor_name, project_id, project_number)
    elif ntype in ("project_created", "project.created"):
        return _format_project_created(notification, project_title, actor_name, project_id, project_number)
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
                                project_id: int | None = None, project_number: int | None = None) -> tuple[str | None, str | None]:
    """Build plain text email from notification. Returns (subject, text) or (None, None)."""
    actor = _resolve_actor(notification, actor_name)
    payload = notification.get("payload") or {}
    link = f"{_get_dashboard_url()}/project/{project_number or project_id}" if (project_number or project_id) else ""

    ntype = notification.get("type")
    if ntype == "note_tagged":
        subject = f"{actor} tagged you on {project_title}"
        body = notification.get("message") or "You were tagged in a note."
    elif ntype == "task_assigned":
        task_title = payload.get("task_title", "")
        subject = f"Task assigned: {task_title}"
        body = f"{actor} assigned you a task on {project_title}."
        if payload.get("due_date"):
            body += f" Due: {payload['due_date']}."
    elif ntype == "task_reminder":
        task_name = payload.get("taskTitle", "Task")
        subject = f"Reminder: {task_name}"
        body = f"Reminder: {task_name}"
        if project_title:
            body += f" on {project_title}"
        body += "."
    elif ntype == "email_linked":
        subject = f"New email linked to {project_title}"
        body = notification.get("message") or "A new email was linked to this project."
    elif ntype == "deal_created":
        subject = notification.get("message") or "New project created"
        body = notification.get("message") or "A new project was auto-created from an email."
    elif ntype in ("document_uploaded", "document.uploaded"):
        filename = payload.get("filename", "document")
        subject = f"Document uploaded to {project_title}"
        body = f"{actor} uploaded {filename} to {project_title}."
    elif ntype in ("photo_uploaded", "photo.uploaded"):
        filename = payload.get("filename", "image")
        subject = f"Photo uploaded to {project_title}"
        body = f"{actor} uploaded {filename} to {project_title}."
    elif ntype in ("task_added", "task.added"):
        task_title = payload.get("task_title", "")
        subject = f"New task on {project_title}"
        body = f"A new task was added to {project_title}: {task_title}."
        if payload.get("due_date"):
            body += f" Due: {payload['due_date']}."
    elif ntype in ("task_completed", "task.completed"):
        task_title = payload.get("task_title", "Task")
        subject = f"Task completed on {project_title}"
        body = f"{actor} completed a task on {project_title}: {task_title}."
    elif ntype in ("project_updated", "project.updated"):
        changes = payload.get("changes") or "details updated"
        subject = f"Project updated: {project_title}"
        body = f"{actor} updated {project_title}. {changes}."
    elif ntype in ("project_created", "project.created"):
        subject = f"New project: {project_title}"
        body = f"{actor} created a new project: {project_title}."
    else:
        return None, None

    # Plain text — no HTML, no links to avoid spam filters
    text = body
    if link:
        text += f"\n\n{link}"

    return subject, text


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
                      o.title AS project_title,
                      o.project_number
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
        project_number = row.get("project_number")
        project_title = row.get("project_title") or "Unknown Project"
        actor_name = row.get("actor_name") or "Someone"

        # ── Telegram channel ──
        if not row.get("telegram_sent"):
            _process_telegram(notification_id, user_id, row, project_title, actor_name, project_id, project_number)
            if row.get("telegram_sent"):
                sent_count += 1

        # ── Email channel ──
        if not row.get("email_sent"):
            _process_email(notification_id, user_id, row, project_title, actor_name, project_id, project_number)
            if row.get("email_sent"):
                sent_count += 1

    return sent_count


def _process_telegram(notification_id: int, user_id: int, row: dict,
                       project_title: str, actor_name: str, project_id: int | None,
                       project_number: int | None = None) -> None:
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

    text = _format_notification(row, project_title, actor_name, project_id, project_number)
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
                    project_title: str, actor_name: str, project_id: int | None,
                    project_number: int | None = None) -> None:
    """Handle email delivery for a single notification."""
    email_pref = _get_email_pref(user_id)
    if email_pref != "immediate":
        logger.debug("Skipping email for user %s: preference=%s", user_id, email_pref)
        _mark_email_sent(notification_id)
        return

    user_email = _get_user_email(user_id)
    if not user_email:
        logger.debug("Skipping email for user %s: no email address", user_id)
        _mark_email_sent(notification_id)
        return

    subject, text_body = _format_email_notification(row, project_title, actor_name, project_id, project_number)
    if not subject or not text_body:
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
            html_body="",
            text_body=text_body,
            oauth_provider=smtp_acct["oauth_provider"] if use_oauth else None,
            oauth_access_token=smtp_acct["oauth_access_token"] if use_oauth else None,
        )
        if not success:
            logger.warning("CRM SMTP error for notification %s to %s: %s", notification_id, user_email, err)
    else:
        from smtp_client import send_email as _send_email
        success = _send_email(user_email, subject, text_body, from_name_override=from_name)

    if success:
        _mark_email_sent(notification_id)
    else:
        logger.warning("Failed to send email notification %s to %s", notification_id, user_email)


def _get_crm_smtp_account() -> dict | None:
    """Find the best SMTP config for notification emails.
    Priority: 1) Alternate SMTP if enabled, 2) CRM mail account.
    Returns dict with SMTP settings or None if nothing configured."""
    # Check alternate SMTP first (better deliverability)
    try:
        from db import query_one
        alt_row = query_one("SELECT value FROM system_settings WHERE key = 'alternate_smtp'")
        if alt_row and alt_row.get("value"):
            import json as _json
            alt = alt_row["value"] if isinstance(alt_row["value"], dict) else _json.loads(str(alt_row["value"]))
            if alt.get("enabled") and alt.get("host"):
                return {
                    "email": alt.get("from_email", ""),
                    "smtp_host": alt["host"],
                    "smtp_port": int(alt.get("port", 587)),
                    "smtp_user": alt.get("user", ""),
                    "smtp_password_encrypted": alt.get("password", ""),
                    "oauth_provider": None,
                    "oauth_access_token": None,
                }
    except Exception:
        logger.debug("Failed to load alternate SMTP for notifications", exc_info=True)
    # Fall back to CRM mail account
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
