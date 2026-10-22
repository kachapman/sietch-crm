// Tests for project-ownership preservation (2026-10-22).
//
// buildOpportunityPutBody resolved the owner as
// opp.responsible?.id ?? opp.responsible?.ID ?? state.currentUserId — but the
// project GET nests responsible as {displayName} with NO id, while the flat
// responsibleUserId the server DOES send was never consulted. Every PUT by
// anyone restamped the deal to themselves. The builder now honors the flat
// keys first; currentUserId is a last resort only.
//
// Run with:  node --test tests/test_responsible_keep.mjs
//
// The resolver below mirrors public/app.js#buildOpportunityPutBody's owner
// line. If you change one, change the other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function resolveResponsible(opp, currentUserId) {
  return opp.responsible?.id ?? opp.responsible?.ID ?? opp.responsibleUserId
    ?? opp.responsibleId ?? opp.responsibleid ?? opp.responsibleID ?? currentUserId;
}

test('flat responsibleUserId survives an edit by someone else', () => {
  const opp = { responsible: { displayName: 'Ken' }, responsibleUserId: 5 };
  assert.equal(resolveResponsible(opp, 7), 5);
});

test('legacy nested id still wins when present', () => {
  const opp = { responsible: { id: 5 }, responsibleUserId: 9 };
  assert.equal(resolveResponsible(opp, 7), 5);
});

test('unknown owner falls back to current user (create-safe)', () => {
  assert.equal(resolveResponsible({}, 7), 7);
});

test('app.js builder honors the flat keys', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  const at = src.indexOf('function buildOpportunityPutBody');
  const region = src.slice(at, at + 800);
  for (const key of ['opp.responsibleUserId', 'opp.responsibleId']) {
    assert.ok(region.includes(key), `builder must consult ${key}`);
  }
});

test('deal-edit modal exposes an editable Responsible field', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const idx = fs.readFileSync(path.join(here, '..', 'public', 'index.html'), 'utf8');
  assert.ok(idx.includes('id="deal-edit-responsible"'), 'picker missing from edit modal');
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  assert.ok(src.includes('deal-edit-responsible'), 'picker never populated/wired');
  assert.ok(src.includes('initialResponsibleId'), 'initial owner not tracked for change detection');
});
