// Tests for Mail modal flicker guards and the Edit Mail Account stale-save fix.
//
// Live bugs 2026-11-10:
//   1. Reused Edit Mail Account modal: switching accounts set the id immediately
//      but left the previous name/email/originalEmail in the form until the
//      async fetch returned. A Save in that gap wrote account 8's display_name
//      ("Estimates Masterx") onto account 10 (mdm.estimates@outlook.com).
//   2. Mail inbox flicker: renderMailTabs added a new delegated click listener
//      on every call (innerHTML does not remove listeners on the container),
//      so switching mailbox tabs fired switchMailTab N times and each load
//      wiped the list to "Loading emails…". mail:new during a scanner sync
//      would also full-rebuild the list per message once the open flag is set.
//
// Run with:  node --test tests/test_mail_modal_stability.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  mailListSignature,
  shouldSkipMailListRender,
  mailTabsSignature,
  accountSettingsSaveIsAllowed,
  createCoalescedRunner,
  createGenerationGate,
  MAIL_NEW_RELOAD_DEBOUNCE_MS,
} from '../public/mail-modal-stability.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const appSrc = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(ROOT, 'public', 'index.html'), 'utf8');

function region(startMarker, length = 8000) {
  const at = appSrc.indexOf(startMarker);
  assert.ok(at >= 0, `marker not found: ${startMarker}`);
  return appSrc.slice(at, at + length);
}

test('accountSettingsSaveIsAllowed refuses a save until the loaded id matches', () => {
  assert.equal(accountSettingsSaveIsAllowed('', null), true);
  assert.equal(accountSettingsSaveIsAllowed(null, null), true);
  assert.equal(accountSettingsSaveIsAllowed(10, null), false);
  assert.equal(accountSettingsSaveIsAllowed(10, 8), false);
  assert.equal(accountSettingsSaveIsAllowed('10', 8), false);
  assert.equal(accountSettingsSaveIsAllowed(10, 10), true);
  assert.equal(accountSettingsSaveIsAllowed('10', 10), true);
});

test('openAccountSettingsModal resets originalEmail and disables Save on switch', () => {
  const body = region('function openAccountSettingsModal', 12000);
  assert.match(body, /_accountSettingsLoadGen/);
  assert.match(body, /originalEmail/);
  assert.match(body, /setAccountSettingsSaveEnabled\(false\)/);
  assert.match(body, /_accountSettingsLoadedId/);
  // Must reset BEFORE the fetch, not only inside the then().
  const resetAt = body.indexOf('setAccountSettingsSaveEnabled(false)');
  const fetchAt = body.indexOf('api(`/api/v2/mail/accounts`)');
  assert.ok(resetAt >= 0 && fetchAt > resetAt,
    'Save must be disabled and fields reset before the accounts fetch');
});

test('stale account-settings fetches are ignored', () => {
  const body = region('function openAccountSettingsModal', 14000);
  assert.match(body, /_accountSettingsLoadGen/);
  assert.match(body, /gen !== _accountSettingsLoadGen/);
});

test('Save is refused when the loaded account id does not match the form', () => {
  const body = region("const saveBtn = $('#account-settings-save')", 4000);
  assert.match(body, /accountSettingsSaveIsAllowed/);
  assert.match(body, /_accountSettingsLoadedId/);
});

test('renderMailTabs binds the tab click listener only once', () => {
  const body = region('async function renderMailTabs', 7000);
  assert.match(body, /tabsEl\.dataset\.clickBound/);
  const adds = body.split("tabsEl.addEventListener('click'").length - 1;
  assert.ok(adds >= 1 && adds <= 2, `expected 1-2 addEventListener sites (success + catch), got ${adds}`);
  assert.equal((body.match(/if \(!tabsEl\.dataset\.clickBound\)/g) || []).length, adds,
    'every tab click bind must be gated by clickBound');
});

test('renderMailTabs skips a full innerHTML rebuild when the tab set is unchanged', () => {
  const body = region('async function renderMailTabs', 7000);
  assert.match(body, /mailTabsSignature/);
  assert.match(body, /tabsEl\.dataset\.tabsSig/);
});

