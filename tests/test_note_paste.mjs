// Guards for the note-editor paste sanitizer (initNoteEditorPasteHandlers).
//
// History: pasted highlight survived in non-mark/span wrappers and resurfaced
// green on backspace-merge; the wrapper-div insertion forced pastes onto a
// new line. The sanitizer is now allowlist-based and inserts children, not
// the wrapper. Interactive editor behavior can't execute under node, so these
// guards pin the structure; manual browser verification still applies (paste
// rich text, confirm inline landing, backspace-merge, inspect for mark /
// background styles before saving).
//
// Run with: node --test tests/test_note_paste.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const appJs = readFileSync(join(root, 'public', 'app.js'), 'utf8');

function pasteRegion() {
  const at = appJs.indexOf('function initNoteEditorPasteHandlers');
  assert.ok(at > 0);
  return appJs.slice(at, at + 6000);
}

test('paste sanitizer is allowlist-based (mark/span/font cannot survive)', () => {
  const region = pasteRegion();
  assert.ok(region.includes('PASTE_ALLOW'), 'allowlist set missing');
  // The allowlist itself must not contain highlight-capable wrappers.
  const allow = region.match(/PASTE_ALLOW = new Set\(\[(.*?)\]\)/s);
  assert.ok(allow, 'PASTE_ALLOW set literal missing');
  for (const bad of ['mark', 'span', 'font', 'div', 'p', 'table', 'td']) {
    assert.ok(!allow[1].split(',').map(s => s.trim().replace(/['"]/g, '')).includes(bad),
      `${bad} must not be in the allowlist`);
  }
});

test('block elements become line breaks (div/p/li covered)', () => {
  const region = pasteRegion();
  for (const tag of ['"div"', '"p"', '"li"']) {
    assert.ok(region.includes(tag), `${tag} must be in PASTE_BLOCKS`);
  }
});

test('insertion spreads children, never the wrapper div', () => {
  const region = pasteRegion();
  assert.ok(region.includes('for (const kid of kids)'), 'must insert children one by one');
  assert.ok(!region.includes('range.insertNode(frag)'), 'wrapper-div insert forces a newline');
});

test('event-handler attributes stripped from pasted HTML', () => {
  const region = pasteRegion();
  assert.ok(region.includes('startsWith("on")'), 'on* stripping missing');
});

test('script/style dropped with content', () => {
  const region = pasteRegion();
  assert.ok(region.includes('"script, style"'), 'script/style removal missing');
});
