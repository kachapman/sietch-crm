// Tests for the forward/reply quote builder + CID inline-image upgrade
// (public/app.js#mailQuotedBody + renderEmailPreviewBody).
//
// Live bugs: (1) 2026-10-21 forward showed raw HTML tags — every branch wrapped
// the original in escapeHtml; (2) 2026-10-21 inline images broken — cid: URLs
// pointed at the authed endpoint from a sandboxed opaque-origin iframe (401).
// Fix: mode-aware quoting (HTML sanitized, text escaped) + fetch-with-auth
// rewritten to data: URLs.
//
// Run with:  node --test tests/test_mail_forward_quote.mjs
//
// Mirrors mailQuotedBody (escapeHtml/sanitizeHistoryHtml replaced with minimal
// equivalents sufficient for these assertions). If you change one, change the
// other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function sanitizeHistoryHtml(html) {
  return String(html).replace(/<script[\s\S]*?<\/script>/gi, '');
}
function mailQuotedBody(bodyPick) {
  if (bodyPick && bodyPick.mode === 'html' && bodyPick.content) {
    try { return sanitizeHistoryHtml(bodyPick.content); } catch { /* fall through */ }
  }
  const text = bodyPick?.content || '';
  return text ? escapeHtml(text).replace(/\r?\n/g, '<br>') : '';
}

test('html original keeps markup, strips scripts', () => {
  const out = mailQuotedBody({ mode: 'html', content: '<p>Hi</p><script>alert(1)</script>' });
  assert.ok(out.includes('<p>Hi</p>'), out);
  assert.ok(!out.includes('<script'), out);
  assert.ok(!out.includes('&lt;p&gt;'), out);
});

test('text original is escaped with line breaks', () => {
  const out = mailQuotedBody({ mode: 'text', content: 'a<b\nline2' });
  assert.equal(out, 'a&lt;b<br>line2');
});

test('empty pick yields empty quote', () => {
  assert.equal(mailQuotedBody({ mode: 'empty', content: '' }), '');
  assert.equal(mailQuotedBody(null), '');
});

test('app.js compose path no longer escapes the whole original', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  assert.ok(src.includes('function mailQuotedBody'), 'helper missing in app.js');
  const at = src.indexOf('function bindEmailPreviewActionButtons');
  const region = src.slice(at, at + 4500);
  assert.ok(!region.includes('${escapeHtml(bodyText)}'), 'compose quote still escapes raw original');
  assert.ok(region.includes('${quoted}'), 'compose quote must use mode-aware quoted body');
});

test('cid upgrade fetches with auth and embeds data urls', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  const at = src.indexOf('function renderEmailPreviewBody');
  const region = src.slice(at, at + 6000);
  assert.ok(region.includes('readAsDataURL'), 'must embed fetched bytes as data: URLs');
  assert.ok(region.includes("credentials: 'same-origin'"), 'fetch must carry the session cookie');
  assert.ok(region.includes('isConnected'), 'must not rewrite a stale iframe');
  assert.ok(!region.includes('return `${quote}/api/v2/mail/messages/'), 'direct cid->endpoint rewrite must be gone');
});
