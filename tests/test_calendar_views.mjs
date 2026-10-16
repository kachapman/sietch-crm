// Tests for calendar tile view logic (Phase 1: nav/refetch ranges).
//
// Mirrors of public/app.js#formatCalendarYmd, calendarViewRange,
// normalizeCalendarViewDate, shiftCalendarView, calendarViewNavLabel.
// Keep in sync with app.js — see tests/test_pure_helpers.mjs convention.
//
// Run with:  node --test tests/test_calendar_views.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';

// Stub: app.js persists via saveCalendarsToStorage; tests don't.
function saveCalendarsToStorage() {}

const CALENDAR_MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December",
];
const CALENDAR_MONTH_SHORT = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
const CALENDAR_WEEKDAY_NAMES = [
  "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
];

function calendarViewMonthLabel(cal) {
  const m = CALENDAR_MONTH_NAMES[(cal.viewMonth || 1) - 1] || "";
  return `${m} ${cal.viewYear || ""}`.trim();
}

function normalizeCalendarViewDate(cal) {
  const d = new Date(
    cal.viewYear || new Date().getFullYear(),
    (cal.viewMonth || new Date().getMonth() + 1) - 1,
    cal.viewDay || new Date().getDate()
  );
  cal.viewYear = d.getFullYear();
  cal.viewMonth = d.getMonth() + 1;
  cal.viewDay = d.getDate();
}

function shiftCalendarViewMonth(cal, delta) {
  let y = cal.viewYear || new Date().getFullYear();
  let m = cal.viewMonth || new Date().getMonth() + 1;
  m += delta;
  while (m < 1) { m += 12; y -= 1; }
  while (m > 12) { m -= 12; y += 1; }
  cal.viewYear = y;
  cal.viewMonth = m;
  saveCalendarsToStorage();
}

function shiftCalendarView(cal, delta) {
  const view = cal.view || "month";
  if (view === "month") {
    shiftCalendarViewMonth(cal, delta);
    return;
  }
  if (view === "agenda") return;
  normalizeCalendarViewDate(cal);
  const d = new Date(cal.viewYear, cal.viewMonth - 1, cal.viewDay);
  d.setDate(d.getDate() + delta * (view === "week" ? 7 : 1));
  cal.viewYear = d.getFullYear();
  cal.viewMonth = d.getMonth() + 1;
  cal.viewDay = d.getDate();
  saveCalendarsToStorage();
}

function calendarViewNavLabel(cal) {
  const view = cal.view || "month";
  if (view === "day") {
    const wd = CALENDAR_WEEKDAY_NAMES[new Date(cal.viewYear, cal.viewMonth - 1, cal.viewDay).getDay()] || "";
    return `${wd} ${cal.viewMonth}/${cal.viewDay}/${cal.viewYear}`;
  }
  if (view === "week") {
    const anchor = new Date(cal.viewYear, cal.viewMonth - 1, cal.viewDay);
    const start = new Date(anchor);
    start.setDate(start.getDate() - start.getDay());
    const end = new Date(start);
    end.setDate(end.getDate() + 6);
    const sm = CALENDAR_MONTH_SHORT[start.getMonth()];
    const em = CALENDAR_MONTH_SHORT[end.getMonth()];
    if (start.getMonth() === end.getMonth()) {
      return `${sm} ${start.getDate()} – ${end.getDate()}, ${end.getFullYear()}`;
    }
    return `${sm} ${start.getDate()} – ${em} ${end.getDate()}, ${end.getFullYear()}`;
  }
  if (view === "agenda") return "Upcoming";
  return calendarViewMonthLabel(cal);
}

