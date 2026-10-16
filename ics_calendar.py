"""Parse iCalendar (ICS) feeds for dashboard calendar tiles."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

# Proton Calendar–style event colors (used when ICS has no COLOR property).
PROTON_EVENT_COLORS = (
    "#6e4bdb",
    "#439fe5",
    "#3eb489",
    "#e8b930",
    "#e07a2f",
    "#e05252",
    "#b35db8",
    "#5b8def",
    "#2dbfa0",
    "#c97cd8",
)

_MAX_ICS_BYTES = 6 * 1024 * 1024


def is_allowed_calendar_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in ("https", "http"):
        return False
    if not parsed.netloc:
        return False
    host = parsed.netloc.lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
        return False
    return True


def unfold_ics_lines(text: str) -> list[str]:
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _parse_prop(line: str) -> tuple[str, dict[str, str], str]:
    if ":" not in line:
        return line, {}, ""
    head, value = line.split(":", 1)
    if ";" in head:
        name, *params = head.split(";")
        meta: dict[str, str] = {}
        for p in params:
            if "=" in p:
                k, v = p.split("=", 1)
                meta[k.upper()] = v
        return name.upper(), meta, value
    return head.upper(), {}, value


def _unescape_ics_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_organizer(raw: str, meta: dict[str, str] | None = None) -> str:
    # CN/PARTSTAT usually arrive as property params (ORGANIZER;CN=Alice:...),
    # which _parse_prop splits off into meta — prefer them over the bare value.
    if meta and meta.get("CN"):
        return meta["CN"].strip().strip('"')
    raw = raw.strip()
    if not raw:
        return ""
    cn_match = re.search(r"CN=([^;:]+)", raw, re.I)
    if cn_match:
        return cn_match.group(1).strip().strip('"')
    mail = re.search(r"mailto:([^;\s]+)", raw, re.I)
    if mail:
        return mail.group(1).strip()
    if raw.lower().startswith("mailto:"):
        return raw[7:].strip()
    return raw


def _parse_attendee(raw: str, meta: dict[str, str] | None = None) -> dict[str, str]:
    raw = raw.strip()
    meta = meta or {}
    cn = (meta.get("CN") or "").strip().strip('"')
    if not cn:
        cn_match = re.search(r"CN=([^;:]+)", raw, re.I)
        cn = cn_match.group(1).strip().strip('"') if cn_match else ""
    mail = re.search(r"mailto:([^;\s]+)", raw, re.I)
    email = mail.group(1).strip() if mail else ""
    partstat = (meta.get("PARTSTAT") or "").strip()
    if not partstat:
        partstat_m = re.search(r"PARTSTAT=([^;:]+)", raw, re.I)
        partstat = partstat_m.group(1).strip() if partstat_m else ""
    label = cn or email or raw
    if partstat and partstat.upper() != "ACCEPTED":
        label = f"{label} ({partstat})"
    return {"name": cn, "email": email, "partstat": partstat, "label": label}


def _format_ics_instant(value: str) -> str:
    val = value.strip()
    if not val:
        return ""
    try:
        if val.endswith("Z") and "T" in val:
            dt = datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        if "T" in val and len(val) >= 15:
            dt = datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M")
        if len(val) == 8 and val.isdigit():
            dt = datetime.strptime(val, "%Y%m%d")
            return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    return val


def _humanize_rrule(rrule: str) -> str:
    r = rrule.strip()
    if not r:
        return ""
    parts = dict(
        p.split("=", 1) if "=" in p else (p, "")
        for p in r.split(";")
        if p
    )
    freq = parts.get("FREQ", "").lower()
    if not freq:
        return r
    label = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "yearly": "Yearly"}.get(
        freq, freq.capitalize()
    )
    if parts.get("INTERVAL") and parts["INTERVAL"] != "1":
        label += f", every {parts['INTERVAL']}"
    if parts.get("COUNT"):
        label += f", {parts['COUNT']} times"
    if parts.get("UNTIL"):
        label += f", until {_format_ics_instant(parts['UNTIL'])}"
    if parts.get("BYDAY"):
        label += f" ({parts['BYDAY']})"
    return label


def _color_from_props(props: dict[str, str], uid: str) -> str:
    for key in ("COLOR", "X-APPLE-CALENDAR-COLOR", "X-FIELD-COLOR"):
        raw = props.get(key, "").strip()
        if not raw:
            continue
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw):
            return raw.lower()
        if re.fullmatch(r"[0-9A-Fa-f]{6}", raw):
            return f"#{raw.lower()}"
    digest = hashlib.sha256((uid or "event").encode()).hexdigest()
    idx = int(digest[:8], 16) % len(PROTON_EVENT_COLORS)
    return PROTON_EVENT_COLORS[idx]


def _parse_ics_datetime(value: str, meta: dict[str, str], default_tz: str) -> dict[str, Any]:
    tzid = meta.get("TZID") or default_tz
    val = value.strip()
    if meta.get("VALUE") == "DATE" or (len(val) == 8 and val.isdigit()):
        y, m, d = int(val[0:4]), int(val[4:6]), int(val[6:8])
        return {
            "allDay": True,
            "year": y,
            "month": m,
            "day": d,
            "iso": f"{y:04d}-{m:02d}-{d:02d}",
            "tzid": tzid,
        }
    if val.endswith("Z"):
        dt = datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        local = dt
        return {
            "allDay": False,
            "year": local.year,
            "month": local.month,
            "day": local.day,
            "hour": local.hour,
            "minute": local.minute,
            "iso": local.isoformat().replace("+00:00", "Z"),
            "tzid": "UTC",
        }
    if "T" in val:
        dt = datetime.strptime(val, "%Y%m%dT%H%M%S")
        return {
            "allDay": False,
            "year": dt.year,
            "month": dt.month,
            "day": dt.day,
            "hour": dt.hour,
            "minute": dt.minute,
            "iso": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "tzid": tzid,
        }
    y, m, d = int(val[0:4]), int(val[4:6]), int(val[6:8])
    return {
        "allDay": True,
        "year": y,
        "month": m,
        "day": d,
        "iso": f"{y:04d}-{m:02d}-{d:02d}",
        "tzid": tzid,
    }


def _event_occurs_in_month(ev: dict[str, Any], year: int, month: int) -> bool:
    start = ev.get("start") or {}
    end = ev.get("end") or start
    if not start:
        return False
    sy, sm, sd = start.get("year"), start.get("month"), start.get("day")
    ey, em, ed = end.get("year"), end.get("month"), end.get("day")
    if sy is None:
        return False
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)
    try:
        ev_start = date(sy, sm, sd)
        ev_end = date(ey or sy, em or sm, ed or sd)
    except (TypeError, ValueError):
        return False
    if ev.get("status") == "CANCELLED":
        return False
    return ev_start <= month_end and ev_end >= month_start


def parse_ics_calendar(text: str) -> dict[str, Any]:
    lines = unfold_ics_lines(text)
    calendar_name = "Calendar"
    default_tz = "America/New_York"
    events: list[dict[str, Any]] = []
    in_event = False
    props: dict[str, str] = {}
    prop_meta: dict[str, dict[str, str]] = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            props = {}
            prop_meta = {}
            continue
        if line == "END:VEVENT":
            in_event = False
            uid = props.get("UID", "")
            summary = (props.get("SUMMARY") or "(No title)").strip()
            status = (props.get("STATUS") or "CONFIRMED").upper()
            start_raw = props.get("DTSTART", "")
            end_raw = props.get("DTEND") or props.get("DTSTART", "")
            if not start_raw:
                continue
            start = _parse_ics_datetime(start_raw, prop_meta.get("DTSTART", {}), default_tz)
            end = _parse_ics_datetime(end_raw, prop_meta.get("DTEND", {}), default_tz)
            color = _color_from_props(props, uid)
            description = _unescape_ics_text(props.get("DESCRIPTION", "").strip())
            description = re.sub(r"<br\s*/?>", "\n", description, flags=re.I)
            location = _unescape_ics_text(props.get("LOCATION", "").strip())
            organizer = _parse_organizer(props.get("ORGANIZER", ""), prop_meta.get("ORGANIZER"))
            url = props.get("URL", "").strip()
            attendees = [
                _parse_attendee(a.get("value", ""), a.get("meta"))
                if isinstance(a, dict)
                else _parse_attendee(a)
                for a in props.get("_ATTENDEES", [])
            ]
            categories_raw = _unescape_ics_text(props.get("CATEGORIES", ""))
            categories = [c.strip() for c in categories_raw.split(",") if c.strip()]
            rrule = props.get("RRULE", "").strip()
            created = _format_ics_instant(props.get("CREATED", ""))
            last_modified = _format_ics_instant(props.get("LAST-MODIFIED", ""))
            dtstamp = _format_ics_instant(props.get("DTSTAMP", ""))
            transp = props.get("TRANSP", "").strip()
            visibility = props.get("CLASS", "").strip()
            sequence = props.get("SEQUENCE", "").strip()
            geo = props.get("GEO", "").strip()
            priority = props.get("PRIORITY", "").strip()
            events.append(
                {
                    "uid": uid,
                    "summary": summary,
                    "status": status,
                    "color": color,
                    "start": start,
                    "end": end,
                    "description": description,
                    "location": location,
                    "organizer": organizer,
                    "url": url,
                    "attendees": attendees,
                    "categories": categories,
                    "rrule": rrule,
                    "recurrence": _humanize_rrule(rrule),
                    "created": created,
                    "lastModified": last_modified,
                    "dtstamp": dtstamp,
                    "transparency": transp,
                    "visibility": visibility,
                    "sequence": sequence,
                    "geo": geo,
                    "priority": priority,
                }
            )
            continue
        if not in_event:
            name, meta, value = _parse_prop(line)
            if name == "X-WR-CALNAME" and value.strip():
                calendar_name = value.strip()
            elif name == "X-WR-TIMEZONE" and value.strip():
                default_tz = value.strip()
            continue
        name, meta, value = _parse_prop(line)
        if name == "ATTENDEE":
            props.setdefault("_ATTENDEES", []).append({"value": value, "meta": meta})
        else:
            props[name] = value
            prop_meta[name] = meta

    return {
        "name": calendar_name,
        "timezone": default_tz,
        "events": events,
    }