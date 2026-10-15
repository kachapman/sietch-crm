"""Background checker for "clocked in for 10+ hours" notifications.

Polls time_entries every 60 seconds for shifts where the user has been
continuously clocked in past a threshold (10h, then 14h) without clocking
out. Inserts a `notifications` row of type 'timesheet_overdue' for each
newly-crossed threshold; the existing notification_dispatcher picks those
rows up on its next poll and sends them through the user's Telegram chat
via the established pipeline. The same row also appears in the CRM
notification drawer (the /api/v2/notifications endpoint reads from the
same table).

De-duplication: a unique partial index on
  (user_id, type, payload->>'entry_id', payload->>'threshold_hours')
WHERE type='timesheet_overdue' (migration 032) makes the INSERT
idempotent. ON CONFLICT DO NOTHING turns the second/third/Nth insert
into a no-op so server restarts and double-instances are safe.

The user chose (round 4, after the original audit): 10h once + 14h
reminder, no per-type opt-out, 60s poll, INSERT-IGNORE-style unique
guard. The thread is started by server.py at boot alongside the other
background subsystems (dispatcher, scanner, service feed poller, etc.).

Run independently for tests: python -m timesheet_overdue_checker (just
runs one scan and exits). In production, server.py calls start_checker()
which spawns the daemon thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import db

logger = logging.getLogger("timesheet-overdue-checker")

# Thresholds (in hours). 10h is the initial "forgot to log out" reminder;
# 14h is a second escalation. The user explicitly chose both in the round 4
# design discussion.
THRESHOLDS_HOURS = (10, 14)

# Polling interval (seconds). Matches the user-pinned "every 60 seconds"
# decision. Cheap query: a single SELECT with two inequality predicates.
POLL_INTERVAL_SEC = 60

# Initial sleep so the server finishes booting before the first scan
# (also gives the dispatcher time to start so the first notifications
# can be sent immediately after insert).
INITIAL_SLEEP_SEC = 10

_CHECKER_STARTED = False
_CHECKER_LOCK = threading.Lock()
_STOP_EVENT: threading.Event | None = None


def start_checker() -> threading.Event:
    """Start the background checker thread. Idempotent. Returns a stop event
    the caller can .set() to request graceful shutdown (the thread checks
    the event every poll cycle)."""
    global _CHECKER_STARTED, _STOP_EVENT
    with _CHECKER_LOCK:
        if _CHECKER_STARTED:
            return _STOP_EVENT  # type: ignore[return-value]
        _CHECKER_STARTED = True
        stop = threading.Event()
        _STOP_EVENT = stop
    t = threading.Thread(target=_loop, args=(stop,), daemon=True, name="timesheet-overdue-checker")
    t.start()
    logger.info("Timesheet overdue checker started (interval=%ds, thresholds=%s)", POLL_INTERVAL_SEC, THRESHOLDS_HOURS)
    return stop


def stop_checker(stop_event: threading.Event) -> None:
    """Signal the background thread to exit. The thread checks the event
    between polls and exits within ~POLL_INTERVAL_SEC."""
    if stop_event:
        stop_event.set()


def scan_once() -> dict:
    """Run one scan pass and return a summary dict.
    Public so tests can call it directly without spinning up the thread."""
    return _scan_once()


def _loop(stop_event: threading.Event) -> None:
    """Main loop: scan every POLL_INTERVAL_SEC until the stop event is set."""
    time.sleep(INITIAL_SLEEP_SEC)
    while not stop_event.is_set():
        try:
            _scan_once()
        except Exception as exc:
            # A single failed scan must not kill the thread.
            logger.exception("Timesheet overdue checker scan error: %s", exc)
        # Sleep in 1s slices so stop is responsive.
        for _ in range(POLL_INTERVAL_SEC):
            if stop_event.is_set():
                return
            time.sleep(1)


def _scan_once() -> dict:
    """Find every open shift past each threshold and insert a notification
    for any (user, entry, threshold) tuple that doesn't already have one.

    Returns a dict with counts for logging/tests.
    """
    summary = {"scanned": 0, "inserted_10h": 0, "inserted_14h": 0, "skipped": 0}
    # Pull every open shift past the LOWEST threshold (10h) so we can
    # check both thresholds per row. The per-threshold loop below skips
    # any threshold that hasn't been crossed yet.
    try:
        rows = db.query_dicts(
            """SELECT te.id, te.user_id, te.clock_in, te.opportunity_id,
                      u.display_name AS user_name,
                      o.title AS opportunity_title
               FROM time_entries te
               JOIN users u ON u.id = te.user_id
               LEFT JOIN opportunities o ON o.id = te.opportunity_id
               WHERE te.clock_out IS NULL
                 AND te.clock_in < (NOW() - (%s || ' hours')::interval)
               ORDER BY te.clock_in ASC""",
            (str(min(THRESHOLDS_HOURS)),),
        )
    except Exception as exc:
        logger.exception("Timesheet overdue checker query failed: %s", exc)
        return summary
    summary["scanned"] = len(rows)
    if not rows:
        return summary

    # For each row, compute how many thresholds it has crossed and insert
    # a notification for any that don't exist yet. ON CONFLICT DO NOTHING
    # makes the de-dup atomic.
    for r in rows:
        try:
            clock_in = r.get("clock_in")
            if clock_in is None:
                summary["skipped"] += 1
                continue
            # Handle both datetime and ISO-string returns from db.query_dicts.
            # The DB stores clock_in as TIMESTAMP (or TIMESTAMPTZ in some
            # code paths). If it comes back as a naive datetime, we treat
            # it as UTC — the server is configured to write clock_in via
            # NOW() in the same session, and PostgreSQL stores it without
            # TZ info in the legacy schema. Without this, Python's
            # .timestamp() on a naive datetime interprets it as LOCAL time,
            # which is wrong on non-UTC servers (the elapsed calc would
            # be off by the local TZ offset).
            from datetime import datetime, timezone
            if hasattr(clock_in, "timestamp"):
                if getattr(clock_in, "tzinfo", None) is None:
                    clock_in_ts = clock_in.replace(tzinfo=timezone.utc).timestamp()
                else:
                    clock_in_ts = clock_in.timestamp()
            else:
                clock_in_ts = datetime.fromisoformat(str(clock_in).replace("Z", "+00:00")).timestamp()
            elapsed_hours = (time.time() - clock_in_ts) / 3600.0
        except Exception as exc:
            logger.debug("Could not compute elapsed for entry %s: %s", r.get("id"), exc)
            summary["skipped"] += 1
            continue

        # Whole hours elapsed (rounded down). e.g. 10h 23m -> 10.
        elapsed_h = int(elapsed_hours)

        # The headline text is "Clocked in for {h}h {m}m" (so the user
        # sees the actual duration, not just the threshold number).
        elapsed_m = int((elapsed_hours - elapsed_h) * 60)
        if elapsed_h > 0:
            elapsed_str = f"{elapsed_h}h {elapsed_m}m"
        else:
            elapsed_str = f"{elapsed_m}m"

        for threshold in THRESHOLDS_HOURS:
            if elapsed_h < threshold:
                continue
            # Headline = time (matches drawer headline convention).
            # Snippet = the question (matches drawer second line + Telegram
            # message body for consistency across both channels).
            opp_title = r.get("opportunity_title")
            base_headline = f"Clocked in for {elapsed_str}"
            if opp_title:
                headline = f"{base_headline} on {opp_title}"
            else:
                headline = base_headline
            if threshold == 10:
                snippet = "Did you forget to log out?"
            else:
                snippet = "Please verify your timesheet."

            try:
                db.execute(
                    """INSERT INTO notifications
                       (user_id, type, opportunity_id, message, payload)
                       VALUES (%s, 'timesheet_overdue', %s, %s,
                               jsonb_build_object(
                                   'entry_id', %s,
                                   'threshold_hours', %s,
                                   'elapsed', %s,
                                   'snippet', %s
                               ))
                       ON CONFLICT DO NOTHING""",
                    (
                        r["user_id"],
                        r.get("opportunity_id"),
                        headline,
                        str(r["id"]),
                        threshold,
                        elapsed_str,
                        snippet,
                    ),
                )
                # No way to tell from execute() whether the row was inserted
                # or the conflict was a no-op, so we just log at debug.
                if threshold == 10:
                    summary["inserted_10h"] += 1
                else:
                    summary["inserted_14h"] += 1
            except Exception as exc:
                logger.exception("Failed to insert timesheet_overdue for entry %s threshold %s: %s",
                                 r.get("id"), threshold, exc)
                summary["skipped"] += 1

    if summary["inserted_10h"] or summary["inserted_14h"]:
        logger.info("Timesheet overdue scan: %s", summary)
    return summary
