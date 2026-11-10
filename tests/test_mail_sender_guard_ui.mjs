// Tests: compose From-account selection + sender guard UI (source guards).
//
// Run with:  node --test tests/test_mail_sender_guard_ui.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const app = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'public', 'index.html'), 'utf8');

function region(marker, len = 3000) {
  const at = app.indexOf(marker);
  assert.ok(at >= 0, `marker not found: ${marker}`);
  return app.slice(at, at + len);
}

test('the only preselects are the open account tab, a reply original, or a draft account', () => {
  const body = region('function composeAccountOptionsHtml', 700);
  assert.ok(body.includes('<option value="">Choose account…</option>'));
  const pre = region('function composePreselectId', 900);
  assert.ok(pre.includes('opts.originalAccountId'));
  assert.ok(pre.includes('opts.draftAccountId'));
  assert.ok(pre.includes('activeMailAccountId()'));
  const active = region('function activeMailAccountId', 700);
  assert.ok(active.includes("active === 'crm'"));
  assert.ok(!app.includes('accounts[0].id'), 'no alphabetical first-account fallback');
});

test('the compose modal applies the approved preselect and records thread context', () => {
  const body = region('async function openComposeModal', 4000);
  assert.ok(body.includes('composePreselectId(opts)'));
  assert.ok(body.includes('replyToMessageId'));
  assert.ok(body.includes('composeAccountOptionsHtml'));
});

test('replies/forwards preselect the original account at every entry point', () => {
  const reply = region('async function openReplyCompose', 1200);
  assert.ok(reply.includes('originalAccountId: data.account_id'));
  assert.ok(reply.includes('replyToMessageId: messageId'));
  const draft = region('async function openDraftInComposeById', 1200);
  assert.ok(draft.includes('draftAccountId: draft.account_id'));
  // preview reply passes the original account + thread
  assert.ok(app.includes('originalAccountId: fullMail &&'));
});

test('the send dialog calls the server preflight and blocks on 409', () => {
  const body = region('async function confirmMailSend', 4000);
  assert.ok(body.includes('/api/v2/mail/send/preflight'));
  assert.ok(body.includes('preflight.ok === false'));
  assert.ok(body.includes('return false'));
  assert.ok(body.includes('Sending from'));
  assert.ok(body.includes('contactName'));
});

test('sends carry the thread id and show sentAs', () => {
  assert.ok(app.includes('reply_to_message_id'));
  const toast = app.match(/result\.sentAs/g) || [];
  assert.ok(toast.length >= 1);
  assert.ok((app.match(/inlineResult\.sentAs/g) || []).length >= 1);
  assert.ok((app.match(/tabResult\.sentAs/g) || []).length >= 1);
});

test('a dead token is shown in the mail tab and the compose banner', () => {
  assert.ok(html.includes('id="mail-auth-banner"'));
  assert.ok(app.includes('function renderMailAuthBanner'));
  assert.ok(app.includes('renderMailAuthBanner(accounts)'));
  assert.ok(app.includes('acct.authError'));
});

test('admins can map/clear the contractor Contact on an account', () => {
  assert.ok(html.includes('id="account-settings-contractor-section"'));
  assert.ok(html.includes('id="account-settings-contractor-search"'));
  assert.ok(html.includes('id="account-settings-contractor-clear"'));
  const body = region('// Contractor mapping search (bind once).', 2500);
  assert.ok(body.includes('/api/v2/contacts?q='));
  assert.ok(body.includes('dataset.contactId'));
  assert.ok(app.includes('contractor_contact_id:'));
});

test('the submitted account id is read from the visible select at send time', () => {
  const helper = region('function selectedFromAccountId', 400);
  assert.ok(helper.includes('parseInt(selectEl.value'));
  // all three send surfaces use the helper (no direct parseInt of the select)
  const uses = (app.match(/selectedFromAccountId\(/g) || []).length;
  assert.ok(uses >= 3, `expected the helper at every send surface, got ${uses}`);
});

test('a divergent actual sender is a loud error', () => {
  const body = region('function verifySentAs', 900);
  assert.ok(body.includes('toLowerCase()'));
  assert.ok(body.includes('showToast'));
  assert.ok(body.includes('Wrong sender'));
  assert.ok((app.match(/verifySentAs\(accountId, /g) || []).length >= 3);
});

test('consecutive compose opens cannot leak a previous selection', () => {
  // Every open rebuilds the options from composePreselectId(opts) and assigns
  // the value from that result — no cached value survives.
  const body = region('async function openComposeModal', 4000);
  assert.ok(body.includes("fromSelect.innerHTML = composeAccountOptionsHtml(wanted || '')"));
  assert.ok(body.includes("fromSelect.value = wanted ? String(wanted) : ''"));
});
