#!/usr/bin/env node
// Regression tests for the timesheet-overdue notification (round 4 part 2).
//
// Audit 2026-10-15 round 4: when a user has been continuously clocked
// in for 10+ hours, send them a Telegram reminder AND surface the same
// message in the CRM notification drawer. The user chose:
//   - 10h once + 14h reminder (two notifications, different thresholds)
//   - no per-type opt-out (existing global Telegram opt-out still respected)
//   - 60s polling interval
//   - INSERT IGNORE-style unique guard (one row per (user, entry, threshold))
//
// The notification row is INSERTed by timesheet_overdue_checker.py; the
// existing notification_dispatcher ships it through Telegram and the
// existing drawer code surfaces it in the bell-icon drawer. This test
// guards the structure of all three layers.
//
// Run with: node --test tests/test_timesheet_overdue_notifications.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const serverPy = readFileSync(join(repoRoot, 'server.py'), 'utf8');
const dispatcherPy = readFileSync(join(repoRoot, 'notification_dispatcher.py'), 'utf8');
const checkerPy = readFileSync(join(repoRoot, 'timesheet_overdue_checker.py'), 'utf8');
const migration = readFileSync(join(repoRoot, 'migrations/032_timesheet_overdue_unique_index.sql'), 'utf8');

test('Audit 2026-10-15 (overdue): migration 032 creates a unique index on (user, type, entry, threshold)', () => {
  // The unique index uses JSONB expressions so the same row can be
  // de-duplicated by the (entry_id, threshold_hours) pair.
  assert.ok(
    /CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+uq_notifications_timesheet_overdue/.test(migration),
    'Migration 032 should CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_timesheet_overdue'
  );
  // The index should be partial (only on timesheet_overdue rows) so
  // other notification types aren't constrained.
  assert.ok(
    /WHERE\s+type\s*=\s*'timesheet_overdue'/.test(migration),
    'Migration 032 unique index should be partial on type=timesheet_overdue'
  );
  // The index columns should include (user_id, type, payload->>'entry_id', payload->>'threshold_hours').
  assert.ok(
    /ON\s+notifications[\s\S]{0,200}?user_id[\s\S]{0,200}?type[\s\S]{0,400}?payload->>'entry_id'[\s\S]{0,200}?payload->>'threshold_hours'/.test(migration),
    'Migration 032 index should include (user_id, type, payload->>entry_id, payload->>threshold_hours)'
  );
});

