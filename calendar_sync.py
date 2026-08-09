"""Calendar sync service — background ICS feed refresh + reminder sweep.

Runs as a daemon thread inside server.py. Handles:
1. ICS feed caching (prevents dashboard hang on load)
2. Syncing ICS events into the calendar_events table
3. Calendar reminder sweep (fires due reminders as notifications)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape as _esc

import db

logger = logging.getLogger("calendar-sync")

ICS_REFRESH_INTERVAL = int(os.getenv("CALENDAR_ICS_REFRESH_INTERVAL", "900"))  # 15 min
ICS_MAX_BYTES = 6 * 1024 * 1024
REMINDER_SWEEP_INTERVAL = 60  # seconds


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def _fetch_ics_feed(url: str) -> tuple[str | None, str | None]:
    """Fetch an ICS feed. Returns (raw_text, error_string)."""
    import re
    # Reuse the same URL validation from ics_calendar
    try:
        from ics_calendar import is_allowed_calendar_url
        if not is_allowed_calendar_url(url):
            return None, "Invalid or disallowed calendar URL"
    except ImportError:
        pass
    req = urllib.request.Request(
        url,
        headers={"Accept": "text/calendar", "User-Agent": "Sietch-CRM-Calendar/3.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read(ICS_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, str(e.reason)
    except Exception as e:
        return None, str(e)
    if len(raw) > ICS_MAX_BYTES:
        return None, "Feed too large"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return text, None


def _store_ics_cache(url: str, raw_text: str, parsed: dict) -> None:
    """Store parsed ICS data in the cache table."""
    h = _url_hash(url)
    events_json = json.dumps(parsed.get("events", []), default=str)
    try:
        db.execute(
            """INSERT INTO ics_feed_cache (url_hash, raw_ics, parsed_events, fetched_at, expires_at, calendar_name, timezone)
               VALUES (%s, %s, %s::jsonb, NOW(), NOW() + INTERVAL '20 minutes', %s, %s)
               ON CONFLICT (url_hash) DO UPDATE SET
                 raw_ics = EXCLUDED.raw_ics,
                 parsed_events = EXCLUDED.parsed_events,
                 fetched_at = NOW(),
                 expires_at = NOW() + INTERVAL '20 minutes',
                 calendar_name = EXCLUDED.calendar_name,
                 timezone = EXCLUDED.timezone""",
            (h, raw_text, events_json, parsed.get("name", "Calendar"), parsed.get("timezone", "UTC")),
        )
    except Exception as e:
        logger.warning("Failed to store ICS cache for %s: %s", url[:80], e)


def _sync_ics_events_to_db(user_id: int, source_id: int, parsed: dict) -> int:
    """Upsert ICS events into calendar_events. Returns count of events synced."""
    from ics_calendar import _parse_ics_datetime
    events = parsed.get("events", [])
    count = 0
    for ev in events:
        uid = ev.get("uid", "")
        if not uid:
            continue
        summary = ev.get("summary", "")
        description = ev.get("description", "")
        location = ev.get("location", "")
        status = (ev.get("status") or "CONFIRMED").upper()
        color = ev.get("color", "")
        rrule = ev.get("rrule", "")

        # Parse start/end
        start_data = ev.get("start", {})
        end_data = ev.get("end", start_data)

        def _to_timestamp(dt_dict):
            if not dt_dict:
                return None
            if dt_dict.get("allDay"):
                return datetime(dt_dict["year"], dt_dict["month"], dt_dict["day"])
            iso = dt_dict.get("iso", "")
            if iso:
                try:
                    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    return dt.astimezone(timezone.utc).replace(tzinfo=None)
                except Exception:
                    pass
            return datetime(dt_dict.get("year", 2000), dt_dict.get("month", 1), dt_dict.get("day", 1),
                            dt_dict.get("hour", 0), dt_dict.get("minute", 0))

        dtstart = _to_timestamp(start_data)
        dtend = _to_timestamp(end_data)
        all_day = start_data.get("allDay", False)

        if not dtstart:
            continue

        # Ensure positive duration
        if dtend and dtend <= dtstart:
            dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))
        if not dtend:
            dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))

        try:
            db.execute(
                """INSERT INTO calendar_events (user_id, source_id, uid, summary, description, location,
                   dtstart, dtend, all_day, rrule, color, status, last_modified)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (user_id, uid) DO UPDATE SET
                     summary = EXCLUDED.summary,
                     description = EXCLUDED.description,
                     location = EXCLUDED.location,
                     dtstart = EXCLUDED.dtstart,
                     dtend = EXCLUDED.dtend,
                     all_day = EXCLUDED.all_day,
                     rrule = EXCLUDED.rrule,
                     color = EXCLUDED.color,
                     status = EXCLUDED.status,
                     last_modified = NOW()""",
                (user_id, source_id, uid, summary, description, location,
                 dtstart, dtend, all_day, rrule or None, color or None, status),
            )
            count += 1
        except Exception as e:
            logger.debug("Failed to upsert event %s: %s", uid[:40], e)
    return count


def _refresh_ics_feeds() -> int:
    """Refresh all ICS feed sources. Returns number of sources refreshed."""
    try:
        sources = db.query_dicts(
            "SELECT id, user_id, ics_url, name FROM calendar_sources WHERE source_type = 'ics' AND ics_url IS NOT NULL"
        )
    except Exception:
        return 0

    refreshed = 0
    for src in sources:
        url = src.get("ics_url", "")
        if not url:
            continue
        # Check if cache is still fresh
        h = _url_hash(url)
        try:
            cached = db.query_one(
                "SELECT id FROM ics_feed_cache WHERE url_hash = %s AND expires_at > NOW()", (h,)
            )
            if cached:
                continue  # Cache still fresh
        except Exception:
            pass

        raw_text, error = _fetch_ics_feed(url)
        if error or not raw_text:
            logger.warning("ICS feed failed for source %s: %s", src.get("id"), error)
            continue

        try:
            from ics_calendar import parse_ics_calendar
            parsed = parse_ics_calendar(raw_text)
        except Exception as e:
            logger.warning("ICS parse failed for source %s: %s", src.get("id"), e)
            continue

        _store_ics_cache(url, raw_text, parsed)
        _sync_ics_events_to_db(src["user_id"], src["id"], parsed)

        # Update source sync time
        try:
            db.execute("UPDATE calendar_sources SET last_synced_at = NOW() WHERE id = %s", (src["id"],))
        except Exception:
            pass

        refreshed += 1
        logger.info("Refreshed ICS source %s (%s): %d events", src.get("id"), src.get("name"), len(parsed.get("events", [])))

    return refreshed


def _sweep_calendar_reminders() -> int:
    """Fire due calendar reminders as notifications. Returns count fired."""
    try:
        due_events = db.query_dicts(
            """SELECT id, user_id, uid, summary, dtstart, reminder_minutes
               FROM calendar_events
               WHERE reminder_sent = FALSE
                 AND reminder_minutes IS NOT NULL
                 AND reminder_minutes > 0
                 AND dtstart - (reminder_minutes || ' minutes')::interval <= NOW()
                 AND dtstart > NOW()
                 AND status != 'CANCELLED'"""
        )
    except Exception:
        return 0

    fired = 0
    for ev in due_events:
        user_id = ev["user_id"]
        summary = ev.get("summary") or "Calendar event"
        dtstart = ev.get("dtstart")
        mins = ev.get("reminder_minutes", 15)

        # Create notification
        message = f"Reminder: {summary} starts in {mins} minutes"
        if dtstart:
            message += f" ({dtstart.strftime('%I:%M %p')})"

        try:
            db.execute(
                """INSERT INTO notifications (user_id, type, message, payload, is_read)
                   VALUES (%s, 'calendar_reminder', %s, %s, FALSE)""",
                (user_id, message, json.dumps({"event_uid": ev.get("uid"), "summary": summary, "dtstart": str(dtstart) if dtstart else ""})),
            )
            db.execute("UPDATE calendar_events SET reminder_sent = TRUE WHERE id = %s", (ev["id"],))
            fired += 1
        except Exception as e:
            logger.warning("Failed to create calendar reminder for event %s: %s", ev.get("uid"), e)

    return fired


# ── Background thread ─────────────────────────────────────────────────────────

def _sync_loop(stop_event: threading.Event) -> None:
    """Main sync loop — refreshes ICS feeds and sweeps reminders."""
    logger.info("Calendar sync started (ICS interval=%ds, reminder interval=%ds)", ICS_REFRESH_INTERVAL, REMINDER_SWEEP_INTERVAL)
    last_ics_refresh = 0
    while not stop_event.is_set():
        now = time.time()
        try:
            # ICS refresh (every 15 min)
            if now - last_ics_refresh >= ICS_REFRESH_INTERVAL:
                refreshed = _refresh_ics_feeds()
                if refreshed:
                    logger.info("Refreshed %d ICS feed(s)", refreshed)
                last_ics_refresh = now

            # Reminder sweep (every 60s)
            fired = _sweep_calendar_reminders()
            if fired:
                logger.info("Fired %d calendar reminder(s)", fired)
        except Exception:
            logger.exception("Calendar sync loop error")
        stop_event.wait(min(ICS_REFRESH_INTERVAL, REMINDER_SWEEP_INTERVAL))


def start_calendar_sync() -> threading.Event:
    """Start the calendar sync background thread. Returns stop event."""
    stop_event = threading.Event()
    t = threading.Thread(target=_sync_loop, args=(stop_event,), daemon=True, name="calendar-sync")
    t.start()
    return stop_event


def stop_calendar_sync(stop_event: threading.Event) -> None:
    """Signal the sync thread to stop."""
    stop_event.set()
