// Tests: system events in project history render as ONE compact line with no
// reply button; human notes and emails keep the full card.
//
// Live request 2026-11-09 (Ken). The classifier/summarizer live in
// public/history-system-events.js (pure), so these tests import the shipped
// code rather than a copy.
//
// Run with:  node --test tests/test_history_system_events.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  isSystemHistoryEvent,
  systemEventText,
  systemEventActor,
  formatSystemEventWhen,
  SYSTEM_EVENT_MAX_CHARS,
} from '../public/history-system-events.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const appSrc = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');
const cssSrc = fs.readFileSync(path.join(ROOT, 'public', 'styles.css'), 'utf8');
const indexHtml = fs.readFileSync(path.join(ROOT, 'public', 'index.html'), 'utf8');

// ── classifier ────────────────────────────────────────────────────────────

test('status/field changes ("Action") are system events', () => {
  assert.equal(isSystemHistoryEvent({ category: { title: 'Action' } }), true);
  // and also when the category row carries the flag
  assert.equal(isSystemHistoryEvent({ category: { title: 'Action', isSystem: true } }), true);
});

// ── server-side marker (2026-11-10) ────────────────────────────────────────
// Ken's "Follow-up Due Date changed from … to …" row had category_id = NULL
// (the "Action" seed row was missing on prod), so category-title matching
// missed it. The row now carries a server marker; it is authoritative.

test('marker true + Quick Context category is compact', () => {
  assert.equal(isSystemHistoryEvent({
    isSystemEvent: true,
    category: { title: 'Quick Context', isSystem: false },
  }), true);
});

test('marker true with no category at all is compact', () => {
  assert.equal(isSystemHistoryEvent({ isSystemEvent: true, category: null }), true);
});

test('snake_case marker alias is honoured too', () => {
  assert.equal(isSystemHistoryEvent({
    is_system_event: true,
    category: { title: 'Note', isSystem: false },
  }), true);
});

test('marker false + Quick Context / Note / Comment stay full cards', () => {
  for (const title of ['Quick Context', 'Note', 'Comment', 'Customer Update']) {
    assert.equal(isSystemHistoryEvent({
      isSystemEvent: false,
      category: { title, isSystem: false },
    }), false, title);
    // absent marker (legacy row) behaves the same way
    assert.equal(isSystemHistoryEvent({ category: { title, isSystem: false } }), false, title);
  }
});

test('a marked email is still a full card', () => {
  const markedMail = { isSystemEvent: true, category: { title: 'Mail Message', isSystem: true } };
  assert.equal(isSystemHistoryEvent(markedMail), true);
  assert.equal(isSystemHistoryEvent({ ...markedMail, isMailEvent: true }), false);
  assert.equal(isSystemHistoryEvent({ ...markedMail, hasMailPayload: true }), false);
  assert.equal(isSystemHistoryEvent({ ...markedMail, hasMessageId: true }), false);
});

test('Action / isSystem fallback still works for unmarked rows', () => {
  assert.equal(isSystemHistoryEvent({ category: { title: 'Action' } }), true);
  assert.equal(isSystemHistoryEvent({ category: { title: 'Task is closed', isSystem: true } }), true);
  assert.equal(isSystemHistoryEvent({ category: { title: 'Files are uploaded', isSystem: true } }), true);
});

test('seeded system categories are system events', () => {
  for (const title of ['Files are uploaded', 'Task is closed']) {
    assert.equal(isSystemHistoryEvent({ category: { title, isSystem: true } }), true, title);
  }
});

test('human note categories are NOT system events', () => {
  for (const title of ['Note', 'Phone call', 'Email', 'Appointment', 'Text',
                       'Customer Update', 'Quick Context', 'Comment']) {
    assert.equal(isSystemHistoryEvent({ category: { title, isSystem: false } }), false, title);
  }
});

test('emails stay full cards even though "Mail Message" is a system category', () => {
  const mailCat = { title: 'Mail Message', isSystem: true };
  assert.equal(isSystemHistoryEvent({ category: mailCat }), true);
  // any of the mail markers wins
  assert.equal(isSystemHistoryEvent({ category: mailCat, isMailEvent: true }), false);
  assert.equal(isSystemHistoryEvent({ category: mailCat, hasMailPayload: true }), false);
  assert.equal(isSystemHistoryEvent({ category: mailCat, hasMessageId: true }), false);
});

test('unknown / missing categories are not system events', () => {
  assert.equal(isSystemHistoryEvent({ category: null }), false);
  assert.equal(isSystemHistoryEvent({}), false);
  assert.equal(isSystemHistoryEvent({ category: { title: '' } }), false);
});

// ── summarizer ────────────────────────────────────────────────────────────