test('Audit 2026-10-15 (overdue): timesheet_overdue_checker.py polls with INSERT ON CONFLICT DO NOTHING', () => {
  // The checker must use ON CONFLICT DO NOTHING for idempotency.
  assert.ok(
    /INSERT\s+INTO\s+notifications[\s\S]{0,500}?ON\s+CONFLICT\s+DO\s+NOTHING/.test(checkerPy),
    'timesheet_overdue_checker should use INSERT ... ON CONFLICT DO NOTHING for idempotency'
  );
  // The checker must be a background thread with a 60s poll interval
  // (matching the user-pinned design decision).
  assert.ok(
    /POLL_INTERVAL_SEC\s*=\s*60/.test(checkerPy),
    'timesheet_overdue_checker should poll every 60 seconds'
  );
  // The thresholds must be (10, 14) per the user-pinned design.
  assert.ok(
    /THRESHOLDS_HOURS\s*=\s*\(\s*10\s*,\s*14\s*\)/.test(checkerPy),
    'timesheet_overdue_checker should use thresholds (10, 14)'
  );
  // The INSERT must include the right payload fields so the dispatcher
  // and drawer have what they need.
  assert.ok(
    /jsonb_build_object\(\s*'entry_id'[\s\S]{0,200}?'threshold_hours'[\s\S]{0,200}?'elapsed'[\s\S]{0,200}?'snippet'/.test(checkerPy),
    'timesheet_overdue_checker INSERT payload should include entry_id, threshold_hours, elapsed, and snippet'
  );
  // The headline ("message" column) and snippet ("payload.snippet") should
  // be different (per the round 4 design: headline = time, snippet = question).
  // The Python source uses double-quoted strings.
  assert.ok(
    /Did you forget to log out/.test(checkerPy) && /Please verify your timesheet/.test(checkerPy),
    'timesheet_overdue_checker should set the snippet to the question (10h = "Did you forget to log out?", 14h = "Please verify your timesheet.")'
  );
  // start_checker / stop_checker must exist for server.py to wire up.
  assert.ok(/def\s+start_checker/.test(checkerPy) && /def\s+stop_checker/.test(checkerPy),
    'timesheet_overdue_checker should expose start_checker() and stop_checker() for server.py');
});

test('Audit 2026-10-15 (overdue): server.py starts and stops the checker', () => {
  // Server should import and start the checker.
  assert.ok(
    /from\s+timesheet_overdue_checker\s+import\s+start_checker/.test(serverPy),
    'server.py should import start_checker from timesheet_overdue_checker'
  );
  assert.ok(
    /ts_overdue_stop\s*=\s*start_ts_overdue_checker\(\)/.test(serverPy) ||
      /ts_overdue_stop\s*=\s*start_checker\(/.test(serverPy),
    'server.py should call start_checker (or start_ts_overdue_checker) at boot'
  );
  // Server should include the checker in the subsystem status dict.
  assert.ok(
    /["']ts_overdue["']\s*:\s*["']ok["']\s+if\s+ts_overdue_stop/.test(serverPy),
    'server.py should include ts_overdue in the READY subsystem status dict'
  );
  // Server should stop the checker on shutdown.
  assert.ok(
    /stop_ts_overdue_checker\(ts_overdue_stop\)/.test(serverPy) ||
      /stop_checker\(ts_overdue_stop\)/.test(serverPy),
    'server.py should call stop_checker (or stop_ts_overdue_checker) on shutdown'
  );
});

test('Audit 2026-10-15 (overdue): notification_dispatcher.py has a timesheet_overdue formatter', () => {
  // The Telegram formatter branch.
  assert.ok(
    /elif\s+ntype\s*==\s*["']timesheet_overdue["']\s*:[\s\S]{0,50}?_format_timesheet_overdue/.test(dispatcherPy),
    '_format_notification should route timesheet_overdue to _format_timesheet_overdue'
  );
  // The formatter must exist and call _build_tg.
  const fnStart = dispatcherPy.indexOf('def _format_timesheet_overdue(');
  assert.ok(fnStart > 0, '_format_timesheet_overdue not defined');
  const fnEnd = dispatcherPy.indexOf('\n\ndef ', fnStart + 1);
  const fn = dispatcherPy.slice(fnStart, fnEnd);
  assert.ok(/_build_tg\(/.test(fn), '_format_timesheet_overdue should use the _build_tg helper for Telegram formatting');
  // Both thresholds should be distinguishable in the formatter.
  assert.ok(
    /threshold\s*>=\s*14/.test(fn) && /threshold\s*==\s*10|Forgotten clock-out\?/.test(fn),
    '_format_timesheet_overdue should differentiate the 10h and 14h messages'
  );
  // The email formatter should also handle the new type.
  assert.ok(
    /elif\s+ntype\s*==\s*["']timesheet_overdue["'][\s\S]{0,500}?subject/.test(dispatcherPy),
    'The email formatter branch should handle timesheet_overdue'
  );
});

test('Audit 2026-10-15 (overdue): drawer renders the alarm SVG icon and opens the modal on click', () => {
  // The icon function must include timesheet_overdue with an alarm icon
  // (the user explicitly chose the Tabler alarm SVG for the visual identity).
  const fnStart = appJs.indexOf('function _notifTypeIcon(');
  assert.ok(fnStart > 0, '_notifTypeIcon not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /type\s*===\s*["']timesheet_overdue["']/.test(fn),
    '_notifTypeIcon should have a branch for timesheet_overdue'
  );
  // The icon must use the alarm SVG (the user explicitly chose the
  // Tabler alarm icon for visual consistency).
  assert.ok(
    /icon-tabler-alarm/.test(fn) || /M5 13a7 7 0 1 0 14 0a7 7 0 1 0 -14 0/.test(fn),
    '_notifTypeIcon should render the Tabler alarm SVG for timesheet_overdue'
  );
  // The click handler should open the activity modal.
  const renderStart = appJs.indexOf('function _renderNotificationDrawerItem(');
  assert.ok(renderStart > 0, '_renderNotificationDrawerItem not found');
  const renderEnd = appJs.indexOf('\nfunction ', renderStart + 1);
  const renderFn = appJs.slice(renderStart, renderEnd);
  assert.ok(
    /n\.type\s*===\s*["']timesheet_overdue["'][\s\S]{0,200}?openActivityModal\(\)/.test(renderFn),
    '_renderNotificationDrawerItem should open the activity modal when a timesheet_overdue notification is clicked'
  );
});
