// Guards for the Tabler avatar/tab icon pool.
//
// History: the pool carried 29 names absent from the loaded webfont (v3
// renames/removals under a floating @latest URL), and the runtime validator
// measured element WIDTH — which cannot distinguish a real glyph from a
// .notdef fallback box. Result: blank selectable icons and blank avatars.
// Fixed by: pinned font version, audited pool, ::before-content probing.
//
// Run with: node --test tests/test_tabler_icons.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const appJs = readFileSync(join(root, 'public', 'app.js'), 'utf8');
const html = readFileSync(join(root, 'public', 'index.html'), 'utf8');
const snapshot = readFileSync(join(root, 'tests', 'tabler_pool_snapshot.txt'), 'utf8');

function poolNames() {
  const m = appJs.match(/const TABLER_ICON_OPTIONS = \[(.*?)\];/s);
  assert.ok(m, 'TABLER_ICON_OPTIONS not found');
  return [...new Set([...m[1].matchAll(/'([^']+)'/g)].map(x => x[1]))].sort();
}

test('pool matches the audited snapshot exactly', () => {
  // Any add/remove/rename fails here until re-verified against the pinned
  // font CSS — that friction is the point (see test header).
  assert.deepEqual(poolNames(), snapshot.trim().split('\n'));
});

test('pool has no duplicates', () => {
  const m = appJs.match(/const TABLER_ICON_OPTIONS = \[(.*?)\];/s);
  const names = [...m[1].matchAll(/'([^']+)'/g)].map(x => x[1]);
  assert.equal(names.length, new Set(names).size);
});

test('webfont version is pinned, never floating', () => {
  assert.ok(!html.includes('@tabler/icons-webfont@latest'),
    'floating @latest reintroduces silent renames');
  assert.ok(html.includes('@tabler/icons-webfont@3.49.0'),
    'expected pinned 3.49.0 stylesheet');
});

test('validator probes ::before content, not width', () => {
  const at = appJs.indexOf('function _getValidTablerIconSet');
  assert.ok(at > 0);
  const region = appJs.slice(at, at + 2500);
  assert.ok(region.includes('getComputedStyle'), 'must probe computed style');
  assert.ok(region.includes('::before'), 'must probe the glyph pseudo-element');
  assert.ok(!region.includes('getBoundingClientRect'),
    'width measurement cannot detect missing glyphs');
});

test('saved-but-missing icons fall back instead of blanking', () => {
  // Avatar render paths must consult the valid set before emitting <i>.
  assert.ok(appJs.includes('_getValidTablerIconSet().has('),
    'avatar render must gate on the validated set');
});
