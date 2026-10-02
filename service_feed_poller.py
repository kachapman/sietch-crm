"""Background poller for service_feeds (RSS/Atom).

Polls each enabled RSS feed on its individual interval, parses entries with
stdlib XML (no feedparser dependency), dedups against the last seen entry id,
and forwards new entries to `server._insert_feed_notification`.

The poller runs as a daemon thread, started by the server at boot. It does
NOT short-circuit on errors — a single failing feed (network blip, malformed
XML, etc.) is logged but doesn't kill the thread or stop other feeds from
polling.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from typing import Any

import db

logger = logging.getLogger("service-feed-poller")

_POLLER_STARTED = False
_POLLER_LOCK = threading.Lock()


def generate_webhook_secret() -> str:
    """Generate a random URL-safe secret token (used in webhook URL path)."""
    return secrets.token_urlsafe(32)


def start_poller(server_module) -> None:
    """Start the background poller thread. Idempotent."""
    global _POLLER_STARTED
    with _POLLER_LOCK:
        if _POLLER_STARTED:
            return
        _POLLER_STARTED = True
    t = threading.Thread(target=_poller_loop, args=(server_module,), daemon=True, name="service-feed-poller")
    t.start()
    logger.info("Service feed poller started")


def _poller_loop(server_module) -> None:
    """Main loop: walk every enabled feed, poll those whose interval has elapsed."""
    # Initial short sleep so the server finishes booting before first poll.
    time.sleep(5)
    while True:
        try:
            _poll_due_feeds(server_module)
        except Exception as exc:
            logger.exception("Service feed poller loop error: %s", exc)
        # Tick every 30s; per-feed check decides whether to actually poll.
        time.sleep(30)


def _poll_due_feeds(server_module) -> None:
    """Poll each enabled feed whose poll_interval_sec has elapsed since last_polled_at."""
    rows = db.query_dicts(
        """SELECT id, name, kind, url, poll_interval_sec, last_polled_at, last_item_id, enabled, accent_color
           FROM service_feeds
           WHERE enabled = TRUE AND kind = 'rss' AND url IS NOT NULL"""
    )
    if not rows:
        return
    now = time.time()
    for feed in rows:
        try:
            last = feed.get("last_polled_at")
            if last is not None:
                try:
                    last_ts = last.timestamp() if hasattr(last, "timestamp") else 0
                except Exception:
                    last_ts = 0
                interval = max(60, int(feed.get("poll_interval_sec") or 300))
                if (now - last_ts) < interval:
                    continue
            _poll_one_feed(server_module, feed)
        except Exception as exc:
            logger.exception("Error polling service feed %s: %s", feed.get("id"), exc)


def _poll_one_feed(server_module, feed: dict) -> None:
    """Fetch + parse one RSS/Atom feed, dispatch new entries as notifications."""
    url = feed.get("url")
    if not url:
        return
    feed_id = feed["id"]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SietchCRM/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        logger.warning("Feed %s (%s) fetch failed: %s", feed_id, url, exc)
        # Still record last_polled_at so we don't hammer failing feeds.
        try:
            db.execute(
                "UPDATE service_feeds SET last_polled_at = NOW(), updated_at = NOW() WHERE id = %s",
                (feed_id,),
            )
        except Exception:
            pass
        return
    entries = _parse_feed(raw)
    if not entries:
        # Empty or unparsable — record poll time, no notifications.
        try:
            db.execute(
                "UPDATE service_feeds SET last_polled_at = NOW(), updated_at = NOW() WHERE id = %s",
                (feed_id,),
            )
        except Exception:
            pass
        return
    last_id = feed.get("last_item_id") or ""
    new_entries: list[dict] = []
    for entry in entries:
        eid = entry.get("id") or entry.get("link") or ""
        if not eid:
            continue
        if eid == last_id:
            continue
        new_entries.append(entry)
    # Only mark new as "the" first if it differs from last_id. If we already
    # saw this id but it was the last seen, skip.
    if new_entries:
        # If last_id is empty, treat all entries as new EXCEPT keep the most
        # recent one (so we don't deliver every historical entry on first poll).
        if not last_id:
            # Drop oldest ones: only notify on entries newer than what's already
            # in the feed. Skip all except the most recent.
            new_entries = new_entries[:1]
        # Insert notifications for each new entry.
        for entry in new_entries:
            try:
                # server_module may be the script-level module; the actual
                # helper is a class method on KanbanHandler. Try both.
                insert_fn = getattr(server_module, "_insert_feed_notification", None) or \
                            getattr(server_module.KanbanHandler, "_insert_feed_notification", None)
                insert_fn(feed, entry)
            except Exception as exc:
                logger.exception("Failed to insert feed notification: %s", exc)
        # Update last_item_id to the most recent entry's id.
        latest_id = new_entries[0].get("id") or new_entries[0].get("link") or ""
        try:
            db.execute(
                "UPDATE service_feeds SET last_polled_at = NOW(), last_item_id = %s, updated_at = NOW() WHERE id = %s",
                (latest_id, feed_id),
            )
        except Exception:
            pass
    else:
        # Nothing new — record poll time only.
        try:
            db.execute(
                "UPDATE service_feeds SET last_polled_at = NOW(), updated_at = NOW() WHERE id = %s",
                (feed_id,),
            )
        except Exception:
            pass


def _parse_feed(raw: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom 1.0 XML into a list of normalized entry dicts.

    Returns a list of {id, title, summary, link, published} sorted newest-first.
    On parse failure returns []. Only stdlib — no feedparser dependency.
    """
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    entries: list[dict] = []
    # Atom detection: root tag is {http://www.w3.org/2005/Atom}feed
    if root.tag.endswith("}feed") or root.tag == "feed":
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for item in root.findall("a:entry", ns):
            eid = item.findtext("a:id", default="", namespaces=ns)
            title = item.findtext("a:title", default="", namespaces=ns)
            summary = item.findtext("a:summary", default="", namespaces=ns)
            content = item.findtext("a:content", default="", namespaces=ns)
            link_el = item.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            published = item.findtext("a:updated", default="", namespaces=ns) or \
                        item.findtext("a:published", default="", namespaces=ns)
            entries.append({
                "id": (eid or link).strip(),
                "title": (title or "(no title)").strip(),
                "summary": (summary or content or "").strip(),
                "link": (link or "").strip(),
                "published": (published or "").strip(),
            })
    elif root.tag.endswith("}rss") or root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            return []
        for item in channel.findall("item"):
            eid = (item.findtext("guid") or "").strip()
            title = (item.findtext("title") or "(no title)").strip()
            description = (item.findtext("description") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            entries.append({
                "id": (eid or link).strip(),
                "title": title,
                "summary": description,
                "link": link,
                "published": pub_date,
            })
    else:
        return []
    # Sort newest-first when we have a date; fall back to document order.
    def _ts(entry: dict) -> float:
        from email.utils import parsedate_to_datetime
        p = entry.get("published") or ""
        try:
            return parsedate_to_datetime(p).timestamp()
        except Exception:
            try:
                from datetime import datetime
                return datetime.fromisoformat(p.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0
    entries.sort(key=_ts, reverse=True)
    return entries


def hash_feed_fingerprint(feed_id: int) -> str:
    """Stable fingerprint for a feed, useful for cache keys."""
    return hashlib.sha1(f"service-feed-{feed_id}".encode()).hexdigest()[:12]