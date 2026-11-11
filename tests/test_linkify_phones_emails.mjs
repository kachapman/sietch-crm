// Tests: phone/email linkifier must not treat ISO dates, US dates, or
// "from X to Y" ranges as tel: links, while still linking real phone numbers.
//
// Ken (2026-11-10): compact history lines like
// "Follow-up Due Date changed from 2026-11-09 to 2026-11-12" were
// underlined as clickable phone numbers.
//
// Run with:  node --test tests/test_linkify_phones_emails.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  isDateLikeNumber,
  phoneMatches,
  linkifyPhonesAndEmailsText,
} from '../public/linkify-phones-emails.js';

function telCount(html) {
  return (html.match(/href="tel:/g) || []).length;
}

function telHrefs(html) {
  return [...html.matchAll(/href="tel:([^"]*)"/g)].map((m) => m[1]);
}

// ── date detection ────────────────────────────────────────────────────────

test('isDateLikeNumber accepts ISO YYYY-MM-DD', () => {
  assert.equal(isDateLikeNumber('2026-11-09'), true);
  assert.equal(isDateLikeNumber('2026-11-12'), true);
  assert.equal(isDateLikeNumber(' 2026-01-01 '), true);
});

test('isDateLikeNumber accepts US MM/DD/YYYY and dashed/dotted variants', () => {
  assert.equal(isDateLikeNumber('11/09/2026'), true);
  assert.equal(isDateLikeNumber('11/9/2026'), true);
  assert.equal(isDateLikeNumber('1/9/2026'), true);
  assert.equal(isDateLikeNumber('11-09-2026'), true);
  assert.equal(isDateLikeNumber('11.09.2026'), true);
  assert.equal(isDateLikeNumber('11/09/26'), true);
});

test('isDateLikeNumber rejects real phone numbers and plain text', () => {
  assert.equal(isDateLikeNumber('555-123-4567'), false);
  assert.equal(isDateLikeNumber('(555) 123-4567'), false);
  assert.equal(isDateLikeNumber('5551234567'), false);
  assert.equal(isDateLikeNumber('from 2026-11-09 to 2026-11-12'), false);
  assert.equal(isDateLikeNumber(''), false);
});

// ── dates are not phones ──────────────────────────────────────────────────

test('ISO dates YYYY-MM-DD are not phone-linked', () => {
  assert.deepEqual(phoneMatches('2026-11-09'), []);
  const html = linkifyPhonesAndEmailsText('2026-11-09');
  assert.equal(telCount(html), 0);
  assert.ok(html.includes('2026-11-09'));
});

test('US dates MM/DD/YYYY are not phone-linked', () => {
  assert.deepEqual(phoneMatches('11/09/2026'), []);
  assert.deepEqual(phoneMatches('11-09-2026'), []);
  assert.equal(telCount(linkifyPhonesAndEmailsText('11/09/2026')), 0);
  assert.equal(telCount(linkifyPhonesAndEmailsText('11-09-2026')), 0);
});

test('from X to Y date ranges are not phone-linked', () => {
  const iso = 'Follow-up Due Date changed from 2026-11-09 to 2026-11-12';
  const us = 'Follow-up Due Date changed from 11/09/2026 to 11/12/2026';
  const dashed = 'Stage changed from 11-09-2026 to 11-12-2026';
  assert.deepEqual(phoneMatches(iso), []);
  assert.deepEqual(phoneMatches(us), []);
  assert.deepEqual(phoneMatches(dashed), []);
  for (const text of [iso, us, dashed]) {
    const html = linkifyPhonesAndEmailsText(text);
    assert.equal(telCount(html), 0, text);
    assert.ok(!html.includes('href="tel:'), text);
    assert.equal(html, text);
  }
});

// ── real phones still link ────────────────────────────────────────────────

test('real phone numbers are still linked', () => {
  const cases = [
    ['555-123-4567', '5551234567'],
    ['(555) 123-4567', '5551234567'],
    ['555.123.4567', '5551234567'],
    ['5551234567', '5551234567'],
    ['+1 555-123-4567', '+15551234567'],
    ['847-824-0116', '8478240116'],
    ['1-800-555-1212', '18005551212'],
  ];
  for (const [text, clean] of cases) {
    const matches = phoneMatches(text);
    assert.ok(matches.length >= 1, `expected a phone match in ${text}`);
    const html = linkifyPhonesAndEmailsText(text);
    assert.ok(telCount(html) >= 1, `expected a tel: link in ${text}`);
    assert.ok(telHrefs(html).includes(clean), `expected tel:${clean} in ${text}, got ${telHrefs(html)}`);
    assert.ok(html.includes(`class="preview-link"`));
    assert.ok(html.includes(text) || html.includes(text.trim()));
  }
});

test('a date range next to a real phone links only the phone', () => {
  const text = 'Follow-up Due Date changed from 2026-11-09 to 2026-11-12, call 555-123-4567';
  const html = linkifyPhonesAndEmailsText(text);
  assert.equal(telCount(html), 1);
  assert.deepEqual(telHrefs(html), ['5551234567']);
  assert.ok(html.includes('2026-11-09'));
  assert.ok(html.includes('2026-11-12'));
  assert.ok(!html.includes('tel:2026'));
  assert.ok(html.includes('href="tel:5551234567"'));
});

test('emails are still mailto-linked and not confused with phones', () => {
  const text = 'Write ken@vanguardadj.online or call 555-123-4567';
  const html = linkifyPhonesAndEmailsText(text);
  assert.ok(html.includes('href="mailto:ken%40vanguardadj.online"') ||
            html.includes('href="mailto:ken@vanguardadj.online"') ||
            html.includes('mailto:'));
  assert.equal(telCount(html), 1);
  assert.deepEqual(telHrefs(html), ['5551234567']);
});

test('skipPhone leaves phones and dates alone but still linkifies emails', () => {
  const text = 'ken@vanguardadj.online 555-123-4567 2026-11-09';
  const html = linkifyPhonesAndEmailsText(text, { skipPhone: true });
  assert.equal(telCount(html), 0);
  assert.ok(html.includes('mailto:'));
  assert.ok(html.includes('555-123-4567'));
  assert.ok(html.includes('2026-11-09'));
});
