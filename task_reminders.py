"""Task reminder sweep — fires due task reminders as in-app notifications.

A reminder is `remind_at` set on a task (Odysseus-style: due date with a time
component, or an explicit remind time). The sweep runs every 30s, creates one
`task_reminder` notification per due task (owned by the task's owner), marks the
reminder as fired, and reschedules repeating reminders (daily / weekly / monthly /
yearly) for their next occurrence.

Repeat grammar:
    daily
    weekly:W          W = 1..7 (ISO weekday, Monday=1)
    monthly:D         D = day of month (1..31, clamped to month end)
    yearly:M-D        M = month 1..12, D = day of month
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

import db

logger = logging.getLogger("task-reminders")

POLL_INTERVAL = 30  # seconds
_notification_type = "task_reminder"


def _parse_repeat(repeat: str) -> tuple[str, tuple[int, ...]] | None:
    """Parse a repeat rule into (kind, args). Returns None if not repeating."""
    if not repeat or repeat.strip() == "" or repeat.strip().lower() in ("none", "false"):
        return None
    parts = [p.strip() for p in str(repeat).split(":")]
    kind = parts[0].lower()
    args: tuple[int, ...] = ()
    try:
        if kind == "daily":
            pass
        elif kind == "weekly" and len(parts) > 1:
            args = (int(parts[1]),)  # ISO weekday 1..7
        elif kind == "monthly" and len(parts) > 1:
            args = (int(parts[1]),)  # day of month
        elif kind == "yearly" and len(parts) > 1:
            m, d = parts[1].split("-")
            args = (int(m), int(d))
        else:
            return None
    except (ValueError, IndexError):
        return None
    return kind, args


def _next_occurrence(from_dt: datetime, rule: tuple[str, tuple[int, ...]]) -> datetime | None:
    """Compute the next remind time after from_dt for a repeat rule."""
    kind, args = rule
    try:
        if kind == "daily":
            return from_dt + timedelta(days=1)
        if kind == "weekly":
            weekday = args[0]  # 1..7
            days_ahead = (weekday - from_dt.isoweekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return from_dt + timedelta(days=days_ahead)
        if kind == "monthly":
            day = min(args[0], 28)
            cand = from_dt.replace(day=day)
            if cand <= from_dt:
                y, m = from_dt.year, from_dt.month
                if m == 12:
                    y, m = y + 1, 1
                else:
                    m += 1
                cand = from_dt.replace(year=y, month=m, day=day)
            return cand.replace(hour=from_dt.hour, minute=from_dt.minute, second=0, microsecond=0)
        if kind == "yearly":
            month, day = args
            candidate = from_dt.replace(year=from_dt.year, month=month, day=min(day, 28))
            if candidate <= from_dt:
                candidate = from_dt.replace(year=from_dt.year + 1, month=month, day=min(day, 28))
            return candidate.replace(hour=from_dt.hour, minute=from_dt.minute, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None
    return None


def _sweep_once() -> int:
    """Fire one batch of due reminders. Returns the number of notifications created."""
    rows = db.query_dicts(
        """SELECT id, title, owner_user_id, remind_at, repeat, opportunity_id
           FROM tasks
           WHERE remind_at IS NOT NULL
             AND remind_at <= NOW()
             AND reminder_fired_at IS NULL
             AND is_closed = FALSE
           LIMIT 100"""
    )
    count = 0
    for task in rows:
        task_id = task["id"]
        owner_id = task["owner_user_id"]
        title = task["title"] or "Task"
        if not owner_id:
            db.execute("UPDATE tasks SET reminder_fired_at = NOW() WHERE id = %s", (task_id,))
            continue
        try:
            db.execute(
                """INSERT INTO notifications (user_id, type, message, payload, created_at)
                   VALUES (%s, %s, %s, %s, NOW())""",
                (
                    owner_id,
                    _notification_type,
                    f"Reminder: {title}",
                    json.dumps({"taskId": task_id, "taskTitle": title}),
                ),
            )
            count += 1
        except Exception:
            logger.exception("Failed to insert reminder notification for task %s", task_id)
            continue

        # Reschedule repeating reminder, or mark fired permanently.
        rule = _parse_repeat(task.get("repeat"))
        if rule:
            remind_at = task.get("remind_at")
            nxt = _next_occurrence(remind_at, rule) if remind_at else None
            if nxt:
                db.execute(
                    "UPDATE tasks SET reminder_fired_at = NOW(), remind_at = %s, updated_at = NOW() WHERE id = %s",
                    (nxt, task_id),
                )
            else:
                db.execute(
                    "UPDATE tasks SET reminder_fired_at = NOW(), updated_at = NOW() WHERE id = %s",
                    (task_id,),
                )
        else:
            db.execute(
                "UPDATE tasks SET reminder_fired_at = NOW(), updated_at = NOW() WHERE id = %s",
                (task_id,),
            )
    return count


def _sweep_loop(stop_event: threading.Event) -> None:
    logger.info("Task reminder sweep started (interval=%ds)", POLL_INTERVAL)
    while not stop_event.is_set():
        try:
            fired = _sweep_once()
            if fired:
                logger.info("Fired %d task reminder(s)", fired)
        except Exception:
            logger.exception("Task reminder sweep error")
        stop_event.wait(POLL_INTERVAL)
    logger.info("Task reminder sweep stopped")


def start_reminder_sweep() -> threading.Event:
    """Start the reminder sweep background thread. Returns stop event."""
    stop_event = threading.Event()
    t = threading.Thread(target=_sweep_loop, args=(stop_event,), daemon=True, name="task-reminders")
    t.start()
    return stop_event


def stop_reminder_sweep(stop_event: threading.Event) -> None:
    """Signal the sweep to stop."""
    stop_event.set()
