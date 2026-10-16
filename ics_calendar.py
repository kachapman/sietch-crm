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


WEEKDAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# Recurrence expansion bounds (Phase 2: expand at sync; infinite series must end somewhere).
EXPANSION_PAST_DAYS = 365
EXPANSION_FUTURE_DAYS = 730
EXPANSION_MAX_OCCURRENCES = 500


def _parse_ics_duration(raw: str) -> timedelta | None:
    """Parse an iCalendar DURATION (P1W, P1DT2H, PT15M) into a timedelta."""
    s = (raw or "").strip().upper()
    if not s:
        return None
    sign = 1
    if s.startswith("-"):
        sign = -1
        s = s[1:]
    elif s.startswith("+"):
        s = s[1:]
    m = re.fullmatch(r"P(?:(\d+)W|(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?)", s)
    if not m or s == "P":
        return None
    weeks, days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return sign * timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


def _parse_alarm_trigger_minutes(raw: str) -> int | None:
    """VALARM TRIGGER like -PT15M → 15 (minutes before start). Post-start triggers ignored."""
    s = (raw or "").strip()
    if not s.startswith("-"):
        return None
    delta = _parse_ics_duration(s)
    if delta is None:
        return None
    mins = int(-delta.total_seconds() // 60)
    return mins if mins > 0 else None


def _event_start_naive(ev: dict[str, Any]) -> datetime | None:
    """Naive datetime for expansion math (same convention as calendar_sync._to_timestamp)."""
    s = ev.get("start") or {}
    try:
        if s.get("allDay"):
            return datetime(s["year"], s["month"], s["day"])
        iso = s.get("iso", "")
        if iso:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None)
        return datetime(s["year"], s["month"], s["day"], s.get("hour", 0), s.get("minute", 0))
    except (KeyError, TypeError, ValueError):
        return None


def _naive_to_start_dict(dt: datetime, all_day: bool, tzid: str) -> dict[str, Any]:
    if all_day:
        return {
            "allDay": True, "year": dt.year, "month": dt.month, "day": dt.day,
            "iso": f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}", "tzid": tzid,
        }
    return {
        "allDay": False, "year": dt.year, "month": dt.month, "day": dt.day,
        "hour": dt.hour, "minute": dt.minute,
        "iso": dt.strftime("%Y-%m-%dT%H:%M:%S"), "tzid": tzid,
    }


def occurrence_uid(master_uid: str, start: datetime, all_day: bool) -> str:
    """Stable per-occurrence UID so sync upserts are idempotent."""
    if all_day:
        return f"{master_uid}#{start.strftime('%Y%m%d')}"
    return f"{master_uid}#{start.strftime('%Y%m%dT%H%M%S')}"


