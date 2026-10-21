// Regression tests for Create Project save crash (2026-10-21).
//
// `submitCreateOpportunityForm` called `unwrapCreatedEntity(data)` after the
// create POST, but the helper was deleted in ae31547 (tasks refactor) — every
// save threw ReferenceError AFTER the server-side insert (ghost duplicates on
// retry). The helper is restored; these tests pin it and guard every unwrap*
// call site against future orphaning.
//
// Run with:  node --test tests/test_create_opp_unwrap.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Mirror of public/app.js#unwrapCreatedEntity. If you change one, change the other.
function unwrapCreatedEntity(data) {
  const r = data?.response ?? data?.result ?? data;
  if (r && typeof r === 'object') return r;
  return data;
}

test('unwraps {response:{id}} envelope', () => {
  assert.equal(unwrapCreatedEntity({ response: { id: 42 } }).id, 42);
});

test('unwraps {result:{id}} envelope', () => {
  assert.equal(unwrapCreatedEntity({ result: { ID: 7 } }).ID, 7);
});

test('passes through bare entity and non-objects', () => {
  assert.equal(unwrapCreatedEntity({ id: 9 }).id, 9);
  assert.equal(unwrapCreatedEntity(null), null);
  assert.equal(unwrapCreatedEntity(5), 5);
});

test('every unwrap* call in app.js has a definition (no orphaned helpers)', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|\s)\/\/.*$/gm, '$1');
  const calls = new Set([...code.matchAll(/(?<![\w$.])((?:unwrap)[A-Za-z]*)\s*\(/g)].map(m => m[1]));
  const defs = new Set([...code.matchAll(/function\s+((?:unwrap)[A-Za-z]*)\s*\(/g)].map(m => m[1]));
  const orphaned = [...calls].filter(n => !defs.has(n));
  assert.deepEqual(orphaned, [], `orphaned unwrap helpers called but never defined: ${orphaned}`);
  assert.ok(defs.has('unwrapCreatedEntity'), 'unwrapCreatedEntity definition missing');
});
