// Tests for the team-tile custom status fix (2026-10-22).
//
// Clicking Custom... in the Team tile status dropdown threw
// `ReferenceError: customInput is not defined` (the name only exists in the
// unrelated bindPresenceTileControls) and killed the dashboard via the global
// boot-error page. The ✓/✕ buttons were also unwired in this builder.
//
// Run with:  node --test tests/test_presence_custom_status.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function tileRegion() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  const at = src.indexOf('function renderPresenceTile(');
  assert.ok(at >= 0, 'renderPresenceTile missing');
  const end = src.indexOf('\nfunction ', at + 10);
  return src.slice(at, end < 0 ? undefined : end);
}

test('no undeclared customInput reference in renderPresenceTile', () => {
  const region = tileRegion();
  assert.ok(!/\bcustomInput\b/.test(region) || region.includes('const customInputEl') || region.includes('querySelector("#presence-tile-custom-input")'),
    'bare customInput must be resolved from its wrapper');
  assert.ok(!/[^A-Za-z$_.]customInput\.focus\(\)/.test(region), 'unscoped customInput.focus() still present');
});

test('custom ok/cancel/enter/escape are all wired', () => {
  const region = tileRegion();
  assert.ok(region.includes('#presence-tile-custom-ok'), 'ok button not queried');
  assert.ok(region.includes('#presence-tile-custom-cancel'), 'cancel button not queried');
  assert.ok(region.includes('submitTileCustomStatus') || region.includes('"Enter"') || region.includes("'Enter'"), 'Enter-to-submit not wired');
});

test('custom submit posts a manual (non-inferred) status', () => {
  const region = tileRegion();
  assert.ok(region.includes('inferred: false'), 'custom status must suppress auto-status');
});

test('global boot page cannot be tripped by an unscoped DOM name here', () => {
  const region = tileRegion();
  const bareRefs = [...region.matchAll(/(?<![A-Za-z_$#."'`])customInput(?![A-Za-z_$])/g)];
  assert.deepEqual(bareRefs.map(m => m[0]), [], 'bare customInput references remain');
});
