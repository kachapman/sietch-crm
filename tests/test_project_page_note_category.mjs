// Tests for the project-page note composer category (public/project.html).
//
// Live bug 2026-11-01: the project-page composer hardcoded categoryId "10"
// ("Files are uploaded", a non-deletable system type) and offered no category
// picker, so every plain note filed there was mislabelled and permanent.
// The composer now renders a note-type <select> populated from user-note
// categories only (mirroring isUserNoteCategory in app.js) and posts the
// selected id.
//
// Run with:  node --test tests/test_project_page_note_category.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, '..', 'public', 'project.html'), 'utf8');

test('project page composer has a note-type picker', () => {
  assert.ok(src.includes('id="phtml-note-category"'), 'missing #phtml-note-category select');
  assert.ok(src.includes('<select'), 'no select element in project.html');
});

test('project page no longer hardcodes the system file-upload category', () => {
  assert.ok(!src.includes('categoryId: "10"'), 'hardcoded categoryId "10" still present');
  assert.ok(!src.includes("categoryId: '10'"), "hardcoded categoryId '10' still present");
});

test('project page filters system categories by flag, not numeric id', () => {
  assert.ok(src.includes('function isPhtmlUserNoteCategory'), 'predicate missing');
  assert.ok(src.includes('function resolvePhtmlNoteCategoryId'), 'resolver missing');
  assert.ok(src.includes('function populatePhtmlNoteCategorySelect'), 'populator missing');
  assert.ok(src.includes('/api/v2/history-categories'), 'categories are never fetched');
  assert.ok(src.includes('isSystem'), 'filter does not key off the isSystem flag');
});

test('project page submit aborts instead of filing under a fallback id', () => {
  assert.ok(
    src.includes('Note categories unavailable'),
    'no abort path when categories fail to load',
  );
});
