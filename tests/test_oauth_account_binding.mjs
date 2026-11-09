// Tests for the mail OAuth account-binding UI (public/app.js).
//
// Live bug 2026-11-09: account 8 (estimates.masterx@outlook.com) showed
// "Connected as mdm.estimates@outlook.com via Microsoft Graph", and
// "Disconnect first" never cleared it. Cause: the Graph Connect/Disconnect
// handlers were bound ONCE with `dataset.bound` and captured the account id
// of the first account opened in that browser session — every later account
// reused it. The status text also printed the ACCOUNT email instead of the
// token's real identity, so a mis-bound token looked healthy.
//
// These are source-level guards: the handlers need a live browser + OAuth
// popup to exercise end to end.
//
// Run with:  node --test tests/test_oauth_account_binding.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const appSrc = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(ROOT, 'public', 'index.html'), 'utf8');

function region(startMarker, length = 7000) {
  const at = appSrc.indexOf(startMarker);
  assert.ok(at >= 0, `marker not found: ${startMarker}`);
  return appSrc.slice(at, at + length);
}

function regionAround(marker, before = 1200, after = 600) {
  const at = appSrc.indexOf(marker);
  assert.ok(at >= 0, `marker not found: ${marker}`);
  return appSrc.slice(Math.max(0, at - before), at + after);
}

test('the account id helper reads the modal field, not a captured value', () => {
  const body = region('function accountSettingsAccountId', 700);
  assert.match(body, /#account-settings-id/);
  assert.match(body, /return raw \? parseInt\(raw, 10\) : null;/);
});

test('graph connect/disconnect are rebound on every modal open', () => {
  const body = region('function updateGraphStatusBlock');
  assert.ok(!body.includes('connectBtn.dataset.bound'),
    'a once-bound handler keeps acting on the first account opened');
  assert.ok(!body.includes('disconnectBtn.dataset.bound'));
  assert.match(body, /connectBtn\.onclick = \(\) => connectGraphSend\(accountSettingsAccountId\(\)/);
  assert.match(body, /disconnectBtn\.onclick = \(\) => disconnectGraphSend\(accountSettingsAccountId\(\)/);
});

test('graph status never paints a foreign identity as a healthy connection', () => {
  const body = region('function updateGraphStatusBlock');
  assert.match(body, /graphMismatch/);
  assert.match(body, /WRONG ACCOUNT/);
  // The green "Connected as <id>" branch must not run for a mismatch.
  const greenAt = body.indexOf('Connected${graphId} via Microsoft Graph');
  const mismatchAt = body.indexOf('if (graphMismatch)');
  assert.ok(mismatchAt >= 0 && greenAt > mismatchAt,
    'the mismatch branch must come first');
});

test('the IMAP status shows the token identity, not the account email', () => {
  const body = region("if (authType !== 'password' && oauthStatusEl)", 1800);
  assert.ok(body.includes('acct.oauthIdentity'));
  assert.ok(!body.includes('`Connected as ${acct.email}`'));
  assert.ok(body.includes('WRONG ACCOUNT'));
});

test('the IMAP connect flow is bound to the account being edited', () => {
  const body = regionAround('/api/v2/mail/oauth/authorize?provider=', 900, 400);
  assert.ok(body.includes('account_id='));
  assert.ok(body.includes('accountSettingsAccountId()'));
});

test('there is a disconnect for the IMAP/SMTP tokens', () => {
  assert.ok(indexHtml.includes('id="account-settings-oauth-disconnect"'));
  const body = region('async function disconnectExchangeOAuth', 1500);
  assert.ok(body.includes('/api/v2/mail/oauth/disconnect/'));
  assert.ok(body.includes('method: \'POST\''));
});

test('graph disconnect calls the per-account endpoint', () => {
  const body = region('async function disconnectGraphSend', 1200);
  assert.ok(body.includes('/api/v2/mail/oauth/microsoft/graph/disconnect/${accountId}'));
});

test('connect refuses to run without an account id', () => {
  const body = region('async function connectGraphSend', 500);
  assert.match(body, /if \(!accountId\)/);
});

test('no token value is ever rendered into the DOM', () => {
  for (const marker of ['oauth_access_token', 'oauth_refresh_token',
                        'oauth_graph_access_token', 'oauth_graph_refresh_token']) {
    assert.ok(!appSrc.includes(`acct.${marker}`),
      `${marker} must never be read by the UI`);
  }
});
