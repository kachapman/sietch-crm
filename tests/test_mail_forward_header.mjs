// Tests for the Outlook-style forward header (2026-10-26).
//
// Forwarded mail showed blank From/Date: normalizeMailMessage didn't know the
// app's native row keys (from_addr/date_received/to_addr strings), so norm.*
// were empty for local mail. Fix: normalizer learns Sietch keys + a single
// mailForwardHeader() choke point (From/Sent/To/Subject) used by both forward
// paths.
//
// Run with:  node --test tests/test_mail_forward_header.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
// Mirror of normalizeMailMessage's Sietch-key extraction.
function normSietch(mail) {
  const from = mail.from_addr || mail.from || '';
  const to = mail.to_addr || mail.to || '';
  const date = mail.date_received || mail.date || '';
  return { subject: mail.subject || '', from, toList: to, date };
}
function headerDate(raw) {
  const d = new Date(String(raw).replace(' ', 'T') + (String(raw).endsWith('Z') ? '' : 'Z'));
  if (Number.isNaN(d.getTime())) return String(raw);
  return d.toLocaleString(undefined, {
    weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  });
}
function mailForwardHeader(norm, fullMail) {
  const mail = fullMail || {};
  const fromName = norm.fromName || '';
  const fromAddr = norm.from || mail.from_addr || '';
  let fromLine = fromAddr;
  if (fromName && fromAddr && fromName !== fromAddr && !fromAddr.includes(fromName)) {
    fromLine = `${fromName} <${fromAddr}>`;
  }
  const to = norm.toList || mail.to_addr || '';
  const sent = headerDate(norm.date || mail.date_received || '');
  const subject = norm.subject || '';
  return `<hr><p><strong>From:</strong> ${escapeHtml(fromLine)}</p>`
    + `<p><strong>Sent:</strong> ${escapeHtml(sent)}</p>`
    + `<p><strong>To:</strong> ${escapeHtml(to)}</p>`
    + `<p><strong>Subject:</strong> ${escapeHtml(subject)}</p>`;
}

// Sietch-native row shape (what /api/v2/mail/messages returns).
const ROW = {
  subject: 'Re: Document packet for Robin Appel',
  from_addr: 'kenc@vanguardadj.com',
  to_addr: 'jasonwheeler.ccc@gmail.com',
  date_received: '2026-10-26 17:45:00',
};

test('normalizer fills from/date/to from Sietch keys', () => {
  const n = normSietch(ROW);
  assert.equal(n.from, 'kenc@vanguardadj.com');
  assert.equal(n.toList, 'jasonwheeler.ccc@gmail.com');
  assert.equal(n.date, '2026-10-26 17:45:00');
});

test('header emits the four Outlook lines with a long date', () => {
  const n = { ...normSietch(ROW), fromName: 'Ken Chapman' };
  const h = mailForwardHeader(n, ROW);
  assert.ok(h.includes('Ken Chapman &lt;kenc@vanguardadj.com&gt;'), h);
  assert.ok(h.includes('jasonwheeler.ccc@gmail.com'), h);
  assert.ok(h.includes('Re: Document packet for Robin Appel'), h);
  assert.ok(h.includes('2026') && h.includes('October') && h.includes(':'), h);
  for (const label of ['From:', 'Sent:', 'To:', 'Subject:']) {
    assert.ok(h.includes(label), label);
  }
});

test('app.js normalizer knows the Sietch row keys', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  const at = src.indexOf('function normalizeMailMessage');
  const region = src.slice(at, at + 2200);
  for (const key of ['from_addr', 'date_received', 'to_addr']) {
    assert.ok(region.includes(key), `normalizer must read ${key}`);
  }
});

test('both forward paths use the shared header, dashed label gone', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  assert.ok(src.includes('function mailForwardHeader'), 'helper missing');
  const uses = (src.match(/mailForwardHeader\(/g) || []).length;
  assert.ok(uses >= 3, `expected def + 2 call sites, found ${uses}`);
  assert.ok(!src.includes('Forwarded message ----------'), 'dashed forward label must be gone');
});
