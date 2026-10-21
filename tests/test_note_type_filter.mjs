// Tests for the note-type dropdown filter (public/app.js#isUserNoteCategory).
//
// Live bug 2026-10-21: the dropdown excluded hardcoded numeric category IDs.
// On prod, ID 12 = "Phone call", so the option vanished (while rows the list
// meant to hide showed up, since IDs drift between environments). The filter
// now keys off the isSystem flag the API sends.
//
// Run with:  node --test tests/test_note_type_filter.mjs
//
// The predicate below mirrors public/app.js#isUserNoteCategory. If you change
// one, change the other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function isUserNoteCategory(cat) {
  if (!cat) return false;
  const title = String(cat.title || cat.Title || '');
  if (title === 'Action') return false;
  if (cat.isSystem === true || cat.is_system === true) return false;
  return true;
}

// Prod-like rows with deliberately shuffled IDs: the filter must not depend
// on any numeric id.
const CATS = [
  { id: 101, title: 'Note', isSystem: false },
  { id: 12, title: 'Phone call', isSystem: false },
  { id: 7, title: 'Mail Message', isSystem: true },
  { id: 77, title: 'Files are uploaded', isSystem: true },
  { id: 3, title: 'Task is closed', isSystem: true },
  { id: 55, title: 'Stage Estimate', isSystem: true },
  { id: 56, title: 'Opportunity', isSystem: true },
  { id: 25, title: 'Action' }, // synthetic fallback row carries no flag
  { id: 13, title: 'Email', isSystem: false },
  { id: 17, title: 'Quick Context', isSystem: false },
];

test('phone call is selectable regardless of numeric id', () => {
  assert.equal(isUserNoteCategory({ id: 12, title: 'Phone call', isSystem: false }), true);
  assert.equal(isUserNoteCategory({ id: 999, title: 'Phone call', isSystem: false }), true);
});

test('system rows are excluded regardless of numeric id', () => {
  for (const c of CATS.filter(c => c.isSystem)) {
    assert.equal(isUserNoteCategory(c), false, c.title);
  }
});

test('synthetic Action fallback is excluded by title', () => {
  assert.equal(isUserNoteCategory({ id: 25, title: 'Action' }), false);
});

test('user-facing rows stay visible', () => {
  const visible = CATS.filter(isUserNoteCategory).map(c => c.title).sort();
  assert.deepEqual(visible, ['Email', 'Note', 'Phone call', 'Quick Context']);
});

test('app.js no longer hardcodes category ids for the dropdown', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  assert.ok(!src.includes('excludedIds'), 'stale excludedIds set still present');
  assert.ok(src.includes('function isUserNoteCategory'), 'predicate missing in app.js');
});
