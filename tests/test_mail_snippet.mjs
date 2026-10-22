// Tests for the mail list snippet helper (public/app.js#mailSnippetText).
//
// Live bug 2026-10-22: list rows showed literal "&nbsp;&nbsp;" (Allstate rows)
// because the snippet path stripped tags but never decoded entities; rows with
// empty body_text showed no preview at all even though body_html was present.
//
// Run with:  node --test tests/test_mail_snippet.mjs
//
// The helper below mirrors public/app.js#mailSnippetText (decodeHtmlEntities
// replaced with a minimal equivalent: the entity set exercised here). If you
// change one, change the other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function decodeHtmlEntities(text) {
  return String(text)
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#0*39;/g, "'");
}

function mailSnippetText(m) {
  if (!m) return '';
  let raw = m.body_text || m.snippet || '';
  if (!String(raw).trim() && m.body_html) {
    raw = String(m.body_html).replace(/<[^>]+>/g, ' ');
  }
  let text = String(raw).replace(/<[^>]+>/g, ' ');
  try { text = decodeHtmlEntities(text); } catch { /* keep raw */ }
  return text.replace(/ /g, ' ').replace(/\s+/g, ' ').replace(/\s+([,.;:!?])/g, '$1').trim();
}

test('Allstate-style entities decode instead of displaying', () => {
  const out = mailSnippetText({ body_text: 'Allstate 96 &nbsp;&nbsp;|&nbsp;&nbsp; (https://' });
  assert.ok(!out.includes('&nbsp'), out);
  assert.ok(out.startsWith('Allstate 96'), out);
});

test('html-only message recovers preview from body_html', () => {
  const out = mailSnippetText({ body_text: '', body_html: '<div>Hello <b>MDM</b>, supplement received.</div>' });
  assert.equal(out, 'Hello MDM, supplement received.');
});

test('empty both stays empty (genuinely bodiless, scanner gap)', () => {
  assert.equal(mailSnippetText({ body_text: '', body_html: '' }), '');
  assert.equal(mailSnippetText(null), '');
});

test('snippet path in app.js routes through the helper + decoder', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  assert.ok(src.includes('function mailSnippetText'), 'helper missing in app.js');
  assert.ok(!src.includes("(m.body_text || '').replace(/<[^>]+>/g, '')"), 'old tag-strip-only path still present');
  const at = src.indexOf('function mailSnippetText');
  assert.ok(src.slice(at, at + 900).includes('decodeHtmlEntities'), 'helper must decode entities');
});
