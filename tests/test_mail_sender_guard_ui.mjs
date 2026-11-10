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

test('the From select starts empty and never defaults to the first account', () => {
  const body = region('function composeAccountOptionsHtml', 700);
  assert.ok(body.includes('<option value="">Choose account…</option>'));
  const mapped = region('function mappedAccountIdsForContact', 600);
  assert.ok(mapped.includes('contractorContactId'));
  const resolve = region('async function resolveComposeAccountId', 1200);
  assert.ok(resolve.includes('mappedAccountIdsForContact'));
  assert.ok(resolve.includes('messageAccountId'));
  assert.ok(!resolve.includes('accounts[0]'));
  // the old alphabetical fallback is gone from the inline reply
  const inline = region('async function renderInlineReply', 1500);
  assert.ok(!inline.includes('accounts[0].id'), 'no first-account fallback');
});

test('the compose modal uses the job mapping and records thread context', () => {
  const body = region('async function openComposeModal', 4000);
  assert.ok(body.includes('resolveComposeAccountId'));
  assert.ok(body.includes('dealId: opts.dealId'));
  assert.ok(body.includes('replyToMessageId'));
  assert.ok(body.includes('composeAccountOptionsHtml'));
});

test('all compose entry points bind an account/deal/thread', () => {
  const reply = region('async function openReplyCompose', 1200);
  assert.ok(reply.includes('fromAccountId: data.account_id'));
  assert.ok(reply.includes('dealId: data.deal_id'));
  assert.ok(reply.includes('replyToMessageId: messageId'));
  const draft = region('async function openDraftInComposeById', 1200);
  assert.ok(draft.includes('fromAccountId: draft.account_id'));
  assert.ok(draft.includes('dealId: draft.deal_id'));
  // preview reply passes the job + thread
  const at = app.indexOf('dealId: oppPreviewContext?.oppId || null,');
  assert.ok(at > -1);
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

test('Send is disabled until the From options are loaded', () => {
  const body = region('async function openComposeModal', 4500);
  assert.ok(body.includes('fromSelect.disabled = true'));
  assert.ok(body.includes('fromSelect.disabled = false'));
  assert.ok(body.includes("sendBtn0.disabled = true"));
});
