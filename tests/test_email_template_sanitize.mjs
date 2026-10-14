#!/usr/bin/env node
// Unit tests for TEMPLATE_AUDIT #1 — template/signature HTML sanitization
// in the compose modal.
//
// The actual sanitizer (sanitizeHistoryHtml, app.js:24449) is the
// existing function used by the email preview iframe (already
// exercised in production). What this audit changed is the *call
// sites* — every place where tpl.body_html or signature_html is
// assigned to innerHTML must now go through sanitizeHistoryHtml.
//
// So these tests are source-grep regression tests: they read
// public/app.js and verify the four injection sites route through
// the sanitizer. The "no remaining raw ... innerHTML" tests
// guarantee the fix doesn't regress in future refactors.
//
// Run with: node --test tests/test_email_template_sanitize.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const appJs = readFileSync(
  new URL('../public/app.js', import.meta.url),
  'utf8'
);

// Helper: scan app.js line-by-line and find lines that match a pattern.
function findLines(predicate) {
  return appJs.split('\n')
    .map((line, i) => ({ line, n: i + 1 }))
    .filter(({ line }) => predicate(line));
}

// --- The four injection sites the audit identified ---

test('site 1: openComposeModal(opts) sanitizes opts.body', () => {
  const re = /bodyDiv\.innerHTML\s*=\s*sanitizeHistoryHtml\(opts\.body\)/;
  assert.ok(re.test(appJs),
    'openComposeModal opts.body must go through sanitizeHistoryHtml');
});

test('site 2: openComposeModal signature_html is sanitized', () => {
  const re = /sanitizeHistoryHtml\(sig\.signature_html\)/;
  assert.ok(re.test(appJs),
    'openComposeModal signature_html must be sanitized');
});

test('site 3: template picker click sanitizes tpl.body_html', () => {
  const re = /sanitizeHistoryHtml\(tpl\.body_html\)/;
  assert.ok(re.test(appJs),
    'template picker tpl.body_html must be sanitized');
});

test('site 4: from-account signature swap sanitizes signature_html', () => {
  // The from-account change handler should compute a safeSig and use it.
  const re = /safeSig\s*=\s*sanitizeHistoryHtml\(sig\.signature_html\)/;
  assert.ok(re.test(appJs),
    'from-account signature swap must sanitize sig.signature_html');
});

// --- Negative tests: no remaining raw injection paths ---

test('no remaining raw tpl.body_html into innerHTML', () => {
  // Look for lines that:
  //  - contain .innerHTML
  //  - reference body_html
  //  - do NOT contain sanitizeHistoryHtml( or escapeHtml(
  //  - are not comments
  // (textarea.value is fine — text-only, not innerHTML)
  const hits = findLines((line) => (
    line.includes('.innerHTML') &&
    line.includes('body_html') &&
    !line.includes('sanitizeHistoryHtml(') &&
    !line.includes('escapeHtml(') &&
    !line.trim().startsWith('//') &&
    !line.trim().startsWith('*') &&
    !line.trim().startsWith('/*')
  ));
  assert.equal(hits.length, 0,
    'Found raw body_html innerHTML assignments: ' +
    JSON.stringify(hits.map((h) => `line ${h.n}: ${h.line.trim()}`)));
});

test('no remaining raw signature_html into innerHTML', () => {
  const hits = findLines((line) => (
    line.includes('.innerHTML') &&
    line.includes('signature_html') &&
    !line.includes('sanitizeHistoryHtml(') &&
    !line.includes('safeSig') &&
    !line.trim().startsWith('//') &&
    !line.trim().startsWith('*') &&
    !line.trim().startsWith('/*')
  ));
  assert.equal(hits.length, 0,
    'Found raw signature_html innerHTML assignments: ' +
    JSON.stringify(hits.map((h) => `line ${h.n}: ${h.line.trim()}`)));
});

// --- Sanity: the audit markers are in the source ---

test('audit #1 markers are present in the source', () => {
  // The fix added comments referencing TEMPLATE_AUDIT #1 at the
  // call sites. If those comments are gone, the fix is probably
  // being reworked and the audit should be re-verified.
  // (Site 1 covers two injection points — opts.body and signature_html
  // in openComposeModal — under a single block comment, so 3 markers
  // is the correct count for 4 distinct sanitization sites.)
  const markers = (appJs.match(/TEMPLATE_AUDIT #1/g) || []).length;
  assert.ok(markers >= 3,
    `Expected at least 3 TEMPLATE_AUDIT #1 markers, found ${markers}`);
});

