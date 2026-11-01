// Date-only parsing fix (bug doc docs/bugs/2026-11-01-follow-up-due-date-off-by-one.md).
//
// "YYYY-MM-DD" strings from the API (PG DATE columns) must render as local
// calendar dates, never as UTC midnight (which shows the previous day in US
// timezones). Full datetimes keep native parsing.
//
// Run with: TZ=America/Chicago node --test tests/test_date_only.mjs
//           TZ=America/New_York node --test tests/test_date_only.mjs
// The suite must pass under both (and UTC).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const projectHtml = readFileSync(join(repoRoot, 'public/project.html'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'public/index.html'), 'utf8');
const dateOnlySrc = readFileSync(join(repoRoot, 'public/date-only.js'), 'utf8');

// Load the REAL shared helper (no mirror drift possible).
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(dateOnlySrc + '\n;globalThis.__t = { parseDateSmart, toDateInputValue };', sandbox);
const { parseDateSmart, toDateInputValue } = sandbox.__t;

// Project-page formatter shape (project.html#formatDate after the fix).
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function formatDate(s) {
  if (!s) return "";
  const d = parseDateSmart(s);
  if (!d || Number.isNaN(d.getTime())) return String(s);
  return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
}

// ── The reported case: project #30, stored "2026-10-26" must show Oct 26 ──
test('reported case: "2026-10-26" formats as "Oct 26, 2026"', () => {
  assert.equal(formatDate("2026-10-26"), "Oct 26, 2026");
});

test('date-only parses to local calendar fields, not UTC midnight', () => {
  const d = parseDateSmart("2026-10-26");
  assert.equal(d.getFullYear(), 2026);
  assert.equal(d.getMonth(), 9);
  assert.equal(d.getDate(), 26);
});

// ── Month and year boundaries ──
for (const [raw, label] of [
  ["2026-01-01", "Jan 1, 2026"],
  ["2026-12-31", "Dec 31, 2026"],
  ["2024-02-29", "Feb 29, 2024"],
  ["2026-03-01", "Mar 1, 2026"],
  ["2027-01-01", "Jan 1, 2027"],
]) {
  test(`boundary: "${raw}" formats as "${label}"`, () => {
    assert.equal(formatDate(raw), label);
  });
}

// ── DST-boundary sweep (US fall-back 2026-11-01): every day renders itself ──
test('DST sweep Oct 25–Nov 2 2026: each date-only renders its own day', () => {
  for (let day = 25; day <= 31; day++) {
    const d = parseDateSmart(`2026-10-${day}`);
    assert.equal(d.getDate(), day, `2026-10-${day} shifted`);
  }
  for (let day = 1; day <= 2; day++) {
    const d = parseDateSmart(`2026-11-0${day}`);
    assert.equal(d.getDate(), day, `2026-11-0${day} shifted`);
  }
});

// ── Datetime regression: real times keep native behavior ──
test('datetime regression: "2026-10-26T14:00:00Z" keeps its time component', () => {
  const d = parseDateSmart("2026-10-26T14:00:00Z");
  assert.equal(d.toISOString(), "2026-10-26T14:00:00.000Z");
});

test('datetime regression: offset datetimes parse natively', () => {
  const d = parseDateSmart("2026-10-26T09:30:00-05:00");
  assert.equal(d.toISOString(), "2026-10-26T14:30:00.000Z");
});

// ── <input type="date"> round-trip ──
test('date-only round-trips through <input type="date"> value', () => {
  assert.equal(toDateInputValue(parseDateSmart("2026-10-26")), "2026-10-26");
  assert.equal(toDateInputValue(parseDateSmart("2026-01-01")), "2026-01-01");
  assert.equal(toDateInputValue(parseDateSmart("2026-12-31")), "2026-12-31");
});

test('null/empty/invalid handling', () => {
  assert.equal(parseDateSmart(null), null);
  assert.equal(parseDateSmart(""), null);
  assert.equal(parseDateSmart(undefined), null);
  assert.ok(Number.isNaN(parseDateSmart("not-a-date").getTime()));
  assert.equal(toDateInputValue(null), "");
  assert.equal(formatDate(""), "");
});

// ── Source guards: the 7 fixed places use the shared helper ──
test('project.html formatDate branches through parseDateSmart', () => {
  assert.ok(projectHtml.includes('parseDateSmart(s)'), 'formatDate must use parseDateSmart');
  assert.ok(!/var d = new Date\(s\)/.test(projectHtml), 'bare new Date(s) must be gone from project.html');
});

test('project.html and index.html load the shared date-only.js', () => {
  assert.ok(projectHtml.includes('date-only.js'), 'project.html must load date-only.js');
  assert.ok(indexHtml.includes('date-only.js'), 'index.html must load date-only.js before app.js');
  assert.ok(indexHtml.indexOf('date-only.js') < indexHtml.indexOf('src="app.js'), 'date-only.js must precede app.js');
});

test('local-kanban display paths use parseDateSmart', () => {
  assert.ok(appJs.includes('parseDateSmart(card.due).toLocaleDateString()'), ':10850 card due display');
  assert.ok(appJs.includes('const dueDate = parseDateSmart(due);'), 'getCardDueInfo day-diff math');
});

test('task display paths use parseDateSmart', () => {
  assert.ok(appJs.includes('const dueParsed = parseDateSmart(task.dueDate);'), ':41934 task due label');
  assert.ok(
    appJs.includes('parseDateSmart(task.dueDate || task.deadLine?.value || task.deadLine).toLocaleDateString()'),
    ':42173 task due label'
  );
  assert.ok(appJs.includes('const t = parseDateSmart(due).getTime();'), 'taskSortMs');
  assert.ok(appJs.includes('const d = parseDateSmart(due);'), 'task tooltip');
  assert.ok(appJs.includes('const due = parseDateSmart(deadline);'), 'isTaskOverdue');
});

test('task date-input prefill round-trips date-only verbatim (no UTC shift)', () => {
  assert.ok(
    appJs.includes('if (/^\\d{4}-\\d{2}-\\d{2}$/.test(String(iso).trim())) return String(iso).trim();'),
    'toLocalDate must pass date-only values straight into <input type="date">'
  );
});

test('no remaining bare new Date() on due-date inputs', () => {
  assert.ok(!/new Date\((card\.due|due|task\.dueDate|deadline)\)/.test(appJs),
    'every due-date parse must go through parseDateSmart');
});
