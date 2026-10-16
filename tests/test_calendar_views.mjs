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
