"""Canonical timestamp handling for mail (and anything else that stores times).

WHY THIS MODULE EXISTS (live bug 2026-11-09)
--------------------------------------------
Ken: "an email Outlook shows received Mon 11/9/2026 1:20 PM (EST, 18:20Z) is
shown in Sietch as 11/9/2026, 2:20:03 AM". That is not a plain UTC display:
a wall-clock offset error is at most ±14h and never preserves the minutes, so
the value itself was already wrong (or was parsed without its offset) before
it reached the screen.

`mail_messages.date_received` was a plain `TIMESTAMP` (no time zone). The
scanner wrote `msg.date.isoformat()` — an ISO string that still carried the
sender's offset — into that column, so the instant Postgres stored depended
on the *session* `TimeZone`. With `TimeZone=UTC` that is correct UTC wall
clock; with anything else (a new container without `TZ=UTC`, a `SET TIME
ZONE`, a restored dump) every row silently becomes local wall clock while the
reader keeps assuming UTC. That is why the bug "keeps recurring".

Rules enforced here (and by migration 048, which retypes the column to
`timestamptz`):

  1. Every parsed value becomes an AWARE datetime in UTC. A value with no
     offset is treated as UTC (documented assumption, matching the droplet's
     UTC database session and the historical reader behaviour).
  2. Never drop tzinfo before storage. Never hand a naive datetime to the DB.
  3. Serialize with an explicit offset (`+00:00`) or `Z`.

Stdlib only, no DB import: used by the scanner, the server, the backfill and
the tests.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

# Values with no offset are assumed UTC. This is the assumption the reader
# (`server._json_default`) has always made and it matches the production DB
# session (UTC). It is applied ONLY when the source genuinely has no offset.
ASSUME_NAIVE_IS_UTC = True

_TZ_SUFFIX = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$", re.IGNORECASE)


def to_utc(value: Any) -> datetime | None:
    """Normalize an aware datetime to UTC. Naive input is assumed UTC."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc) if ASSUME_NAIVE_IS_UTC else value.astimezone()
    return value.astimezone(timezone.utc)


def parse_email_date(value: Any) -> datetime | None:
    """Parse an email date into an AWARE UTC datetime.

    Accepts:
      * a datetime (aware → converted; naive → assumed UTC)
      * an ISO-8601 string, with or without an offset
      * an RFC 2822 / RFC 5322 `Date:` header value
        ("Mon, 9 Nov 2026 13:20:03 -0500", "... GMT", "... (EST)")
      * an epoch number (seconds or milliseconds)

    Returns None when the value cannot be parsed. Never raises.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # ISO-8601 first (the scanner's own format, and Graph's).
    iso_text = text
    if iso_text.endswith("Z"):
        iso_text = iso_text[:-1] + "+00:00"
    try:
        return to_utc(datetime.fromisoformat(iso_text))
    except ValueError:
        pass

    # RFC 2822 `Date:` header.
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        return to_utc(parsed)

    return None


def parse_graph_received_datetime(value: Any) -> datetime | None:
    """Parse a Microsoft Graph `receivedDateTime` / `sentDateTime`.

    Graph returns ISO-8601 in UTC with a `Z` and up to 7 fractional digits
    ("2026-11-09T18:20:03.0000000Z"). `datetime.fromisoformat` accepts at most
    6 digits, so the fraction is normalized first. A value with an explicit
    offset is honoured; a value with none is treated as UTC (Graph documents
    these properties as UTC, so this is not a guess).
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" ", "T", 1) if re.match(r"^\d{4}-\d{2}-\d{2} \d", text) else text
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", text)
    if m:
        base, fraction, offset = m.groups()
        frac = ""
        if fraction:
            digits = (fraction[1:] + "000000")[:6]
            frac = "." + digits
        off = "+00:00" if not offset or offset.upper() == "Z" else offset
        if len(off) == 5:  # +0530 → +05:30
            off = off[:3] + ":" + off[3:]
        try:
            return datetime.fromisoformat(f"{base}{frac}{off}").astimezone(timezone.utc)
        except ValueError:
            return None
    return parse_email_date(text)


def iso_utc(value: Any) -> str:
    """Serialize to ISO-8601 with an explicit offset (empty string on failure).

    Naive input is treated as UTC. This is the single string form the API and
    the backfill emit, so a browser can never parse it as local time.
    """
    dt = value if isinstance(value, datetime) else parse_email_date(value)
    if dt is None:
        return ""
    dt = to_utc(dt)
    return dt.isoformat() if dt else ""


