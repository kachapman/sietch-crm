#!/usr/bin/env node
// Regression tests for the timesheet audit round 3 fixes (2026-10-15).
//
// Audit findings (from the doc audit + bug reports) that this file guards:
//   2. _handle_timesheet_clock_out silently consumed any in-progress break
//      and closed the shift. Now rejects with 400 when break_start is set.
//   3. /api/timesheet/{history,daily,export,export-pdf} silently coerced
//      a non-admin's cross-user request to the caller's own data. Now
//      returns 403 "Admin required to view/export other users' timesheets".
//   4. The right-click context menu in the timesheet modal allowed
//      deleting the active shift (clock_out IS NULL). Now disabled with
//      a tooltip explaining why.
//   5. tsEditEntry / tsAssignProject attached a document-level click
//      listener for backdrop dismissal that was only removed on the
//      backdrop path — not on Save/Cancel/Escape. Now every close path
//      goes through a single teardown() that removes the listener.
//
// Run with: node --test tests/test_timesheet_audit_round3.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const serverPy = readFileSync(join(repoRoot, 'server.py'), 'utf8');

// ─── Item 2: clock-out while on break returns 400 ─────────────────────────

test('Audit 2026-10-15 (item 2): _handle_timesheet_clock_out rejects when break_start is set', () => {
  const fnStart = serverPy.indexOf('def _handle_timesheet_clock_out(self) -> None:');
  assert.ok(fnStart > 0, '_handle_timesheet_clock_out not found');
  // Find the next top-level def to bound the function body.
  const fnEnd = serverPy.indexOf('\n    def _handle_timesheet_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);

  // Must NOT contain the old "extra_break" path that silently added
  // in-progress break minutes to break_minutes and closed the shift.
  assert.ok(
    !/extra_break\s*=/.test(fn),
    '_handle_timesheet_clock_out still has the "extra_break" silent-coerce path'
  );
  // Must contain the new 400 rejection with a clear error message.
  assert.ok(
    /break_start[\s\S]{0,200}?End your break before clocking out/.test(fn),
    '_handle_timesheet_clock_out should return 400 "End your break before clocking out" when break_start is set'
  );
});

// ─── Item 3: non-admin cross-user requests return 403 on 4 endpoints ─────

test('Audit 2026-10-15 (item 3): history/daily/export/export-pdf reject non-admin cross-user with 403', () => {
  for (const fnName of [
    '_handle_timesheet_history',
    '_handle_timesheet_daily',
    '_handle_timesheet_export_csv',
    '_handle_timesheet_export_pdf',
  ]) {
    const fnStart = serverPy.indexOf(`def ${fnName}(`);
    assert.ok(fnStart > 0, `${fnName} not found`);
    const fnEnd = serverPy.indexOf('\n    def _handle_timesheet_', fnStart + 1);
    const fn = serverPy.slice(fnStart, fnEnd);

    // The old "target_user = user['id']" silent-coerce pattern must be gone.
    assert.ok(
      !/target_user\s*=\s*user\[["']id["']\]/.test(fn),
      `${fnName} still silently coerces target_user to caller's id`
    );
    // Each handler must explicitly reject with 403 for non-admin
    // cross-user access. The 4 handlers share a near-identical
    // guard block; check for the keyword from the new guard.
    assert.ok(
      /Admin required to (view|export) other users' timesheets/.test(fn),
      `${fnName} should return 403 "Admin required to ..." for non-admin cross-user requests`
    );
  }
});

// ─── Item 4: context menu disables Delete on the active shift ─────────────

test('Audit 2026-10-15 (item 4): tsEntryContextMenu disables Delete when clock_out is null', () => {
  const fnStart = appJs.indexOf('function tsEntryContextMenu(');
  assert.ok(fnStart > 0, 'tsEntryContextMenu not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);

  // The fix detects the active shift by entry.clock_out.
  assert.ok(
    /!entry\.clock_out/.test(fn),
    'tsEntryContextMenu should detect the active shift via !entry.clock_out'
  );
  // Delete must be marked disabled with a tooltip.
  assert.ok(
    /Clock out before deleting this entry/.test(fn),
    'tsEntryContextMenu should show a "Clock out before deleting this entry" tooltip on the disabled Delete item'
  );
  // The Delete item in the items array must include a `disabled` field
  // driven by isActive.
  assert.ok(
    /label:\s*["']Delete["'][\s\S]{0,200}disabled:\s*isActive/.test(fn),
    'tsEntryContextMenu Delete item should set disabled: isActive'
  );
});

// ─── Item 5: tsEditEntry / tsAssignProject teardown removes listener ──────

test('Audit 2026-10-15 (item 5a): tsEditEntry uses a single teardown() to remove the document click listener', () => {
  const fnStart = appJs.indexOf('function tsEditEntry(');
  assert.ok(fnStart > 0, 'tsEditEntry not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);

  // There must be a teardown() that removes the document click listener.
  assert.ok(
    /const\s+teardown\s*=\s*\(\)\s*=>\s*\{[\s\S]*?document\.removeEventListener\(["']click["'],\s*closeBackdropListener\)/.test(fn),
    'tsEditEntry teardown should document.removeEventListener("click", closeBackdropListener)'
  );
  // Both save and cancel must call teardown (not just popup.remove()).
  // cancel is a one-liner arrow: `const cancel = () => teardown();`
  // save has braces because it also calls the API.
  const saveBlock = fn.match(/const\s+save\s*=\s*\(\)\s*=>\s*\{[\s\S]*?\}\s*;/);
  const cancelLine = fn.match(/const\s+cancel\s*=\s*\(\)\s*=>\s*[^;]+;/);
  assert.ok(saveBlock && /teardown\s*\(/.test(saveBlock[0]), 'tsEditEntry save() must call teardown()');
  assert.ok(cancelLine && /teardown\s*\(/.test(cancelLine[0]), 'tsEditEntry cancel() must call teardown()');
});

test('Audit 2026-10-15 (item 5b): tsAssignProject uses a single teardown() to remove the document click listener', () => {
  const fnStart = appJs.indexOf('function tsAssignProject(');
  assert.ok(fnStart > 0, 'tsAssignProject not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);

  // Same teardown pattern as tsEditEntry.
  assert.ok(
    /const\s+teardown\s*=\s*\(\)\s*=>\s*\{[\s\S]*?document\.removeEventListener\(["']click["'],\s*closeBackdropListener\)/.test(fn),
    'tsAssignProject teardown should document.removeEventListener("click", closeBackdropListener)'
  );
  // Project-pick click must also call teardown (not just popup.remove()).
  const pickBlock = fn.match(/btn\.addEventListener\(["']click["'],\s*\(\)\s*=>\s*\{[\s\S]*?\}\);/);
  assert.ok(pickBlock && /teardown\s*\(/.test(pickBlock[0]), 'tsAssignProject project-pick click must call teardown()');
  // Escape keypress should also tear down (no orphan listener).
  assert.ok(
    /keydown[\s\S]{0,100}Escape[\s\S]{0,100}teardown\s*\(/.test(fn),
    'tsAssignProject should call teardown() on Escape keypress'
  );
  // The old anonymous `function close(ev) { ... }` pattern that leaked
  // listeners must be gone.
  assert.ok(
    !/function\s+close\s*\(\s*ev\s*\)\s*\{[\s\S]*?document\.removeEventListener\(["']click["'],\s*close\)/.test(fn),
    'tsAssignProject still has the old anonymous function close(ev) leak pattern'
  );
});
