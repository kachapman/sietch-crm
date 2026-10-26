// Tests for the note-editor paste gate + read-only field cursor
// (public/app.js#resolveNoteEditorFromEventTarget, styles.css).
//
// Live bugs 2026-10-26:
// 1. Paste sanitization was bypassed for nearly every paste into non-empty
//    content: paste events target the deepest node under the caret (a
//    <div>/<b>/<span> INSIDE the editor), but the gate required e.target
//    itself to carry .note-editor. Raw clipboard HTML (source background
//    styles) was inserted natively; the highlight resurfaced on
//    backspace-merge. The gate must resolve via closest().
// 2. Read-only Details fields showed the text-entry cursor. Static
//    dt/dd must use the default cursor (links keep pointer).
//
// Run with:  node --test tests/test_note_paste_target.mjs
//
// The resolver below mirrors public/app.js#resolveNoteEditorFromEventTarget.
// If you change one, change the other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function resolveNoteEditorFromEventTarget(target) {
  if (!target) return null;
  if (target.classList && target.classList.contains('note-editor')) return target;
  if (typeof target.closest === 'function') return target.closest('.note-editor');
  return null;
}

function stubTarget({ isEditor = false, editorAncestor = null } = {}) {
  return {
    classList: { contains: (c) => isEditor && c === 'note-editor' },
    closest: (sel) => (sel === '.note-editor' ? editorAncestor : null),
  };
}

test('paste into a child node still resolves the editor', () => {
  const editor = { id: 'editor' };
  const child = stubTarget({ editorAncestor: editor });
  assert.equal(resolveNoteEditorFromEventTarget(child), editor);
});

test('paste directly on the editor resolves itself', () => {
  const editor = stubTarget({ isEditor: true, editorAncestor: null });
  assert.equal(resolveNoteEditorFromEventTarget(editor), editor);
});

test('paste outside any editor resolves null', () => {
  assert.equal(resolveNoteEditorFromEventTarget(stubTarget({})), null);
  assert.equal(resolveNoteEditorFromEventTarget(null), null);
  assert.equal(resolveNoteEditorFromEventTarget({}), null);
});

test('app.js paste gate uses the resolver, not a direct class check', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  const at = src.indexOf('function initNoteEditorPasteHandlers');
  assert.ok(at >= 0, 'paste handler missing');
  const region = src.slice(at, at + 800);
  assert.ok(region.includes('resolveNoteEditorFromEventTarget'), 'gate must use the resolver');
  assert.ok(!region.includes('target.classList.contains("note-editor")'), 'direct e.target class gate must be gone');
});

test('read-only preview fields use the default cursor', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const css = fs.readFileSync(path.join(here, '..', 'public', 'styles.css'), 'utf8');
  const at = css.indexOf('.opp-preview-field dd {');
  assert.ok(at >= 0, 'preview field rule missing');
  const region = css.slice(at, at + 1200);
  assert.ok(region.includes('cursor: default'), 'read-only fields must not show a text cursor');
});
