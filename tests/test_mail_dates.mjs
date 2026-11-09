// Tests for the one canonical mail date formatter (public/mail-dates.js).
//
// Live bug 2026-11-09: an email Outlook shows as received Mon 11/9/2026
// 1:20 PM (EST = 18:20:03Z) appeared in Sietch as "11/9/2026, 2:20:03 AM".
// The old UI fed offset-less strings to `new Date()` (parsed as browser-local)
// and rendered in the browser timezone, with ~40 hand-rolled formatters.
//
// This file imports the REAL module, so the rendering assertions below are
// about the code that ships (not a copy of it).
//
// Run with:  node --test tests/test_mail_dates.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MAIL_DEFAULT_TIMEZONE,
  parseMailDateInput,
  formatMailDate,
  formatMailDateOrRaw,
  resolveMailDisplayTimezone,
} from '../public/mail-dates.js';

const NY = { timezone: 'America/New_York' };

test('the default display timezone is America/New_York', () => {
  assert.equal(MAIL_DEFAULT_TIMEZONE, 'America/New_York');
  assert.equal(resolveMailDisplayTimezone({ timezone: 'UTC' }), 'UTC');
  assert.equal(resolveMailDisplayTimezone({ userTimezone: 'America/Chicago' }), 'America/Chicago');
  assert.equal(resolveMailDisplayTimezone({ timezone: 'Europe/Berlin', userTimezone: 'UTC' }), 'Europe/Berlin');
});

test('an offset-less timestamp is UTC, never browser-local', () => {
  // The API emits naive UTC for some legacy paths; parsing it as local time
  // is exactly what shifted the displayed hour.
  const a = parseMailDateInput('2026-11-09 18:20:03');
  const b = parseMailDateInput('2026-11-09T18:20:03');
  const c = parseMailDateInput('2026-11-09T18:20:03Z');
  assert.equal(a.toISOString(), '2026-11-09T18:20:03.000Z');
  assert.equal(b.toISOString(), '2026-11-09T18:20:03.000Z');
  assert.equal(c.toISOString(), '2026-11-09T18:20:03.000Z');
});

test('ISO with an offset keeps its instant', () => {
  assert.equal(parseMailDateInput('2026-11-09T13:20:03-05:00').toISOString(),
    '2026-11-09T18:20:03.000Z');
});

test('RFC-2822 Date headers are understood', () => {
  assert.equal(parseMailDateInput('Mon, 9 Nov 2026 13:20:03 -0500').toISOString(),
    '2026-11-09T18:20:03.000Z');
  assert.equal(parseMailDateInput('Mon, 09 Nov 2026 18:20:03 GMT').toISOString(),
    '2026-11-09T18:20:03.000Z');
});

test('epoch seconds and milliseconds are both accepted', () => {
  const ms = Date.parse('2026-11-09T18:20:03Z');
  assert.equal(parseMailDateInput(ms).toISOString(), '2026-11-09T18:20:03.000Z');
  assert.equal(parseMailDateInput(ms / 1000).toISOString(), '2026-11-09T18:20:03.000Z');
});

test('Graph/OnlyOffice { value } wrappers are unwrapped', () => {
  assert.equal(parseMailDateInput({ value: '2026-11-09T18:20:03Z' }).toISOString(),
    '2026-11-09T18:20:03.000Z');
  assert.equal(parseMailDateInput({ dateTime: '2026-11-09T18:20:03Z' }).toISOString(),
    '2026-11-09T18:20:03.000Z');
});

test('empty and invalid values return null / empty string', () => {
  for (const v of [null, undefined, '', '   ', 'not a date']) {
    assert.equal(parseMailDateInput(v), null);
    assert.equal(formatMailDate(v), '');
  }
  assert.equal(formatMailDateOrRaw('not a date'), 'not a date');
});

test('THE REPORTED CASE renders 1:20:03 PM, not 2:20:03 AM', () => {
  // 18:20:03Z is 1:20:03 PM EST on 11/9/2026.
  assert.equal(
    formatMailDate('2026-11-09T18:20:03Z', { mode: 'datetime', ...NY }),
    '11/9/2026, 1:20:03 PM');
  // The naive variant (legacy stored value, same instant) must agree.
  assert.equal(
    formatMailDate('2026-11-09 18:20:03', { mode: 'datetime', ...NY }),
    '11/9/2026, 1:20:03 PM');
  assert.notEqual(
    formatMailDate('2026-11-09T18:20:03Z', { mode: 'datetime', ...NY }),
    '11/9/2026, 2:20:03 AM');
});

test('list / header / date / time modes', () => {
  const v = '2026-11-09T18:20:03Z';
  assert.equal(formatMailDate(v, { mode: 'list', ...NY }), '11/9/2026, 1:20 PM');
  assert.equal(formatMailDate(v, { mode: 'header', ...NY }), 'Monday, November 9, 2026 at 1:20 PM');
  assert.equal(formatMailDate(v, { mode: 'date', ...NY }), '11/9/2026');
  assert.equal(formatMailDate(v, { mode: 'time', ...NY }), '1:20 PM');
  assert.equal(formatMailDate(v, { mode: 'iso', ...NY }), '2026-11-09T18:20:03.000Z');
});

test('the same instant renders differently in another timezone', () => {
  const v = '2026-11-09T18:20:03Z';
  assert.equal(formatMailDate(v, { mode: 'datetime', timezone: 'UTC' }), '11/9/2026, 6:20:03 PM');
  assert.equal(formatMailDate(v, { mode: 'datetime', timezone: 'America/Los_Angeles' }),
    '11/9/2026, 10:20:03 AM');
});

test('DST: spring forward', () => {
  // 2026-03-08 02:00 EST -> 03:00 EDT.
  assert.equal(formatMailDate('2026-03-08T06:59:59Z', { mode: 'datetime', ...NY }),
    '3/8/2026, 1:59:59 AM');
  assert.equal(formatMailDate('2026-03-08T07:00:00Z', { mode: 'datetime', ...NY }),
    '3/8/2026, 3:00:00 AM');
});

test('DST: fall back (the repeated hour)', () => {
  assert.equal(formatMailDate('2026-11-01T05:30:00Z', { mode: 'datetime', ...NY }),
    '11/1/2026, 1:30:00 AM');
  assert.equal(formatMailDate('2026-11-01T06:30:00Z', { mode: 'datetime', ...NY }),
    '11/1/2026, 1:30:00 AM');
  // ...and the hour after the second 1:30 is 2:30, not 1:30.
  assert.equal(formatMailDate('2026-11-01T07:30:00Z', { mode: 'datetime', ...NY }),
    '11/1/2026, 2:30:00 AM');
});

test('an invalid timezone falls back to UTC instead of browser-local', () => {
  assert.equal(
    formatMailDate('2026-11-09T18:20:03Z', { mode: 'datetime', timezone: 'Not/AZone' }),
    '11/9/2026, 6:20:03 PM');
});

test('the reported case is stable across the year boundary', () => {
  assert.equal(formatMailDate('2026-12-31T23:59:59Z', { mode: 'datetime', ...NY }),
    '12/31/2026, 6:59:59 PM');
  assert.equal(formatMailDate('2027-01-01T04:59:59Z', { mode: 'datetime', ...NY }),
    '12/31/2026, 11:59:59 PM');
});
