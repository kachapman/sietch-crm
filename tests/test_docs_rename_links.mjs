// Tests for project-tab Documents Rename + history editor-first links.
//
// - Rename: the shared project-tab Actions… menu (regular/search/bookmark,
//   one builder) gains a single-selection Rename that PATCHes and refreshes;
//   multi-select refuses loudly.
// - History attachments: rows WITH a project_documents twin link to
//   /doc-editor.html?id=… like the Documents tab (which falls back to
//   download for unsupported types); rows without keep direct download.
//
// Run with:  node --test tests/test_docs_rename_links.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function appSrc() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
}

test('project-tab Actions menu offers Rename (photos menu untouched)', () => {
  const src = appSrc();
  const docsMenu = src.indexOf('opp-docs-actions-select');
  assert.ok(docsMenu >= 0, 'docs Actions select missing');
  assert.ok(src.slice(docsMenu, docsMenu + 600).includes('value="rename"'), 'Rename option missing in docs menu');
  const photosMenu = src.indexOf('opp-photos-actions-select');
  assert.ok(photosMenu >= 0, 'photos Actions select missing');
  assert.ok(!src.slice(photosMenu, photosMenu + 600).includes('value="rename"'), 'Rename must not leak into the photos menu');
});

test('rename handler is single-selection with loud multi-refuse', () => {
  const src = appSrc();
  const at = src.indexOf('if (action === "rename")', src.indexOf('opp-docs-actions-select'));
  assert.ok(at >= 0, 'rename branch missing in project-tab handler');
  const region = src.slice(at, at + 900);
  assert.ok(region.includes('ids.length !== 1'), 'must gate on single selection');
  assert.ok(region.includes('startPreviewDocRename'), 'must enter inline rename');
});

test('inline rename commits via PATCH and refreshes', () => {
  const src = appSrc();
  const at = src.indexOf('async function commitPreviewDocRename');
  assert.ok(at >= 0, 'commit helper missing');
  const region = src.slice(at, at + 2600);
  assert.ok(region.includes('method: "PATCH"'), 'must PATCH the document');
  assert.ok(region.includes('JSON.stringify({ title })'), 'must send the title');
  assert.ok(region.includes('refreshDocs()'), 'must refresh the list');
  // Escape-cancel lives on the rename input's keydown, ~7KB above the commit
  // helper (same render closure) — assert in a wide window, not the helper.
  assert.ok(src.slice(at - 8000, at + 2600).includes('ev.key === "Escape"'), 'Escape must cancel (input keydown)');
});

test('history extractor carries the project-documents twin id', () => {
  const src = appSrc();
  const at = src.indexOf('function extractHistoryAttachments');
  assert.ok(at >= 0, 'extractor missing');
  const region = src.slice(at, at + 4000);
  assert.ok(region.includes('projectDocumentsId'), 'twin id must be propagated');
});

test('history aside prefers the editor when a twin exists', () => {
  const src = appSrc();
  const at = src.indexOf('function renderHistoryAttachmentsAside');
  assert.ok(at >= 0, 'aside renderer missing');
  const region = src.slice(at, at + 2500);
  assert.ok(region.includes('/doc-editor.html?id='), 'editor-first link missing');
  assert.ok(region.includes('a.download = filename'), 'download fallback must remain');
});
