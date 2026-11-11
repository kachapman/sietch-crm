// Guards for the notification-drawer type icons (live bug 2026-11-10).
//
// Mail-account notifications (mail_auth_error) and task/calendar reminders
// fell through to the generic fallback in `_notifTypeIcon`, whose path data
// was malformed (disconnected strokes) — the drawer showed a broken glyph.
// The clock-in alarm (timesheet_overdue) was already correct and must stay.
//
// Run with: node --test tests/test_notification_icons.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const appJs = readFileSync(join(root, 'public', 'app.js'), 'utf8');

function notifIconSource() {
  const start = appJs.indexOf('function _notifTypeIcon(');
  assert.ok(start > 0, '_notifTypeIcon missing');
  const end = appJs.indexOf('\nfunction _renderNotificationDrawerItem(', start);
  assert.ok(end > start, 'could not delimit _notifTypeIcon');
  return appJs.slice(start, end);
}

const src = notifIconSource();

// Every branch returns an inline SVG in the same 24x24 viewBox.
function assertSvg(fragment, label) {
  assert.match(fragment, /viewBox="0 0 24 24"/, `${label}: viewBox missing`);
  assert.match(fragment, /stroke="currentColor"/, `${label}: stroke missing`);
  assert.match(fragment, /<path stroke="none" d="M0 0h24v24H0z" fill="none"\/>/, `${label}: base path missing`);
}

test('malformed fallback path data is gone', () => {
  // The old default icon drew these disconnected strokes (broken glyph).
  assert.ok(!src.includes('M8 9l3 3l-2 1'), 'old broken fallback path still present');
  assert.ok(!src.includes('M10.5 17.5l-3.5 -2l-2 1'), 'old broken fallback path still present');
});

test('mail-account notifications use a real mail-exclamation icon', () => {
  const i = src.indexOf('type === "mail_auth_error"');
  assert.ok(i > 0, 'mail_auth_error branch missing');
  const branch = src.slice(i, i + 900);
  assertSvg(branch, 'mail_auth_error');
  assert.ok(branch.includes('M12 19h-7a2 2 0 0 1 -2 -2v-10'), 'mail body path missing');
  assert.ok(branch.includes('M3 7l9 6l9 -6'), 'envelope flap path missing');
  assert.ok(branch.includes('M19 22v.01'), 'exclamation dot path missing');
});

test('task reminders use a real bell icon', () => {
  const i = src.indexOf('type === "task_reminder"');
  assert.ok(i > 0, 'task_reminder branch missing');
  const branch = src.slice(i, i + 900);
  assertSvg(branch, 'task_reminder');
  assert.ok(branch.includes('M10 5a2 2 0 1 1 4 0'), 'bell dome path missing');
  assert.ok(branch.includes('M9 17v1a3 3 0 0 0 6 0v-1'), 'bell clapper path missing');
});

test('calendar reminders use a real calendar-event icon', () => {
  const i = src.indexOf('type === "calendar_reminder"');
  assert.ok(i > 0, 'calendar_reminder branch missing');
  const branch = src.slice(i, i + 900);
  assertSvg(branch, 'calendar_reminder');
  assert.ok(branch.includes('M16 3l0 4'), 'calendar pin path missing');
  assert.ok(branch.includes('M8 15h2v2h-2z'), 'calendar event box path missing');
});

test('clock-in (timesheet_overdue) alarm icon is unchanged', () => {
  const i = src.indexOf('type === "timesheet_overdue"');
  assert.ok(i > 0, 'timesheet_overdue branch missing');
  const branch = src.slice(i, i + 900);
  assert.ok(branch.includes('M5 13a7 7 0 1 0 14 0'), 'alarm clock path missing');
  assert.ok(branch.includes('icon-tabler-alarm'), 'alarm class missing');
  assert.ok(branch.includes('stroke-width="1.25"'), 'alarm weight changed');
});

test('unknown types still render a valid bell instead of nothing', () => {
  const tail = src.slice(src.lastIndexOf('return `<svg'));
  assertSvg(tail, 'default');
  assert.ok(tail.includes('M10 5a2 2 0 1 1 4 0'), 'default must be a real icon');
});