function formatCalendarYmd(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function calendarViewRange(cal) {
  const view = cal.view || "month";
  const y = cal.viewYear || new Date().getFullYear();
  const m = cal.viewMonth || new Date().getMonth() + 1;
  const day = cal.viewDay || new Date().getDate();
  if (view === "day") {
    const d = new Date(y, m - 1, day);
    const s = formatCalendarYmd(d);
    return { startStr: s, endStr: s };
  }
  if (view === "week") {
    const anchor = new Date(y, m - 1, day);
    const start = new Date(anchor);
    start.setDate(start.getDate() - start.getDay());
    const end = new Date(start);
    end.setDate(end.getDate() + 6);
    return { startStr: formatCalendarYmd(start), endStr: formatCalendarYmd(end) };
  }
  if (view === "agenda") {
    const now = new Date();
    const end = new Date(now);
    end.setDate(end.getDate() + 90);
    return { startStr: formatCalendarYmd(now), endStr: formatCalendarYmd(end) };
  }
  const startDate = new Date(y, m - 1, 1);
  const endDate = new Date(y, m, 0);
  return { startStr: formatCalendarYmd(startDate), endStr: formatCalendarYmd(endDate) };
}

// ── range tests ──────────────────────────────────────────────────────

test('month range covers the whole month', () => {
  const r = calendarViewRange({ view: 'month', viewYear: 2026, viewMonth: 2, viewDay: 15 });
  assert.equal(r.startStr, '2026-02-01');
  assert.equal(r.endStr, '2026-02-28');
});

test('day range is a single date', () => {
  const r = calendarViewRange({ view: 'day', viewYear: 2026, viewMonth: 10, viewDay: 16 });
  assert.deepEqual(r, { startStr: '2026-10-16', endStr: '2026-10-16' });
});

test('week range is Sun–Sat around the viewed day', () => {
  // 2026-10-16 is a Friday; week = Oct 11 (Sun) – Oct 17 (Sat).
  const r = calendarViewRange({ view: 'week', viewYear: 2026, viewMonth: 10, viewDay: 16 });
  assert.deepEqual(r, { startStr: '2026-10-11', endStr: '2026-10-17' });
});

test('week range crosses a month boundary', () => {
  // 2026-11-01 is a Sunday; week = Nov 01 – Nov 07.
  const r = calendarViewRange({ view: 'week', viewYear: 2026, viewMonth: 11, viewDay: 1 });
  assert.deepEqual(r, { startStr: '2026-11-01', endStr: '2026-11-07' });
});

test('agenda range is today plus 90 days', () => {
  const now = new Date();
  const end = new Date(now);
  end.setDate(end.getDate() + 90);
  const r = calendarViewRange({ view: 'agenda' });
  assert.equal(r.startStr, formatCalendarYmd(now));
  assert.equal(r.endStr, formatCalendarYmd(end));
});

// ── shift tests ──────────────────────────────────────────────────────

test('month shift wraps year boundaries', () => {
  const cal = { view: 'month', viewYear: 2026, viewMonth: 1 };
  shiftCalendarView(cal, -1);
  assert.equal(cal.viewYear, 2025);
  assert.equal(cal.viewMonth, 12);
});

test('week shift moves 7 days across month boundary', () => {
  const cal = { view: 'week', viewYear: 2026, viewMonth: 10, viewDay: 30 };
  shiftCalendarView(cal, 1);
  assert.equal(cal.viewMonth, 11);
  assert.equal(cal.viewDay, 6);
});

test('day shift moves 1 day across year boundary', () => {
  const cal = { view: 'day', viewYear: 2026, viewMonth: 1, viewDay: 1 };
  shiftCalendarView(cal, -1);
  assert.deepEqual([cal.viewYear, cal.viewMonth, cal.viewDay], [2025, 12, 31]);
});

test('agenda shift is a no-op (anchored at today)', () => {
  const cal = { view: 'agenda', viewYear: 2026, viewMonth: 10, viewDay: 16 };
  shiftCalendarView(cal, 1);
  assert.deepEqual([cal.viewYear, cal.viewMonth, cal.viewDay], [2026, 10, 16]);
});

// ── label tests ──────────────────────────────────────────────────────

test('month label unchanged', () => {
  assert.equal(calendarViewNavLabel({ view: 'month', viewYear: 2026, viewMonth: 10 }), 'October 2026');
});

test('day label has weekday and date', () => {
  assert.equal(
    calendarViewNavLabel({ view: 'day', viewYear: 2026, viewMonth: 10, viewDay: 16 }),
    'Friday 10/16/2026'
  );
});

test('week label shows range within a month', () => {
  assert.equal(
    calendarViewNavLabel({ view: 'week', viewYear: 2026, viewMonth: 10, viewDay: 16 }),
    'Oct 11 – 17, 2026'
  );
});

test('week label shows range across months', () => {
  assert.equal(
    calendarViewNavLabel({ view: 'week', viewYear: 2026, viewMonth: 11, viewDay: 1 }),
    'Nov 1 – 7, 2026'
  );
});

test('agenda label is Upcoming', () => {
  assert.equal(calendarViewNavLabel({ view: 'agenda' }), 'Upcoming');
});

// ── Phase 3 mirrors ────────────────────────────────────────────────
// Mirrors of public/app.js#formatCalendarClock, layoutDayEvents,
// calendarWeekStartDate. Keep in sync with app.js.

function formatCalendarClock(hour, minute, timeFormat) {
  const fmt = timeFormat || '24h';
  const m = String(minute).padStart(2, "0");
  if (fmt === "12h") {
    const suffix = hour >= 12 ? "PM" : "AM";
    const h = hour % 12 === 0 ? 12 : hour % 12;
    return `${h}:${m} ${suffix}`;
  }
  return `${String(hour).padStart(2, "0")}:${m}`;
}

function layoutDayEvents(spans) {
  const idx = spans.map((s, i) => ({ ...s, _i: i })).sort((a, b) => a.start - b.start || a.end - b.end);
  const out = new Array(spans.length);
  let cluster = [];
  let clusterEnd = -1;
  const flush = () => {
    const cols = [];
    for (const ev of cluster) {
      let c = 0;
      while (cols[c] != null && cols[c] > ev.start) c++;
      cols[c] = ev.end;
      out[ev._i] = { col: c, ncols: 0 };
    }
    const ncols = cols.length || 1;
    for (const ev of cluster) out[ev._i].ncols = ncols;
    cluster = [];
    clusterEnd = -1;
  };
  for (const ev of idx) {
    if (cluster.length && ev.start >= clusterEnd) flush();
    cluster.push(ev);
    clusterEnd = Math.max(clusterEnd, ev.end);
  }
  if (cluster.length) flush();
  return out;
}

function calendarWeekStartDate(y, m, d, weekStart) {
  const ws = weekStart === "mon" ? "mon" : "sun";
  const anchor = new Date(y, m - 1, d);
  const shift = ws === "mon" ? (anchor.getDay() + 6) % 7 : anchor.getDay();
  const start = new Date(anchor);
  start.setDate(start.getDate() - shift);
  return start;
}

test('24h clock pads hours', () => {
  assert.equal(formatCalendarClock(9, 5, '24h'), '09:05');
  assert.equal(formatCalendarClock(14, 30, '24h'), '14:30');
});

test('12h clock converts correctly', () => {
  assert.equal(formatCalendarClock(0, 0, '12h'), '12:00 AM');
  assert.equal(formatCalendarClock(9, 5, '12h'), '9:05 AM');
  assert.equal(formatCalendarClock(12, 0, '12h'), '12:00 PM');
  assert.equal(formatCalendarClock(14, 30, '12h'), '2:30 PM');
  assert.equal(formatCalendarClock(23, 59, '12h'), '11:59 PM');
});

test('non-overlapping events each take the full width', () => {
  const out = layoutDayEvents([
    { start: 540, end: 600 },
    { start: 660, end: 720 },
  ]);
  assert.deepEqual(out, [{ col: 0, ncols: 1 }, { col: 0, ncols: 1 }]);
});

test('overlapping events share columns', () => {
  const out = layoutDayEvents([
    { start: 540, end: 660 },
    { start: 600, end: 720 },
  ]);
  assert.deepEqual(out, [{ col: 0, ncols: 2 }, { col: 1, ncols: 2 }]);
});

test('three-way overlap gets three columns', () => {
  const out = layoutDayEvents([
    { start: 540, end: 700 },
    { start: 560, end: 640 },
    { start: 600, end: 720 },
  ]);
  assert.deepEqual(out.map(o => o.ncols), [3, 3, 3]);
  assert.deepEqual(out.map(o => o.col).sort(), [0, 1, 2]);
});

test('adjacent events do not overlap', () => {
  const out = layoutDayEvents([
    { start: 540, end: 600 },
    { start: 600, end: 660 },
  ]);
  assert.deepEqual(out, [{ col: 0, ncols: 1 }, { col: 0, ncols: 1 }]);
});

test('separate clusters are independent', () => {
  const out = layoutDayEvents([
    { start: 540, end: 660 },
    { start: 600, end: 620 },
    { start: 800, end: 860 },
  ]);
  assert.equal(out[2].ncols, 1);
  assert.equal(out[0].ncols, 2);
});

test('week start Sunday by default', () => {
  const s = calendarWeekStartDate(2026, 10, 16, 'sun');
  assert.equal(formatCalendarYmd(s), '2026-10-11');
});

test('week start Monday shifts the anchor', () => {
  const s = calendarWeekStartDate(2026, 10, 16, 'mon');
  assert.equal(formatCalendarYmd(s), '2026-10-12');
});

test('week start Monday on a Sunday goes back 6 days', () => {
  const s = calendarWeekStartDate(2026, 10, 18, 'mon');
  assert.equal(formatCalendarYmd(s), '2026-10-12');
});

// ── Phase 4 mirrors ────────────────────────────────────────────────
// Mirrors of public/app.js#buildRruleFromControls, parseRruleToControls,
// attendeeLineToObj, attendeeObjToLine. Keep in sync with app.js.

function buildRruleFromControls({ freq, interval, byday, endCond, count, until }) {
  if (!freq) return "";
  const parts = [`FREQ=${freq}`];
  const n = parseInt(interval, 10);
  if (Number.isFinite(n) && n > 1) parts.push(`INTERVAL=${n}`);
  if (freq === "WEEKLY" && Array.isArray(byday) && byday.length) {
    parts.push(`BYDAY=${byday.join(",")}`);
  }
  if (endCond === "count") {
    const c = parseInt(count, 10);
    if (Number.isFinite(c) && c >= 2) parts.push(`COUNT=${Math.min(c, 500)}`);
  } else if (endCond === "until" && until) {
    const d = String(until).replace(/-/g, "");
    if (/^\d{8}$/.test(d)) parts.push(`UNTIL=${d}T000000Z`);
  }
  return parts.join(";");
}

function parseRruleToControls(rrule) {
  const WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
  if (!rrule) return { freq: "", interval: 1, byday: [], endCond: "never", count: 10, until: "" };
  const dict = {};
  for (const part of String(rrule).split(";")) {
    if (part.includes("=")) {
      const [k, v] = part.split("=", 2);
      dict[k.trim().toUpperCase()] = v.trim().toUpperCase();
    }
  }
  if (!["DAILY", "WEEKLY", "MONTHLY", "YEARLY"].includes(dict.FREQ)) return null;
  if (dict.BYMONTHDAY || dict.BYMONTH || dict.BYYEARDAY || dict.BYWEEKNO || dict.BYHOUR || dict.BYMINUTE) return null;
  if (dict.BYDAY && dict.FREQ !== "WEEKLY") return null;
  const byday = dict.FREQ === "WEEKLY" && dict.BYDAY
    ? dict.BYDAY.split(",").filter((d) => WEEKDAYS.includes(d))
    : [];
  if (dict.BYDAY && dict.FREQ === "WEEKLY" && !byday.length) return null;
  const out = {
    freq: dict.FREQ,
    interval: Math.max(parseInt(dict.INTERVAL || "1", 10) || 1, 1),
    byday, endCond: "never", count: 10, until: "",
  };
  if (dict.COUNT) {
    const c = parseInt(dict.COUNT, 10);
    if (!Number.isFinite(c) || c < 2) return null;
    out.endCond = "count";
    out.count = Math.min(c, 500);
  } else if (dict.UNTIL) {
    const m = dict.UNTIL.match(/^(\d{4})(\d{2})(\d{2})/);
    if (!m) return null;
    out.endCond = "until";
    out.until = `${m[1]}-${m[2]}-${m[3]}`;
  }
  return out;
}

function attendeeLineToObj(line) {
  const s = String(line || "").trim();
  if (!s) return null;
  const m = s.match(/^(.*?)\s*<([^<>@\s]+@[^<>@\s]+)>\s*$/);
  if (m) return { name: m[1].trim(), email: m[2].trim() };
  if (/^[^@\s]+@[^@\s]+$/.test(s)) return { name: "", email: s };
  return { name: s, email: "" };
}

function attendeeObjToLine(a) {
  const name = String(a?.name || "").trim();
  const email = String(a?.email || "").trim();
  if (name && email) return `${name} <${email}>`;
  return name || email;
}

test('empty freq builds no rule', () => {
  assert.equal(buildRruleFromControls({ freq: '' }), '');
});

test('daily with interval and count', () => {
  assert.equal(
    buildRruleFromControls({ freq: 'DAILY', interval: '2', byday: [], endCond: 'count', count: '10', until: '' }),
    'FREQ=DAILY;INTERVAL=2;COUNT=10'
  );
});

test('weekly with days and until date', () => {
  assert.equal(
    buildRruleFromControls({ freq: 'WEEKLY', interval: '1', byday: ['MO', 'WE'], endCond: 'until', count: '', until: '2026-12-31' }),
    'FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231T000000Z'
  );
});

test('count clamps to 500, interval 1 omitted', () => {
  assert.equal(
    buildRruleFromControls({ freq: 'MONTHLY', interval: '1', byday: [], endCond: 'count', count: '9999', until: '' }),
    'FREQ=MONTHLY;COUNT=500'
  );
});

test('rrule round-trips through parse', () => {
  const built = buildRruleFromControls({ freq: 'WEEKLY', interval: '2', byday: ['TU'], endCond: 'count', count: '8', until: '' });
  assert.deepEqual(parseRruleToControls(built), {
    freq: 'WEEKLY', interval: 2, byday: ['TU'], endCond: 'count', count: 8, until: '',
  });
});

test('complex rules return null (kept as-is)', () => {
  assert.equal(parseRruleToControls('FREQ=MONTHLY;BYDAY=3TU'), null);
  assert.equal(parseRruleToControls('FREQ=MINUTELY;INTERVAL=5'), null);
  assert.equal(parseRruleToControls('FREQ=YEARLY;BYMONTH=1'), null);
});

test('empty rrule parses to defaults', () => {
  assert.deepEqual(parseRruleToControls(''), {
    freq: '', interval: 1, byday: [], endCond: 'never', count: 10, until: '',
  });
});

test('attendee lines parse', () => {
  assert.deepEqual(attendeeLineToObj('Jane <jane@x.com>'), { name: 'Jane', email: 'jane@x.com' });
  assert.deepEqual(attendeeLineToObj('jane@x.com'), { name: '', email: 'jane@x.com' });
  assert.deepEqual(attendeeLineToObj('Jane'), { name: 'Jane', email: '' });
  assert.equal(attendeeLineToObj('  '), null);
});

test('attendee objects format back to lines', () => {
  assert.equal(attendeeObjToLine({ name: 'Jane', email: 'jane@x.com' }), 'Jane <jane@x.com>');
  assert.equal(attendeeObjToLine({ name: '', email: 'j@x.com' }), 'j@x.com');
  assert.equal(attendeeObjToLine({ name: 'Jane', email: '' }), 'Jane');
});

// ── Phase 5 mirrors ────────────────────────────────────────────────
// Mirrors of public/app.js#calendarTileSourceParam, calendarAggregateName.

function calendarTileSourceParam(cal) {
  const id = Number(cal.onlySourceId);
  return Number.isFinite(id) && id > 0 ? `&source_id=${id}` : "";
}

function calendarAggregateName(events, fallback) {
  const names = [...new Set((events || []).map((e) => e.source_name).filter(Boolean))];
  if (names.length === 1) return names[0];
  if (names.length > 1) return `${names.length} calendars`;
  return fallback;
}

test('no filter means no source param', () => {
  assert.equal(calendarTileSourceParam({}), '');
  assert.equal(calendarTileSourceParam({ onlySourceId: null }), '');
  assert.equal(calendarTileSourceParam({ onlySourceId: 'abc' }), '');
});

test('filtered tile passes source_id', () => {
  assert.equal(calendarTileSourceParam({ onlySourceId: 7 }), '&source_id=7');
  assert.equal(calendarTileSourceParam({ onlySourceId: '7' }), '&source_id=7');
});

test('aggregate name prefers the single source', () => {
  assert.equal(
    calendarAggregateName([{ source_name: 'Team' }, { source_name: 'Team' }], 'Cal'),
    'Team'
  );
});

test('aggregate name counts multiple sources', () => {
  assert.equal(
    calendarAggregateName([{ source_name: 'A' }, { source_name: 'B' }], 'Cal'),
    '2 calendars'
  );
});

test('aggregate name falls back with no source names', () => {
  assert.equal(calendarAggregateName([], 'Cal'), 'Cal');
  assert.equal(calendarAggregateName([{ source_name: null }], 'Cal'), 'Cal');
});
