"""Durable classification of auto-logged project-history rows.

Why this module exists (2026-11-10): the client used to decide "is this a system
event?" from the event's category (`is_system` / a title allowlist). A field
change is filed under the synthetic "Action" category, which was missing on
production (migration 017 was recorded as applied even though its seed rolled
back — see `db.py` `_run_pending_migrations`). The writer therefore inserted
`category_id = NULL`, the client saw no category, and Ken's "Follow-up Due Date
changed from … to …" row rendered as a full card with a Reply button.

The fix is a server-side marker column on `history_events`
(`is_system_event`), set by every auto-log writer. This module is the shared,
DB-free predicate for the *import* writers: rows restored from another CRM
carry no marker, so they are classified from the source category's system flag
(`Action`/`Files are uploaded`/`Task is closed`/…) or from the exact
tracked-field content shape. It deliberately does NOT look for the words
"changed from" anywhere in a note — human notes mention those phrases in prose
("the date was set to 10/5 in sietch") and must stay full cards.

Emails are never auto history events: "Mail Message" keeps its full card with
the body, attachments and reply thread even though its category is a system
category.
"""

from __future__ import annotations

import re

# Labels used by `server.py::_handle_project_update` (`TRACKED_FIELDS`).
# A field change is exactly one of:
#   "{label} changed from {old} to {new}"
#   "{label} set to {new}"
#   "{label} cleared"
FIELD_CHANGE_LABELS: tuple[str, ...] = (
    "Stage",
    "Value",
    "Contact",
    "Responsible",
    "Follow-up Due Date",
    "Private",
)

FIELD_CHANGE_PATTERN = (
    r"^(?:"
    + "|".join(re.escape(label) for label in FIELD_CHANGE_LABELS)
    + r") (?:changed from [^<>]+ to [^<>]+|set to [^<>]+|cleared)$"
)

_FIELD_CHANGE_RE = re.compile(FIELD_CHANGE_PATTERN)

# The CRM's own auto-log category (field changes). Matched title-insensitively.
AUTO_LOG_CATEGORY_TITLES = frozenset({"action"})

# Titles emitted only by the document generators (invoice / COC / photo report).
AUTO_LOG_EVENT_TITLES = frozenset({
    "Invoice generated",
    "Photo Report generated",
    "Certificate of Completion generated",
})

# Emails keep their full card, marker or not.
MAIL_CATEGORY_TITLES = frozenset({"mail message"})


def looks_like_field_change(content) -> bool:
    """True for the exact tracked-field logger shapes (full string match).

    The logger writes one plain-text line. Human notes are HTML/markdown and
    often span lines, so tags and newlines disqualify a row — a note that
    quotes "Stage changed from A to B" inside a paragraph never matches.
    """
    text = str(content or "").strip()
    if not text or "<" in text or ">" in text or "\n" in text or "\r" in text:
        return False
    return bool(_FIELD_CHANGE_RE.fullmatch(text))


def is_auto_history_event(
    *,
    category_title=None,
    category_is_system=None,
    content=None,
    title=None,
) -> bool:
    """Classify a history row restored/imported from another CRM.

    Durable signals only: an auto-log category, a system category (never the
    mail category), a generator-only title, or the exact field-change shape.
    """
    cat_title = str(category_title or "").strip()
    cat_lower = cat_title.lower()
    if cat_lower in MAIL_CATEGORY_TITLES:
        return False
    if title is not None and str(title).strip() in AUTO_LOG_EVENT_TITLES:
        return True
    if cat_lower in AUTO_LOG_CATEGORY_TITLES:
        return True
    if category_is_system is True:
        return True
    return looks_like_field_change(content)


__all__ = [
    "AUTO_LOG_CATEGORY_TITLES",
    "AUTO_LOG_EVENT_TITLES",
    "FIELD_CHANGE_LABELS",
    "FIELD_CHANGE_PATTERN",
    "MAIL_CATEGORY_TITLES",
    "is_auto_history_event",
    "looks_like_field_change",
]
