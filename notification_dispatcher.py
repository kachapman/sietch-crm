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

logger = logging.getLogger("notification_dispatcher")

# ── Config ──────────────────────────────────────────────────────────────────

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://127.0.0.1:8766")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_NOTIFY_ENABLED = os.getenv("TELEGRAM_NOTIFY_ENABLED", "true").lower() == "true"
POLL_INTERVAL = int(os.getenv("TELEGRAM_NOTIFY_POLL_INTERVAL", "5"))
BATCH_SIZE = int(os.getenv("TELEGRAM_NOTIFY_BATCH_SIZE", "10"))
PORTAL = "sietch"  # TODO: make configurable per deployment


def _truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _escape_html(text: str) -> str:
    return _esc(str(text))


# ── Notification formatting ─────────────────────────────────────────────────

def _format_note_tagged(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    msg = _escape_html(notification.get("message") or "")
    link = f"{DASHBOARD_URL}/project/{project_id}" if project_id else ""
    return (
        f"\U0001f4cb <b>Note tagged you</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"By: {_escape_html(actor_name)}\n\n"
        f"{_truncate(msg)}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
        + f"\u21a9\ufe0f Reply to add a note"
    )


def _format_task_assigned(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str:
    payload = notification.get("payload") or {}
    task_title = _escape_html(payload.get("task_title") or "")
    due_date = payload.get("due_date") or "No due date"
    link = f"{DASHBOARD_URL}/project/{project_id}" if project_id else ""
    return (
        f"\u2705 <b>Task assigned to you</b>\n\n"
        f"Project: <b>{_escape_html(project_title)}</b>\n"
        f"Task: {task_title}\n"
        f"Due: {due_date}\n\n"
        + (f"\U0001f517 <a href=\"{link}\">View project</a>\n" if link else "")
        + f"\u21a9\ufe0f Reply to add a note"
    )


def _format_notification(notification: dict, project_title: str, actor_name: str, project_id: int | None = None) -> str | None:
    ntype = notification.get("type")
    if ntype == "note_tagged":
        return _format_note_tagged(notification, project_title, actor_name, project_id)
    elif ntype == "task_assigned":
        return _format_task_assigned(notification, project_title, actor_name, project_id)
    return None


# ── Telegram API ────────────────────────────────────────────────────────────

def _send_telegram_message(chat_id: int, text: str, reply_to_message_id: int | None = None) -> int | None:
    """Send a Telegram message. Returns message_id on success, None on failure."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set, cannot send notification")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
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
    """Process unsent notifications. Returns number of notifications sent."""
    if not TELEGRAM_BOT_TOKEN:
        return 0

    try:
        # Find notifications not yet sent via Telegram
        rows = db.query(
            """SELECT n.id, n.user_id, n.type, n.message, n.payload,
                      n.opportunity_id, n.actor_user_id,
                      u.display_name AS actor_name,
                      o.title AS project_title
               FROM notifications n
               LEFT JOIN users u ON u.id = n.actor_user_id
               LEFT JOIN opportunities o ON o.id = n.opportunity_id
               WHERE n.telegram_sent IS NOT TRUE
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

        # Check if user has Telegram notifications enabled
        if not _user_telegram_enabled(user_id):
            _mark_telegram_sent(notification_id)
            continue

        # Skip customers — only employees get Telegram notifications
        if _is_customer(user_id):
            _mark_telegram_sent(notification_id)
            continue

        # Look up employee chat_id
        chat_id = _get_employee_chat_id(user_id)
        if not chat_id:
            # Employee not linked to bot — mark as sent to avoid retrying
            _mark_telegram_sent(notification_id)
            continue

        # Format message
        project_id = row.get("opportunity_id")
        text = _format_notification(
            row,
            row.get("project_title") or "Unknown Project",
            row.get("actor_name") or "Someone",
            project_id,
        )
        if not text:
            _mark_telegram_sent(notification_id)
            continue

        # Send
        message_id = _send_telegram_message(chat_id, text)
        if message_id:
            _log_telegram_message(notification_id, chat_id, message_id)
            _mark_telegram_sent(notification_id)
            sent_count += 1
        else:
            # Failed to send — will retry on next poll
            logger.warning("Failed to send notification %s to chat %s", notification_id, chat_id)

    return sent_count


def _user_telegram_enabled(user_id: int) -> bool:
    """Check if user has Telegram notifications enabled in their preferences."""
    try:
        row = db.query_one(
            "SELECT enabled FROM notification_preferences WHERE user_id = %s AND notification_type = 'telegram'",
            (user_id,),
        )
        if row is None:
            return True  # Default: enabled
        return bool(row[0])
    except Exception:
        return True  # Default: enabled on error


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