test('system event text is one clean line', () => {
  assert.equal(systemEventText({ content: 'Stage changed from Estimate to Won' }),
    'Stage changed from Estimate to Won');
  assert.equal(systemEventText({ content: '<b>Stage</b> changed<br>to Won' }),
    'Stage changed to Won');
  assert.equal(systemEventText({ content: 'A &amp; B&nbsp;closed' }), 'A & B closed');
  assert.equal(systemEventText({ content: '  lots\n\n  of\t space  ' }), 'lots of space');
});

test('long text is truncated with an ellipsis', () => {
  const long = 'x'.repeat(SYSTEM_EVENT_MAX_CHARS + 40);
  const out = systemEventText({ content: long });
  assert.equal(out.length, SYSTEM_EVENT_MAX_CHARS);
  assert.ok(out.endsWith('\u2026'));
});

test('empty content falls back to the category label', () => {
  assert.equal(systemEventText({ content: '', category: { title: 'Files are uploaded' } }),
    'Files are uploaded event');
  assert.equal(systemEventText({}), 'System activity');
});

// ── actor (who made the change) ───────────────────────────────────────────

test('systemEventActor prefers the history row author', () => {
  assert.equal(systemEventActor({ author: 'Ken Chapman' }), 'Ken Chapman');
  assert.equal(systemEventActor({ Author: 'Ken Chapman' }), 'Ken Chapman');
});

test('systemEventActor reads createdBy.displayName when author is empty', () => {
  assert.equal(systemEventActor({
    author: '',
    createdBy: { displayName: 'Ken Chapman' },
  }), 'Ken Chapman');
  assert.equal(systemEventActor({
    createBy: { display_name: 'Claudiu' },
  }), 'Claudiu');
});

test('systemEventActor falls back to the API token name', () => {
  assert.equal(systemEventActor({ tokenName: 'mail acceptance test' }),
    'mail acceptance test');
  assert.equal(systemEventActor({ author: '', token_name: 'CI token' }),
    'CI token');
  // a named author wins over a token
  assert.equal(systemEventActor({
    author: 'Ken Chapman',
    tokenName: 'mail acceptance test',
  }), 'Ken Chapman');
});

test('systemEventActor is System when no user or token is present', () => {
  assert.equal(systemEventActor({}), 'System');
  assert.equal(systemEventActor({ author: '' }), 'System');
  assert.equal(systemEventActor({ author: '   ' }), 'System');
  // a bare numeric createdBy id is not a name
  assert.equal(systemEventActor({ createdBy: 12, author: '' }), 'System');
});

// ── compact timestamp ─────────────────────────────────────────────────────

test('formatSystemEventWhen omits the year in the current year', () => {
  const now = new Date(2026, 10, 10, 18, 0, 0); // Nov 10, 2026
  const d = new Date(2026, 10, 9, 13, 53, 0);   // Nov 9, 1:53 PM
  const out = formatSystemEventWhen(d, now, 'en-US');
  assert.match(out, /Nov 9/);
  assert.match(out, /1:53/);
  assert.doesNotMatch(out, /2026/);
  assert.doesNotMatch(out, /EST|EDT|GMT/);
});

test('formatSystemEventWhen includes the year (and zone) for other years', () => {
  const now = new Date(2026, 10, 10, 18, 0, 0);
  const d = new Date(2025, 10, 9, 13, 53, 0);
  const out = formatSystemEventWhen(d, now, 'en-US');
  assert.match(out, /Nov 9/);
  assert.match(out, /2025/);
  assert.match(out, /1:53/);
});

test('formatSystemEventWhen is empty for invalid dates', () => {
  assert.equal(formatSystemEventWhen(null), '');
  assert.equal(formatSystemEventWhen(new Date(NaN)), '');
});

// ── wiring in app.js ──────────────────────────────────────────────────────

test('app.js imports the shared classifier', () => {
  assert.ok(appSrc.includes('from "./history-system-events.js'));
  assert.ok(appSrc.includes('isSystemHistoryEvent({'));
  assert.ok(appSrc.includes('systemEventText(ev)'));
});

test('app.js passes the server marker into the classifier', () => {
  const at = appSrc.indexOf('const systemEvent = isSystemHistoryEvent({');
  const end = appSrc.indexOf('});', at);
  const call = appSrc.slice(at, end);
  assert.ok(call.includes('isSystemEvent: ev.isSystemEvent ?? ev.is_system_event'),
    'the history row marker must reach the classifier');
});

test('the classifier excludes mail BEFORE trusting the marker', () => {
  const modSrc = fs.readFileSync(
    path.join(ROOT, 'public', 'history-system-events.js'), 'utf8');
  const at = modSrc.indexOf('export function isSystemHistoryEvent');
  const end = modSrc.indexOf('\n}', at);
  const body = modSrc.slice(at, end);
  const mail = body.indexOf('isMailEvent ||');
  const marker = body.indexOf('isSystemEvent === true');
  assert.ok(mail > -1 && marker > -1);
  assert.ok(mail < marker, 'emails must be excluded before the marker is consulted');
});