def iso_utc_z(value: Any) -> str:
    """Same as iso_utc() but with a trailing `Z` instead of `+00:00`."""
    text = iso_utc(value)
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def has_explicit_offset(value: Any) -> bool:
    """True when a string date carries a timezone offset (or is a datetime
    with tzinfo). Used by the diagnostic/backfill to explain a delta."""
    if isinstance(value, datetime):
        return value.tzinfo is not None
    if not isinstance(value, str):
        return False
    return bool(_TZ_SUFFIX.search(value.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# Undated messages + the date fallback chain (bug fix 2026-11-11)
# ─────────────────────────────────────────────────────────────────────────────
#
# SMTP sends from Sietch used to carry no `Date` header. imap_tools reports a
# missing/unparseable Date as the 1900-01-01 sentinel (`parse_email_date` in
# imap_tools returns datetime(1900, 1, 1)), and the scanner's 90-day retention
# guard then treated those messages as "older than the window": it skipped the
# import, marked the UID processed, and advanced the folder watermark past it.
# The messages were therefore never imported, even though they were brand new.
#
# A 1900 value is NOT a message date. The rules below are the single source of
# truth for that judgement and for the replacement chain the scanner uses:
#
#   1. a real Date header / imap_tools date   → "header"
#   2. IMAP INTERNALDATE (when the header is unusable) → "internaldate"
#   3. the date inside a `Received:` header   → "received"
#   4. import time (now, UTC)                 → "import"
#
# `date_source` is stored on the row (mail_messages.date_source) so the chosen
# fallback is auditable instead of being a silent guess.

UNDATED_SENTINEL_YEAR = 1900

DATE_SOURCE_HEADER = "header"
DATE_SOURCE_INTERNALDATE = "internaldate"
DATE_SOURCE_RECEIVED = "received"
DATE_SOURCE_IMPORT = "import"

DATE_SOURCES = (
    DATE_SOURCE_HEADER,
    DATE_SOURCE_INTERNALDATE,
    DATE_SOURCE_RECEIVED,
    DATE_SOURCE_IMPORT,
)

# RFC 3501 INTERNALDATE: `17-Jul-2026 09:20:00 -0400` (the day may be
# space-padded, and the value normally sits inside a quoted FETCH response).
_INTERNALDATE_RE = re.compile(
    r"(\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}:\d{2}\s+[+-]\d{4})")


def is_undated_date(value: Any) -> bool:
    """True when `value` is not a usable message date.

    That is: None/blank, an unparseable value, or the 1900-01-01 sentinel that
    IMAP libraries use for "this message has no Date header" (naive or aware,
    string or datetime — all are recognised). Callers must not compare such a
    value against a retention cutoff.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (bytes, bytearray)):
        dt = parse_email_date(bytes(value).decode("utf-8", "replace").strip())
    else:
        dt = parse_email_date(value)
    if dt is None:
        return True
    return dt.year <= UNDATED_SENTINEL_YEAR


def parse_internaldate(value: Any) -> datetime | None:
    """Parse an IMAP INTERNALDATE into an AWARE UTC datetime, or None.

    Accepts the bare RFC 3501 value ("17-Jul-2026 09:20:00 -0400") or a whole
    FETCH response line containing it. Never raises.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
    match = _INTERNALDATE_RE.search(text)
    if not match:
        return None
    try:
        parsed = parsedate_to_datetime(match.group(1))
    except (TypeError, ValueError):
        return None
    return to_utc(parsed) if parsed is not None else None


def parse_received_header_date(value: Any) -> datetime | None:
    """The date at the end of a `Received:` header, as AWARE UTC, or None.

    A Received header looks like
        from mail.example by mx.example with ESMTPS id x;
        Mon, 9 Nov 2026 13:20:03 -0500
    The date is everything after the final semicolon (RFC 5321 allows other
    semicolons earlier in the clause, so the LAST one wins). Accepts a single
    header, a list/tuple of headers, or a whole raw header block. Never raises.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            dt = parse_received_header_date(item)
            if dt is not None:
                return dt
        return None
    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
    if ";" not in text:
        return None
    tail = text.rsplit(";", 1)[1].strip()
    return parse_email_date(tail)


def resolve_message_date(
    header_date: Any = None,
    internal_date: Any = None,
    received_headers: Any = None,
    now: datetime | None = None,
) -> tuple[datetime, str]:
    """Resolve the timestamp to store for a message, with its provenance.

    Returns `(aware UTC datetime, source)` where source is one of
    "header", "internaldate", "received", "import". Never returns None: when
    nothing usable exists the import time is used, because dropping a brand
    new message as "undated" is exactly the bug this chain fixes.

    A 1900 sentinel is treated as "no date" at every step, never as an old
    message. A REAL old date is returned unchanged, so the retention guard can
    still skip it.
    """
    if not is_undated_date(header_date):
        parsed = parse_email_date(header_date)
        if parsed is not None:
            return parsed, DATE_SOURCE_HEADER
    if not is_undated_date(internal_date):
        parsed = parse_internaldate(internal_date)
        if parsed is not None:
            return parsed, DATE_SOURCE_INTERNALDATE
    if received_headers:
        parsed = parse_received_header_date(received_headers)
        if parsed is not None:
            return parsed, DATE_SOURCE_RECEIVED
    fallback = to_utc(now) if isinstance(now, datetime) else None
    return (fallback or datetime.now(timezone.utc)), DATE_SOURCE_IMPORT


__all__ = [
    "ASSUME_NAIVE_IS_UTC",
    "DATE_SOURCES",
    "DATE_SOURCE_HEADER",
    "DATE_SOURCE_IMPORT",
    "DATE_SOURCE_INTERNALDATE",
    "DATE_SOURCE_RECEIVED",
    "UNDATED_SENTINEL_YEAR",
    "has_explicit_offset",
    "is_undated_date",
    "iso_utc",
    "iso_utc_z",
    "parse_email_date",
    "parse_graph_received_datetime",
    "parse_internaldate",
    "parse_received_header_date",
    "resolve_message_date",
    "to_utc",
]