def _parse_rrule_dict(rrule: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (rrule or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip().upper()
    return out


def _parse_weekday_entry(entry: str) -> tuple[int | None, str] | None:
    """'MO' → (None, 'MO'); '3TU' → (3, 'TU'); '-1FR' → (-1, 'FR')."""
    m = re.fullmatch(r"([+-]?\d+)?([A-Z]{2})", (entry or "").strip().upper())
    if not m or m.group(2) not in WEEKDAY_CODES:
        return None
    num = int(m.group(1)) if m.group(1) else None
    return num, m.group(2)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date | None:
    """n=3 → 3rd <weekday>; n=-1 → last <weekday> of the month."""
    import calendar as _cal

    month_days = _cal.monthrange(year, month)[1]
    if n > 0:
        first_wd = date(year, month, 1).weekday()
        day = 1 + ((weekday - first_wd) % 7) + (n - 1) * 7
        return date(year, month, day) if day <= month_days else None
    last_wd = date(year, month, month_days).weekday()
    day = month_days - ((last_wd - weekday) % 7)
    return date(year, month, day)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


def _generate_rrule_starts(
    start0: datetime, rule: dict[str, str], window_end: datetime, cap: int
) -> list[datetime]:
    """Candidate occurrence starts within (start0, window_end], before COUNT/UNTIL/cap.

    DTSTART itself is always the first instance (handled by the caller).
    """
    freq = rule.get("FREQ", "")
    try:
        interval = max(int(rule.get("INTERVAL", "1") or "1"), 1)
    except ValueError:
        interval = 1
    count_raw = rule.get("COUNT", "")
    count = int(count_raw) if count_raw.isdigit() else None
    until: datetime | None = None
    if rule.get("UNTIL"):
        try:
            u = rule["UNTIL"]
            if len(u) == 8 and u.isdigit():
                until = datetime(int(u[0:4]), int(u[4:6]), int(u[6:8])) + timedelta(days=1)
            elif "T" in u:
                until = datetime.strptime(u[:15], "%Y%m%dT%H%M%S") + timedelta(seconds=1)
            else:
                until = datetime.strptime(u[:8], "%Y%m%d") + timedelta(days=1)
        except (ValueError, IndexError):
            until = None

    limit = window_end if until is None else min(window_end, until)
    max_extra = cap - 1  # DTSTART takes the first slot
    if count is not None:
        max_extra = min(max_extra, count - 1)
    if max_extra <= 0:
        return []

    out: list[datetime] = []

    def accept(dt: datetime) -> bool:
        return dt > start0 and dt <= limit

    if freq == "DAILY":
        d = start0
        while len(out) < max_extra:
            d += timedelta(days=interval)
            if d > limit:
                break
            out.append(d)
    elif freq == "WEEKLY":
        bydays = [e for e in rule.get("BYDAY", "").split(",") if e]
        wds = [WEEKDAY_CODES[e] for e in bydays if e in WEEKDAY_CODES]
        if not wds:
            wds = [start0.weekday()]
        wkst = WEEKDAY_CODES.get(rule.get("WKST", "MO"), 0)
        week_start = start0 - timedelta(days=(start0.weekday() - wkst) % 7)
        w = 0
        while len(out) < max_extra:
            base = week_start + timedelta(weeks=w * interval)
            earliest = base + timedelta(days=min((wd - wkst) % 7 for wd in wds))
            if earliest.replace(hour=start0.hour, minute=start0.minute, second=0, microsecond=0) > limit and w > 0:
                # Even the earliest slot this cycle is past the limit; later cycles only get later.
                if (base - week_start).days > 0 and earliest > limit:
                    break
            for wd in sorted(wds):
                cand = (base + timedelta(days=(wd - wkst) % 7)).replace(
                    hour=start0.hour, minute=start0.minute, second=0, microsecond=0
                )
                if accept(cand):
                    out.append(cand)
                    if len(out) >= max_extra:
                        break
            w += 1
            if w > 3000:  # hard stop for pathological rules
                break
        out.sort()
    elif freq == "MONTHLY":
        bymonthday = [e for e in rule.get("BYMONTHDAY", "").split(",") if e]
        byday = [_parse_weekday_entry(e) for e in rule.get("BYDAY", "").split(",") if e]
        byday = [b for b in byday if b]
        m = 0
        while len(out) < max_extra:
            y, mo = _add_months(start0.year, start0.month, m * interval)
            if datetime(y, mo, 1) > limit + timedelta(days=31):
                break
            if byday and not bymonthday:
                import calendar as _cal

                month_days = _cal.monthrange(y, mo)[1]
                for num, code in byday:
                    wd = WEEKDAY_CODES[code]
                    if num is None:
                        # Bare weekday in MONTHLY = every such weekday of the month.
                        d = date(y, mo, 1) + timedelta(days=(wd - date(y, mo, 1).weekday()) % 7)
                        while d.month == mo:
                            cand = datetime(d.year, d.month, d.day, start0.hour, start0.minute)
                            if accept(cand):
                                out.append(cand)
                            d += timedelta(days=7)
                    else:
                        dd = _nth_weekday_of_month(y, mo, wd, num)
                        if dd:
                            cand = datetime(dd.year, dd.month, dd.day, start0.hour, start0.minute)
                            if accept(cand):
                                out.append(cand)
            else:
                days = [int(x) for x in bymonthday if x.lstrip("-").isdigit()] or [start0.day]
                import calendar as _cal

                month_days = _cal.monthrange(y, mo)[1]
                for dd in days:
                    day = dd if dd > 0 else month_days + dd + 1
                    if 1 <= day <= month_days:
                        cand = datetime(y, mo, day, start0.hour, start0.minute)
                        if accept(cand):
                            out.append(cand)
            m += 1
            if m > 2400:
                break
        out = sorted(set(out))
    elif freq == "YEARLY":
        bymonth = [int(x) for x in rule.get("BYMONTH", "").split(",") if x.isdigit()]
        months = bymonth or [start0.month]
        bymonthday = [int(x) for x in rule.get("BYMONTHDAY", "").split(",") if x.lstrip("-").isdigit()]
        days = bymonthday or [start0.day]
        y = start0.year
        while len(out) < max_extra:
            if y > limit.year + 1:
                break
            if (y - start0.year) % interval == 0:
                import calendar as _cal

                for mo in sorted(set(months)):
                    if not 1 <= mo <= 12:
                        continue
                    month_days = _cal.monthrange(y, mo)[1]
                    for dd in days:
                        if 1 <= dd <= month_days:
                            cand = datetime(y, mo, dd, start0.hour, start0.minute)
                            if accept(cand):
                                out.append(cand)
            y += 1
        out = sorted(set(out))
    else:
        return []

    return out[:max_extra]


def expand_event_occurrences(
    ev: dict[str, Any],
    window_start: date,
    window_end: date,
    max_occurrences: int = EXPANSION_MAX_OCCURRENCES,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expand one parsed event into its occurrences within [window_start, window_end].

    Non-recurring events return [ev] unchanged. Occurrences are copies with
    replaced start/end/uid plus isOccurrence/recurrenceId markers; a
    RECURRENCE-ID override (keyed by naive start ISO) replaces that instance.
    """
    if not ev.get("rrule"):
        return [ev]
    start0 = _event_start_naive(ev)
    if start0 is None:
        return [ev]
    overrides = overrides or {}
    end0 = _event_start_naive({"start": ev.get("end")}) or start0
    duration = end0 - start0
    if duration <= timedelta(0):
        duration = timedelta(days=1) if ev.get("start", {}).get("allDay") else timedelta(hours=1)
    rule = _parse_rrule_dict(ev["rrule"])
    if not rule.get("FREQ"):
        return [ev]

    window_end_dt = datetime(window_end.year, window_end.month, window_end.day, 23, 59, 59)
    window_start_dt = datetime(window_start.year, window_start.month, window_start.day)

    exdates = {_event_start_naive({"start": x}) for x in ev.get("exdates", [])}
    exdates.discard(None)
    rdates = sorted(
        {
            d
            for d in (_event_start_naive({"start": x}) for x in ev.get("rdates", []))
            if d is not None and window_start_dt <= d <= window_end_dt
        }
    )

    starts = [start0] if window_start_dt <= start0 <= window_end_dt else []
    starts += _generate_rrule_starts(start0, rule, window_end_dt, max_occurrences)
    starts += [rd for rd in rdates if rd not in starts]
    # Generated starts predate the window when DTSTART is old — clip them too.
    starts = sorted(s for s in set(starts) if window_start_dt <= s <= window_end_dt)
    starts = [s for s in starts if s not in exdates][:max_occurrences]

    all_day = bool(ev.get("start", {}).get("allDay"))
    tzid = ev.get("start", {}).get("tzid", "")
    end_tzid = ev.get("end", {}).get("tzid", tzid)
    out: list[dict[str, Any]] = []
    for s in starts:
        occ = dict(ev)
        occ["start"] = _naive_to_start_dict(s, all_day, tzid)
        occ["end"] = _naive_to_start_dict(s + duration, all_day, end_tzid)
        occ["uid"] = occurrence_uid(ev.get("uid", ""), s, all_day)
        occ["isOccurrence"] = True
        occ["recurrenceId"] = ev.get("uid", "")
        ov = overrides.get(s.isoformat())
        if ov:
            for k in ("summary", "description", "location", "status", "url", "organizer"):
                if ov.get(k):
                    occ[k] = ov[k]
            if ov.get("start"):
                occ["start"] = ov["start"]
                os_ = _event_start_naive({"start": ov["start"]})
                oe_ = _event_start_naive({"start": ov.get("end")}) or (os_ + duration if os_ else None)
                if os_ and oe_:
                    occ["end"] = _naive_to_start_dict(oe_, all_day, end_tzid)
                    # Stable UID follows the actual (overridden) start.
                    occ["uid"] = occurrence_uid(ev.get("uid", ""), os_, all_day)
        out.append(occ)
    return out


def _recurrence_id_key(raw: str) -> str:
    """Naive-ISO key for a RECURRENCE-ID value so overrides match generated starts."""
    raw = (raw or "").strip()
    try:
        if raw.endswith("Z"):
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").isoformat()
        if "T" in raw:
            return datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").isoformat()
        return datetime.strptime(raw[:8], "%Y%m%d").isoformat()
    except (ValueError, TypeError):
        return ""


def expand_calendar_events(
    events: list[dict[str, Any]],
    window_start: date,
    window_end: date,
    max_occurrences: int = EXPANSION_MAX_OCCURRENCES,
) -> list[dict[str, Any]]:
    """Expand all recurring masters; RECURRENCE-ID rows become per-instance overrides."""
    masters = [e for e in events if not e.get("recurrenceIdRaw")]
    # Overrides share the master UID; the instance they patch is in recurrenceIdRaw.
    by_uid: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("recurrenceIdRaw"):
            by_uid.setdefault(e.get("uid", ""), []).append(e)
    out: list[dict[str, Any]] = []
    for m in masters:
        ov_map: dict[str, dict[str, Any]] = {}
        for ov in by_uid.get(m.get("uid", ""), []):
            key = _recurrence_id_key(ov.get("recurrenceIdRaw", ""))
            if key:
                ov_map[key] = ov
        out.extend(expand_event_occurrences(m, window_start, window_end, max_occurrences, ov_map))
    # Standalone overrides whose master is absent pass through as-is.
    known = {m.get("uid", "") for m in masters}
    out.extend(
        e for e in events if e.get("recurrenceIdRaw") and e.get("uid", "") not in known
    )
    return out


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
            if "DTEND" in props:
                end = _parse_ics_datetime(end_raw, prop_meta.get("DTEND", {}), default_tz)
            elif props.get("DURATION", "").strip():
                # No DTEND: DURATION defines the end (RFC 5545 3.6.1).
                dur = _parse_ics_duration(props.get("DURATION", ""))
                try:
                    if start.get("allDay"):
                        base = datetime(start["year"], start["month"], start["day"])
                    elif start.get("iso", "").endswith("Z"):
                        base = datetime.fromisoformat(start["iso"].replace("Z", "+00:00")).replace(tzinfo=None)
                    else:
                        base = datetime(
                            start["year"], start["month"], start["day"],
                            start.get("hour", 0), start.get("minute", 0),
                        )
                    if dur is not None and dur.total_seconds() > 0:
                        e = base + dur
                        end = _naive_to_start_dict(
                            e, bool(start.get("allDay")), prop_meta.get("DTSTART", {}).get("TZID") or default_tz
                        )
                    else:
                        end = _parse_ics_datetime(end_raw, prop_meta.get("DTEND", {}), default_tz)
                except (ValueError, KeyError, TypeError):
                    end = _parse_ics_datetime(end_raw, prop_meta.get("DTEND", {}), default_tz)
            else:
                end = _parse_ics_datetime(end_raw, prop_meta.get("DTEND", {}), default_tz)
            color = _color_from_props(props, uid)
            description = _unescape_ics_text(props.get("DESCRIPTION", "").strip())
            description = re.sub(r"<br\s*/?>", "\n", description, flags=re.I)
            location = _unescape_ics_text(props.get("LOCATION", "").strip())
            organizer = _parse_organizer(props.get("ORGANIZER", ""), prop_meta.get("ORGANIZER"))
            url = props.get("URL", "").strip()
            # EXDATE/RDATE can repeat and hold comma-separated lists; each part
            # keeps its own params (TZID/VALUE) for correct parsing.
            exdates: list[dict[str, Any]] = []
            for raw_list, meta in props.get("_EXDATES", []):
                for part in raw_list.split(","):
                    part = part.strip()
                    if part:
                        try:
                            exdates.append(_parse_ics_datetime(part, meta, default_tz))
                        except (ValueError, IndexError):
                            continue
            rdates: list[dict[str, Any]] = []
            for raw_list, meta in props.get("_RDATES", []):
                for part in raw_list.split(","):
                    part = part.strip()
                    if part:
                        try:
                            rdates.append(_parse_ics_datetime(part, meta, default_tz))
                        except (ValueError, IndexError):
                            continue
            recurrence_id_raw = props.get("RECURRENCE-ID", "").strip()
            recurrence_range = (prop_meta.get("RECURRENCE-ID", {}).get("RANGE") or "").strip().upper()
            alarm_minutes = _parse_alarm_trigger_minutes(props.get("TRIGGER", ""))
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
                    "exdates": exdates,
                    "rdates": rdates,
                    "recurrenceId": recurrence_id_raw,
                    "recurrenceIdRaw": recurrence_id_raw,
                    "recurrenceRange": recurrence_range,
                    "duration": props.get("DURATION", "").strip(),
                    "alarmMinutes": alarm_minutes,
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
        elif name in ("EXDATE", "RDATE"):
            # Repeatable + comma-separated; keep each raw list with its params.
            props.setdefault("_" + name + "S", []).append((value, meta))
        else:
            props[name] = value
            prop_meta[name] = meta

    return {
        "name": calendar_name,
        "timezone": default_tz,
        "events": events,
    }