test('mail:new reloads are coalesced, not per-message full redraws', () => {
  assert.match(appSrc, /_scheduleMailSyncReload/);
  assert.match(appSrc, /createCoalescedRunner/);
  assert.match(appSrc, /MAIL_NEW_RELOAD_DEBOUNCE_MS/);
  const handler = region('case "mail:new":', 400);
  assert.match(handler, /_scheduleMailSyncReload\(\)/);
  assert.ok(!/case "mail:new":[\s\S]{0,200}loadMailMessagesForModal\(true\)/.test(handler),
    'mail:new must not call loadMailMessagesForModal directly');
});

test('loadMailMessagesForModal ignores stale responses and does not wipe an existing list', () => {
  const body = region('async function loadMailMessagesForModal', 5000);
  assert.match(body, /_mailLoadGen/);
  assert.match(body, /gen !== _mailLoadGen/);
  assert.match(body, /shouldSkipMailListRender/);
  // Non-background loads must not unconditionally replace the list with
  // "Loading emails…" when rows are already on screen (stacked tab switches).
  const loadingAt = body.indexOf("Loading emails…");
  assert.ok(loadingAt >= 0, 'loading placeholder still exists for an empty list');
  assert.match(body, /mail-item/);
});

test('openMailInboxModal sets the mailModalOpen flag used by mail:new', () => {
  const openBody = region('async function openMailInboxModal', 2500);
  assert.match(openBody, /state\.mailModalOpen\s*=\s*true/);
  const closeBody = region('function closeMailModal', 800);
  assert.match(closeBody, /state\.mailModalOpen\s*=\s*false/);
});

test('mail list signature skips identical background redraws', () => {
  const a = mailListSignature([{ id: 1, subject: 'Hi', read: false }], 1, 1);
  const b = mailListSignature([{ id: 1, subject: 'Hi', read: false }], 1, 1);
  const c = mailListSignature([{ id: 1, subject: 'Hi', read: true }], 1, 1);
  assert.equal(a, b);
  assert.equal(shouldSkipMailListRender(a, b), true);
  assert.equal(shouldSkipMailListRender(a, c), false);
  assert.equal(shouldSkipMailListRender(null, a), false);
});

test('mail tab signature changes when a label or the active tab changes', () => {
  const accounts = [
    { id: 8, display_name: 'Estimates Masterx', email: 'estimates.masterx@outlook.com', is_crm_mail: false },
    { id: 10, display_name: 'MDM Estimates', email: 'mdm.estimates@outlook.com', is_crm_mail: false },
    { id: 2, display_name: 'CRM Mail', email: 'crm@vanguardadj.online', is_crm_mail: true },
  ];
  const a = mailTabsSignature(accounts, 8, true);
  const b = mailTabsSignature(accounts, 8, true);
  const c = mailTabsSignature(accounts, 10, true);
  const renamed = accounts.map((x) => (x.id === 10 ? { ...x, display_name: 'Estimates Masterx' } : x));
  const d = mailTabsSignature(renamed, 8, true);
  assert.equal(a, b);
  assert.notEqual(a, c);
  assert.notEqual(a, d);
  assert.ok(!a.includes('CRM Mail'), 'company inbox is the hardcoded CRM Mail tab');
});

test('coalesced runner: a burst of sync events produces one run (bounded render count)', async () => {
  let renders = 0;
  const run = createCoalescedRunner(() => { renders += 1; }, 30);
  for (let i = 0; i < 25; i++) run();
  assert.equal(run.stats().calls, 25);
  assert.equal(run.stats().runs, 0);
  assert.equal(renders, 0);
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(renders, 1, '25 mail:new-style calls must produce exactly one render');
  assert.equal(run.stats().runs, 1);
  assert.equal(run.stats().pending, false);
  assert.ok(MAIL_NEW_RELOAD_DEBOUNCE_MS >= 200);
});

test('generation gate ignores stale responses after a rapid account/tab switch', () => {
  const gate = createGenerationGate();
  const first = gate.next();
  const second = gate.next();
  assert.equal(gate.isCurrent(first), false);
  assert.equal(gate.isCurrent(second), true);
  assert.equal(gate.current, 2);
});

test('the account-settings save button exists in the markup', () => {
  assert.ok(indexHtml.includes('id="account-settings-save"'));
  assert.ok(indexHtml.includes('id="account-settings-name"'));
  assert.ok(indexHtml.includes('id="account-settings-id"'));
});
