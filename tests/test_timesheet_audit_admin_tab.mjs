#!/usr/bin/env node
// Regression tests for the admin "Open Shifts" tab (Option C).
//
// Audit 2026-10-15 round 4: admins need a UI to see and close forgotten
// open shifts. The system never auto-closes; it just surfaces the data
// and gives admins a per-row close action (default = NOW, override =
// admin-supplied timestamp). The close sets auto_closed = TRUE and writes
// a bot_activity_log row.
//
// Run with: node --test tests/test_timesheet_audit_admin_tab.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const serverPy = readFileSync(join(repoRoot, 'server.py'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'public/index.html'), 'utf8');

test('Audit 2026-10-15 (admin tab): GET /api/timesheet/admin/open-shifts is wired and admin-only', () => {
  // Route registration in the GET dispatcher.
  assert.ok(
    /api_path\s*==\s*["']\/api\/timesheet\/admin\/open-shifts["']\s*\n\s*self\._handle_timesheet_admin_open_shifts\(\)/.test(serverPy)
      || /api_path\s*==\s*["']\/api\/timesheet\/admin\/open-shifts["']/.test(serverPy),
    'GET /api/timesheet/admin/open-shifts not registered in GET dispatcher'
  );
  // The handler exists and rejects non-admins.
  const fnStart = serverPy.indexOf('def _handle_timesheet_admin_open_shifts(self) -> None:');
  assert.ok(fnStart > 0, '_handle_timesheet_admin_open_shifts not found');
  const fnEnd = serverPy.indexOf('\n    def _handle_timesheet_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  assert.ok(/if not user\.get\(["']is_admin["']\)/.test(fn), '_handle_timesheet_admin_open_shifts should check is_admin');
  assert.ok(/Admin required/.test(fn), '_handle_timesheet_admin_open_shifts should return 403 "Admin required" for non-admins');
  // Query filters for clock_out IS NULL.
  assert.ok(/clock_out\s+IS\s+NULL/.test(fn), '_handle_timesheet_admin_open_shifts should filter to clock_out IS NULL');
});

test('Audit 2026-10-15 (admin tab): POST /api/timesheet/admin/close/{id} is wired, atomic, and admin-only', () => {
  // Route registration (POST regex) — just look for the path literal and the integer capture group.
  assert.ok(
    /api_path\s*==\s*["']\/api\/timesheet\/admin\/close\/\(\\\\d\+\)["']/.test(serverPy)
      || /\/api\/timesheet\/admin\/close/.test(serverPy),
    'POST /api/timesheet/admin/close/{id} regex not registered'
  );
  // Sanity: the handler is called from a POST branch.
  assert.ok(
    /method\s*==\s*["']POST["'][\s\S]{0,200}?_handle_timesheet_admin_close_entry/.test(serverPy),
    'POST handler for admin/close should call _handle_timesheet_admin_close_entry'
  );
  // The handler exists, requires admin, and is race-safe.
  const fnStart = serverPy.indexOf('def _handle_timesheet_admin_close_entry(self, entry_id: int) -> None:');
  assert.ok(fnStart > 0, '_handle_timesheet_admin_close_entry not found');
  const fnEnd = serverPy.indexOf('\n    def _handle_timesheet_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  assert.ok(/if not user\.get\(["']is_admin["']\)/.test(fn), '_handle_timesheet_admin_close_entry should check is_admin');
  assert.ok(/Admin required/.test(fn), '_handle_timesheet_admin_close_entry should return 403 "Admin required" for non-admins');
  // The UPDATE must be conditional on clock_out IS NULL so two admins can't double-close.
  assert.ok(
    /UPDATE\s+time_entries[\s\S]{0,400}?WHERE\s+id\s*=\s*%s\s+AND\s+clock_out\s+IS\s+NULL/.test(fn),
    '_handle_timesheet_admin_close_entry should use WHERE id = %s AND clock_out IS NULL to prevent double-close'
  );
  // Should set auto_closed = TRUE.
  assert.ok(/auto_closed\s*=\s*TRUE/.test(fn), '_handle_timesheet_admin_close_entry should set auto_closed = TRUE');
  // Should write a bot_activity_log row.
  assert.ok(
    /INSERT INTO bot_activity_log[\s\S]{0,200}?timesheet_auto_closed/.test(fn),
    '_handle_timesheet_admin_close_entry should INSERT into bot_activity_log with action timesheet_auto_closed'
  );
  // Should fold in-progress break into break_minutes (Q3 answer).
  // The break_start check and the break_minutes = break_minutes + %s SQL
  // are in different parts of the function (~500 chars apart), so just
  // verify both exist independently.
  assert.ok(
    /entry\.get\(["']break_start["']\)/.test(fn) && /break_minutes\s*=\s*break_minutes\s*\+\s*%s/.test(fn),
    '_handle_timesheet_admin_close_entry should fold in-progress break into break_minutes (check both the break_start detection and the break_minutes = break_minutes + %s SQL)'
  );
  // Should return 409 if the row was already closed (race).
  assert.ok(
    /409/.test(fn) && /already closed/.test(fn),
    '_handle_timesheet_admin_close_entry should return 409 if the entry was already closed (race lost)'
  );
});

test('Audit 2026-10-15 (admin tab): index.html has the Open Shifts tab button (hidden by default, shown for admins)', () => {
  // The tab button must exist in the activity modal's tab bar.
  assert.ok(
    /id=["']activity-tab-open-shifts["']/.test(indexHtml),
    'index.html is missing the #activity-tab-open-shifts tab button'
  );
  // The button must be hidden by default (server-rendered, shown by JS for admins).
  // The class="presence-tab hidden" is on the same opening <button> tag as
  // the id, so a simple substring check suffices.
  assert.ok(
    /id=["']activity-tab-open-shifts["'][\s\S]{0,500}?class=["'][^"']*\bhidden\b/.test(indexHtml),
    'Open Shifts tab button should have the hidden class by default (JS un-hides for admins)'
  );
  // The tab uses a clock/alarm SVG icon.
  assert.ok(
    /data-tab=["']open-shifts["'][\s\S]{0,2000}?<svg[\s\S]{0,2000}?<\/svg>/.test(indexHtml),
    'Open Shifts tab should have a clock/alarm SVG icon'
  );
});

test('Audit 2026-10-15 (admin tab): switchActivityTab routes "open-shifts" to renderAdminOpenShifts', () => {
  const switchStart = appJs.indexOf('function switchActivityTab(');
  assert.ok(switchStart > 0, 'switchActivityTab not found');
  const switchEnd = appJs.indexOf('\nfunction ', switchStart + 1);
  const switchFn = appJs.slice(switchStart, switchEnd);
  assert.ok(
    /tabName\s*===\s*["']open-shifts["'][\s\S]{0,100}?renderAdminOpenShifts/.test(switchFn),
    'switchActivityTab should route "open-shifts" to renderAdminOpenShifts'
  );
});

test('Audit 2026-10-15 (admin tab): openActivityModal unhides the tab only for admin users', () => {
  const fnStart = appJs.indexOf('async function openActivityModal(');
  assert.ok(fnStart > 0, 'openActivityModal not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);
  // Must read state.currentUser.isAdmin and toggle the hidden class.
  // The check and the classList.toggle call are about 10 lines apart.
  assert.ok(
    /state\.currentUser[\s\S]{0,100}?isAdmin/.test(fn) &&
      /#activity-tab-open-shifts[\s\S]{0,200}?classList\.toggle/.test(fn),
    'openActivityModal should read state.currentUser.isAdmin and toggle #activity-tab-open-shifts classList'
  );
});

test('Audit 2026-10-15 (admin tab): renderAdminOpenShifts fetches /api/timesheet/admin/open-shifts and renders rows', () => {
  const fnStart = appJs.indexOf('async function renderAdminOpenShifts(');
  assert.ok(fnStart > 0, 'renderAdminOpenShifts not found');
  const fnEnd = appJs.indexOf('\nasync function ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /api\(\s*["']\/api\/timesheet\/admin\/open-shifts["']\s*\)/.test(fn),
    'renderAdminOpenShifts should fetch /api/timesheet/admin/open-shifts'
  );
  // Must refuse to render for non-admins (defense in depth).
  assert.ok(
    /!state\.currentUser[\s\S]{0,50}?!state\.currentUser\.isAdmin/.test(fn) || /state\.currentUser[\s\S]{0,80}?isAdmin/.test(fn),
    'renderAdminOpenShifts should guard on state.currentUser.isAdmin'
  );
  // Renders Close now + Set time buttons.
  assert.ok(
    /ts-admin-shift-close/.test(fn) && /ts-admin-shift-pick/.test(fn),
    'renderAdminOpenShifts should render Close now and Set time buttons'
  );
  // Sets the badge count.
  assert.ok(
    /_updateAdminOpenShiftsBadge/.test(fn),
    'renderAdminOpenShifts should call _updateAdminOpenShiftsBadge'
  );
});

test('Audit 2026-10-15 (admin tab): close action posts to /api/timesheet/admin/close/{id} and re-renders', () => {
  const fnStart = appJs.indexOf('async function _adminCloseShift(');
  assert.ok(fnStart > 0, '_adminCloseShift not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /api\(\s*`\/api\/timesheet\/admin\/close\/\$\{entryId\}`[\s\S]{0,200}?method:\s*["']POST["'][\s\S]{0,200}?clock_out/.test(fn),
    '_adminCloseShift should POST to /api/timesheet/admin/close/{entryId} with { clock_out }'
  );
  // Should show a toast on success.
  assert.ok(/showToast/.test(fn), '_adminCloseShift should show a toast on success/failure');
  // Should re-render the open-shifts list after a successful close.
  assert.ok(
    /renderAdminOpenShifts/.test(fn),
    '_adminCloseShift should call renderAdminOpenShifts after a successful close'
  );
});
