#!/usr/bin/env node
// Regression test for the "schedule fields greyed out" bug.
//
// Audit 2026-10-15: the previous version of loadBackupHistory() in
// public/app.js disabled every input/button inside the
// #backup-schedule-fs fieldset when status.configured was false. The
// "Not configured yet" notice was the only path to creating the
// initial config, but the Save button itself was disabled — so the
// user could see the notice but couldn't actually save. This test
// guards against the regression by asserting the fieldset is always
// interactive (no setAttribute("disabled", "") on its child inputs
// or buttons).
//
// Run with: node --test tests/test_backup_schedule_editable.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');

// Locate the loadBackupHistory function and the save handler.
const loadFnStart = appJs.indexOf('async function loadBackupHistory(');
const loadFnEnd = loadFnStart > 0
  ? appJs.indexOf('\n  } catch', loadFnStart) + 5
  : -1;
assert.ok(loadFnStart > 0, 'loadBackupHistory not found');
const loadFn = appJs.slice(loadFnStart, loadFnEnd);

const saveStart = appJs.indexOf('$("#backup-config-save")?.addEventListener("click"');
const saveEnd = saveStart > 0
  ? appJs.indexOf('\n    });', saveStart) + 6
  : -1;
assert.ok(saveStart > 0, 'backup config save handler not found');
const saveFn = appJs.slice(saveStart, saveEnd);

test('Audit 2026-10-15: loadBackupHistory does not disable form inputs in the unconfigured state', () => {
  // The fix removed the `else` branch that called
  // setAttribute("disabled", "") on every child input/button. Assert
  // no such call remains in the function.
  assert.ok(
    !/setAttribute\(["']disabled["']/.test(loadFn),
    'loadBackupHistory still calls setAttribute("disabled", ...) — the form cannot be saved when no config exists yet'
  );
  // Sanity: the function should still set the notice visibility based
  // on status.configured.
  assert.ok(
    /notCfg\.style\.display/.test(loadFn),
    'loadBackupHistory should still toggle the "Not configured yet" notice visibility'
  );
});

test('Audit 2026-10-15: loadBackupHistory keeps the form fully interactive', () => {
  // The fieldset is always opacity:1, with removeAttribute("disabled")
  // on every child input/button. This matches the fixed behavior.
  assert.ok(
    /fs\.style\.opacity\s*=\s*["']1["']/.test(loadFn),
    'loadBackupHistory should set fs.style.opacity = 1 (form always visible)'
  );
  assert.ok(
    /removeAttribute\(["']disabled["']/.test(loadFn),
    'loadBackupHistory should removeAttribute("disabled") on form children so they are always editable'
  );
});

test('Audit 2026-10-15: save handler also re-enables child inputs after a successful save', () => {
  // After clicking Save, the fieldset opacity goes to 1 and any leftover
  // disabled attributes are removed. The previous version only set
  // opacity on the fieldset itself, leaving the child inputs disabled.
  assert.ok(
    /removeAttribute\(["']disabled["']/.test(saveFn),
    'save handler should removeAttribute("disabled") on form children after a successful save'
  );
  assert.ok(
    /fs\.style\.opacity\s*=\s*["']1["']/.test(saveFn),
    'save handler should set fs.style.opacity = 1 (form always visible)'
  );
});
