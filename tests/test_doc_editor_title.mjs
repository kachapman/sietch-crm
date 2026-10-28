// Tests for the document editor page: title + rename stickiness
// (public/doc-editor.html).
//
// Live bug 2026-10-27: a Sietch-side rename stuck, then "went back" after the
// file was opened in the editor. Cause: onMetaChange PATCHed the title from
// OnlyOffice's session broadcast — the session key is stable per document
// (sietch-doc-{id}), so a session opened before the rename re-broadcasts the
// OLD title and overwrote the rename. Fix: the DB title is authoritative;
// broadcasts are ignored, explicit renames still sync, and the page title
// shows the file name.
//
// Run with:  node --test tests/test_doc_editor_title.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function editorSrc() {
  const here = path.dirname(fileURLToPath(import.meta.url));
  return fs.readFileSync(path.join(here, '..', 'public', 'doc-editor.html'), 'utf8');
}

function block(src, startMarker, endMarker) {
  const start = src.indexOf(startMarker);
  assert.ok(start >= 0, `${startMarker} missing`);
  const end = src.indexOf(endMarker, start);
  assert.ok(end > start, `${endMarker} missing after ${startMarker}`);
  return src.slice(start, end);
}

test('onMetaChange does not PATCH the title (broadcast must not revert renames)', () => {
  const src = editorSrc();
  const region = block(src, 'config.events.onMetaChange', 'config.events.onDocumentReady');
  assert.ok(!region.includes('method: "PATCH"'), 'meta broadcast must not PATCH the title');
  assert.ok(!region.includes('/api/v2/documents/'), 'meta broadcast must not touch the document API');
});

test('explicit rename inside OnlyOffice still syncs (onRequestRename PATCHes)', () => {
  const src = editorSrc();
  const region = block(src, 'config.events.onRequestRename', 'config.events.onRequestSaveAs');
  assert.ok(region.includes('method: "PATCH"'), 'explicit rename must PATCH the title');
  assert.ok(region.includes('c: "meta"'), 'explicit rename must also update the session');
});

test('page title shows the file name from the editor config', () => {
  const src = editorSrc();
  assert.ok(src.includes('document.title = fileName'), 'tab title must be set from the file name');
  assert.ok(src.includes('config.document && config.document.title'), 'file name must come from config.document.title');
});

test('session is re-aligned with our title on document ready', () => {
  const src = editorSrc();
  const region = block(src, 'config.events.onDocumentReady', 'config.events.onRequestRename');
  assert.ok(region.includes('/command'), 'must push the title into the live session');
  assert.ok(region.includes('c: "meta"'), 'must use the meta command');
});

test('embedded previews still hide the page chrome', () => {
  const src = editorSrc();
  assert.ok(src.includes('data-embedded'), 'embedded mode must be preserved');
});