test('the compact branch returns before the reply button is created', () => {
  const at = appSrc.indexOf('function renderHistoryEventItem(ev) {');
  const end = appSrc.indexOf('\nfunction ', at + 10);
  const body = appSrc.slice(at, end === -1 ? undefined : end);
  const compact = body.indexOf('opp-preview-history-item--system');
  const reply = body.indexOf('opp-preview-history-reply-btn');
  assert.ok(compact > -1, 'the compact system-event branch must exist');
  assert.ok(reply > -1, 'human notes must still get a reply button');
  assert.ok(compact < reply, 'system events must return before the reply UI is built');
  assert.ok(/return li;\s*}\s*\n\s*const metaRow/.test(body),
    'the compact branch must return the <li> immediately');
});

test('the compact row carries icon + text + actor + timestamp and no reply/delete', () => {
  const at = appSrc.indexOf('if (systemEvent) {');
  const end = appSrc.indexOf('const metaRow = document.createElement("div");', at);
  const branch = appSrc.slice(at, end);
  assert.ok(branch.includes('opp-preview-history-system-row'));
  assert.ok(branch.includes('opp-preview-history-system-text'));
  assert.ok(branch.includes('opp-preview-history-system-meta'));
  assert.ok(branch.includes('opp-preview-history-system-actor'));
  assert.ok(branch.includes('opp-preview-history-system-time'));
  assert.ok(branch.includes('historyCategoryIconHtml'));
  assert.ok(branch.includes('systemEventActor(ev)'));
  assert.ok(branch.includes('formatSystemEventWhen'));
  assert.ok(branch.includes('sysText.title'), 'full change text must be in a title tooltip');
  assert.ok(!branch.includes('reply'), 'no reply button in the compact branch');
  assert.ok(!branch.includes('opp-preview-history-delete'), 'no delete button on system events');
  assert.ok(!branch.includes('renderHistoryNoteBody'), 'no full body for system events');
});

test('the compact style exists and keeps it on one line', () => {
  assert.ok(cssSrc.includes('.opp-preview-history-item--system'));
  const at = cssSrc.indexOf('.opp-preview-history-item--system {');
  const block = cssSrc.slice(at, at + 500);
  assert.ok(block.includes('border-left'));
  assert.ok(cssSrc.includes('.opp-preview-history-item--system .opp-preview-history-system-text'));
  assert.ok(cssSrc.includes('text-overflow: ellipsis'));

  const rowAt = cssSrc.indexOf('.opp-preview-history-item--system .opp-preview-history-system-row {');
  assert.ok(rowAt > -1, 'system-row rule must exist');
  const rowBlock = cssSrc.slice(rowAt, cssSrc.indexOf('}', rowAt) + 1);
  assert.ok(rowBlock.includes('flex-wrap: nowrap'), 'the row must not wrap at normal widths');

  const textAt = cssSrc.indexOf('.opp-preview-history-item--system .opp-preview-history-system-text {');
  const textBlock = cssSrc.slice(textAt, cssSrc.indexOf('}', textAt) + 1);
  assert.ok(textBlock.includes('text-overflow: ellipsis'));
  assert.ok(textBlock.includes('white-space: nowrap'));
  assert.ok(textBlock.includes('overflow: hidden'));
  assert.ok(textBlock.includes('min-width: 0'));

  const metaAt = cssSrc.indexOf('.opp-preview-history-item--system .opp-preview-history-system-meta {');
  assert.ok(metaAt > -1, 'system-meta rule must exist');
  const metaBlock = cssSrc.slice(metaAt, cssSrc.indexOf('}', metaAt) + 1);
  assert.ok(metaBlock.includes('white-space: nowrap'));
  assert.ok(metaBlock.includes('margin-left: auto'), 'actor + time stay right-aligned');
  assert.ok(metaBlock.includes('flex: 0 0 auto'));
});

test('cache-buster bumped for the actor and linkify change', () => {
  assert.ok(indexHtml.includes('app.js?v=3.7.60'));
  assert.ok(indexHtml.includes('styles.css?v=3.7.23'));
  assert.ok(appSrc.includes('history-system-events.js?v=3.7.59'),
    'the classifier module query must bump when the module changes');
  assert.ok(appSrc.includes('linkify-phones-emails.js?v=3.7.60'));
  assert.ok(indexHtml.includes('name="format-detection"'),
    'Safari/iOS must not auto-link dates as phone numbers');
  assert.ok(indexHtml.includes('content="telephone=no"'));
});

test('app.js uses the shared phone/email linkifier', () => {
  assert.ok(appSrc.includes('linkifyPhonesAndEmailsText(txt, { skipPhone })'));
  assert.ok(!/txt\.replace\(\/\(\\\+\?\[\\d\\s\(\)\.-\]\{7,\}\\d\)/.test(appSrc),
    'the old greedy phone regex must not still be applied in app.js');
});
