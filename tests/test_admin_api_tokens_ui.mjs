// Admin API-token UI: real helper behaviour + DOM/source contracts.
//
// The admin console is vanilla JS inside public/app.js (2 MB, DOM-bound), so
// the repo convention for its event handlers is source-level guards. The
// security-relevant logic that CAN run without a browser lives in
// public/api-token-ui.js and is loaded here with vm — the real file, not a
// mirror, so drift cannot silently pass.
//
// Run with:  node --test tests/test_admin_api_tokens_ui.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const HELPERS = path.join(ROOT, 'public', 'api-token-ui.js');
const APP = path.join(ROOT, 'public', 'app.js');
const HTML = path.join(ROOT, 'public', 'index.html');

const helperSrc = fs.readFileSync(HELPERS, 'utf8');
const appSrc = fs.readFileSync(APP, 'utf8');
const html = fs.readFileSync(HTML, 'utf8');

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(helperSrc + '\n;globalThis.__ui = SietchApiTokenUi;', sandbox);
const ui = sandbox.__ui;

const START = '// ── Admin: API tokens';
const startAt = appSrc.indexOf(START);
const endAt = appSrc.indexOf('function switchAdminTab(tab) {', startAt);
const region = startAt > -1 && endAt > startAt ? appSrc.slice(startAt, endAt) : '';

// ── create validation ────────────────────────────────────────────────────
test('token name validation rejects blank/whitespace-only names', () => {
  for (const raw of ['', '   ', '\t\n', null, undefined]) {
    const res = ui.validateTokenName(raw);
    assert.equal(res.ok, false, JSON.stringify(raw));
    assert.ok(res.error.length > 0);
  }
});

test('token name validation trims and accepts a normal name', () => {
  const res = ui.validateTokenName('  Nightly archive pull  ');
  assert.equal(res.ok, true);
  assert.equal(res.value, 'Nightly archive pull');
});

test('token name length boundary is 80 characters', () => {
  assert.equal(ui.TOKEN_NAME_MAX, 80);
  assert.equal(ui.validateTokenName('a'.repeat(80)).ok, true);
  assert.equal(ui.validateTokenName('a'.repeat(81)).ok, false);
});

test('token name validation rejects control characters', () => {
  assert.equal(ui.validateTokenName('bad\u0000name').ok, false);
  assert.equal(ui.validateTokenName('bad\nname').ok, false);
  assert.equal(ui.validateTokenName('bad\u007fname').ok, false);
});

// ── create request contract (full by default; optional restrictions) ─────
test('create request defaults to full access with name + scope full', () => {
  const res = ui.buildCreateRequest('  bulk pull ');
  assert.equal(res.ok, true);
  const body = JSON.parse(JSON.stringify(res.body));
  assert.equal(body.name, 'bulk pull');
  assert.equal(body.scope, 'full');
  assert.equal(body.read_only, undefined);
  assert.equal(body.restrictions, undefined);
});

test('create request can opt into read-only and resource/method limits', () => {
  const res = ui.buildCreateRequest('notes writer', {
    readOnly: true,
    methods: ['GET', 'POST'],
    resources: ['notes'],
  });
  assert.equal(res.ok, true);
  const body = JSON.parse(JSON.stringify(res.body));
  assert.equal(body.scope, 'read');
  assert.equal(body.read_only, true);
  assert.deepEqual(body.restrictions.methods, ['GET', 'POST']);
  assert.deepEqual(body.restrictions.resources, ['notes']);
});

test('invalid names produce no request body', () => {
  const res = ui.buildCreateRequest('   ');
  assert.equal(res.ok, false);
  assert.equal(res.body, null);
  assert.ok(res.error.length > 0);
});

// ── show-once secret handling ────────────────────────────────────────────
test('create response secret is extracted; missing secret is an error', () => {
  const good = ui.extractCreatedSecret({ token: 'sietch_synthetic_fixture' });
  assert.equal(good.ok, true);
  assert.equal(good.secret, 'sietch_synthetic_fixture');
  for (const payload of [null, {}, { token: '' }, { token: 42 }]) {
    const bad = ui.extractCreatedSecret(payload);
    assert.equal(bad.ok, false);
    assert.equal(bad.secret, '');
    assert.ok(bad.error.length > 0);
  }
});

// ── list safety ──────────────────────────────────────────────────────────
test('summarizeToken whitelists safe metadata and drops secrets/hashes', () => {
  const row = ui.summarizeToken({
    id: 7,
    name: 'bulk pull',
    token_prefix: 'sietch_abcde',
    scope: 'read',
    created_by: 1,
    created_by_email: 'admin@example.test',
    created_at: '2026-11-06T10:00:00+00:00',
    last_used_at: null,
    revoked_at: null,
    revoked: false,
    token: 'sietch_SHOULD_NOT_SURVIVE',
    token_hash: 'deadbeef',
  });
  const keys = Object.keys(row).sort();
  assert.deepEqual(keys, [
    'createdAt', 'createdByEmail', 'expiresAt', 'id', 'lastUsedAt', 'name',
    'prefix', 'restrictions', 'revoked', 'revokedAt', 'scope',
  ]);
  const serialized = JSON.stringify(row);
  assert.ok(!serialized.includes('SHOULD_NOT_SURVIVE'));
  assert.ok(!serialized.includes('deadbeef'));
  assert.equal(row.revoked, false);
});

test('summarizeToken marks revoked rows and drops malformed rows', () => {
  assert.equal(ui.summarizeToken({ id: 1, revoked_at: '2026-11-06T10:00:00Z' }).revoked, true);
  assert.equal(ui.summarizeToken({ id: 1, revoked: true }).revoked, true);
  assert.equal(ui.summarizeToken({ id: 0 }), null);
  assert.equal(ui.summarizeToken({ id: 'abc' }), null);
  assert.equal(ui.summarizeToken(null), null);
});

test('summarizeTokenList accepts the API envelope and ignores junk', () => {
  const rows = ui.summarizeTokenList({
    tokens: [{ id: 1, name: 'a' }, null, { id: 2, name: 'b', token: 'leak' }],
  });
  assert.equal(rows.length, 2);
  assert.equal(rows[1].name, 'b');
  assert.ok(!JSON.stringify(rows).includes('leak'));
  assert.equal(ui.summarizeTokenList(null).length, 0);
  assert.equal(ui.summarizeTokenList({ tokens: 'nope' }).length, 0);
});

test('countActiveTokens counts non-revoked rows only', () => {
  assert.equal(ui.countActiveTokens([
    { revoked: false }, { revoked: true }, { revoked: false },
  ]), 2);
  assert.equal(ui.countActiveTokens(null), 0);
});

// ── clipboard behaviour ──────────────────────────────────────────────────
const SECRET = 'sietch_synthetic_fixture_do_not_print_0123456789';

test('copy uses the async clipboard when available', async () => {
  let got = null;
  const res = await ui.copySecretToClipboard(SECRET, {
    clipboard: { writeText: async (v) => { got = v; } },
  });
  assert.equal(res.ok, true);
  assert.equal(res.method, 'clipboard');
  assert.equal(got, SECRET);
});

test('copy falls back to the legacy path when clipboard is missing', async () => {
  let got = null;
  const res = await ui.copySecretToClipboard(SECRET, {
    clipboard: null,
    legacyCopy: (v) => { got = v; return true; },
  });
  assert.equal(res.ok, true);
  assert.equal(res.method, 'legacy');
  assert.equal(got, SECRET);
});

test('copy falls back when clipboard rejects, and never leaks the secret', async () => {
  const res = await ui.copySecretToClipboard(SECRET, {
    clipboard: { writeText: async () => { throw new Error('denied ' + SECRET); } },
    legacyCopy: () => true,
  });
  assert.equal(res.ok, true);
  assert.equal(res.method, 'legacy');
  assert.ok(!JSON.stringify(res).includes(SECRET));
});

test('copy failure is reported safely with no secret or raw error text', async () => {
  const res = await ui.copySecretToClipboard(SECRET, {
    clipboard: { writeText: async () => { throw new Error('denied ' + SECRET); } },
    legacyCopy: () => false,
  });
  assert.equal(res.ok, false);
  assert.equal(res.reason, 'failed');
  const text = JSON.stringify(res);
  assert.ok(!text.includes(SECRET));
  assert.ok(!text.includes('denied'));

  const thrown = await ui.copySecretToClipboard(SECRET, {
    legacyCopy: () => { throw new Error('boom ' + SECRET); },
  });
  assert.equal(thrown.ok, false);
  assert.ok(!JSON.stringify(thrown).includes(SECRET));
});

test('copy of an empty or absent secret is a no-op failure', async () => {
  for (const v of ['', null, undefined, 42]) {
    const res = await ui.copySecretToClipboard(v, {});
    assert.equal(res.ok, false);
    assert.equal(res.reason, 'empty');
  }
});

// ── revoke confirmation ──────────────────────────────────────────────────
test('revoke confirmation names the token and warns it stops working', () => {
  const prompt = ui.revokeConfirm({ id: 3, name: 'Nightly archive', prefix: 'sietch_abcde' });
  assert.equal(prompt.title, 'Revoke API token');
  assert.equal(prompt.confirmLabel, 'Revoke token');
  assert.ok(prompt.message.includes('Nightly archive'));
  assert.ok(prompt.message.includes('stops working immediately'));
  assert.ok(prompt.message.includes('cannot be undone'));
  assert.ok(prompt.message.includes('sietch_abcde'));
  assert.ok(!prompt.message.toLowerCase().includes('sietch_synthetic_fixture'));
});

// ── admin gating helper ──────────────────────────────────────────────────
test('isAdminViewer is strict: only boolean true is admin', () => {
  assert.equal(ui.isAdminViewer(true), true);
  for (const v of [false, 'true', 1, 'admin', null, undefined, {}]) {
    assert.equal(ui.isAdminViewer(v), false, String(v));
  }
});

// ── status/date formatting ───────────────────────────────────────────────
test('token status and date formatting degrade safely', () => {
  assert.equal(ui.formatTokenDate(null), '\u2014');
  assert.equal(ui.formatTokenDate('not-a-date'), '\u2014');
  assert.ok(ui.formatTokenDate('2026-11-06T10:00:00Z').length > 0);
  assert.equal(ui.tokenStatusText({ revoked: false }), 'Active');
  assert.equal(ui.tokenStatusText({ revoked: true }), 'Revoked');
});


// ── behavioural harness: the REAL app.js token handlers in a fake DOM ────
// The admin console code is DOM-bound, but the token flow is small enough to
// execute for real: the block from app.js is evaluated in a vm with a minimal
// element shim, the real helper module, and stubbed fetch/confirm/toast. This
// covers show-once, reset-on-revisit, clipboard failure, and revoke flows
// without a browser or npm dependencies.
const TOKEN_REGION = region + '\n;globalThis.__tokenUi = {' +
  'loadAdminApiTokens, _createApiTokenFromForm, _copyApiTokenSecret,' +
  '_revokeApiTokenById, _showApiTokenReveal, clearApiTokenReveal,' +
  'getSecret: () => _apiTokenSecret, getRows: () => _apiTokenRows };';

function makeEl(id, classes) {
  const set = new Set(classes || []);
  const el = {
    id,
    textContent: '',
    innerHTML: '',
    value: '',
    disabled: false,
    dataset: {},
    style: {},
    _classes: set,
    classList: {
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      contains: (c) => set.has(c),
      toggle: (c, on) => (on === undefined ? (set.has(c) ? set.delete(c) : set.add(c)) : (on ? set.add(c) : set.delete(c))),
    },
    addEventListener: () => {},
    focus: () => {},
    remove: () => {},
    select: () => {},
    setAttribute: () => {},
    querySelector: () => null,
    closest: () => null,
  };
  return el;
}

function buildHarness(opts = {}) {
  const els = {};
  for (const id of [
    'api-token-reveal', 'api-token-secret-value', 'api-token-copy-status',
    'api-token-copy-btn', 'api-token-create-btn', 'api-token-name',
    'api-token-create-status', 'api-token-list-status', 'api-token-list',
    'api-token-reveal-done', 'api-token-refresh-btn',
  ]) {
    els[id] = makeEl(id, id === 'api-token-reveal' ? ['hidden'] : []);
  }
  const calls = [];
  const toasts = [];
  const sandbox = {};
  sandbox.window = { addEventListener: () => {}, confirm: () => false };
  sandbox.state = { currentUserIsAdmin: opts.isAdmin !== false };
  sandbox.SietchApiTokenUi = ui;
  sandbox.$ = (sel) => (typeof sel === 'string' && sel.startsWith('#') ? els[sel.slice(1)] || null : null);
  sandbox.escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  sandbox.showToast = (msg, type) => { toasts.push({ msg, type }); };
  sandbox.confirmDialog = async () => opts.confirmResult !== false;
  sandbox.navigator = { clipboard: opts.clipboard || { writeText: async () => {} } };
  sandbox.document = {
    createElement: () => makeEl('tmp'),
    body: { appendChild: () => {} },
    execCommand: () => opts.legacyCopyOk !== false,
  };
  sandbox.api = async (path, options) => {
    calls.push({ path, options });
    if (opts.apiImpl) return opts.apiImpl(path, options, calls.length);
    if (path === '/api/v2/admin/api-tokens' && options && options.method === 'POST') {
      return { id: 5, name: 'created', token: 'sietch_synthetic_fixture_value', warning: 'only once', token_prefix: 'sietch_abcde' };
    }
    return { tokens: [] };
  };
  vm.createContext(sandbox);
  vm.runInContext(helperSrc, sandbox);
  vm.runInContext(TOKEN_REGION, sandbox);
  return { sandbox, els, calls, toasts, tokenUi: sandbox.__tokenUi };
}

test('list rendering escapes names and never contains the secret', async () => {
  const secret = 'sietch_synthetic_fixture_leak_0000';
  const h = buildHarness({
    apiImpl: () => ({
      tokens: [{
        id: 1, name: '<img src=x onerror=alert(1)>', token_prefix: 'sietch_abcde',
        scope: 'read', created_by_email: 'admin@synthetic.test', created_at: null,
        last_used_at: null, revoked_at: null, token: secret,
      }],
    }),
  });
  await h.tokenUi.loadAdminApiTokens();
  const htmlOut = h.els['api-token-list'].innerHTML;
  assert.ok(!htmlOut.includes(secret), 'secret must not reach the DOM');
  assert.ok(!htmlOut.includes('<img'), 'names must be escaped');
  assert.ok(htmlOut.includes('&lt;img'), 'escaped name is rendered');
  assert.ok(htmlOut.includes('sietch_abcde'), 'prefix is shown');
});

test('create shows the secret once, clears the input, and locks the button', async () => {
  const secret = 'sietch_synthetic_fixture_created_1111';
  const h = buildHarness({
    apiImpl: (path, options) => {
      if (options && options.method === 'POST') {
        return { id: 5, name: 'nightly', token: secret, warning: 'only once', token_prefix: 'sietch_abcde' };
      }
      return { tokens: [] };
    },
  });
  h.els['api-token-name'].value = '  nightly  ';
  await h.tokenUi._createApiTokenFromForm({ preventDefault() {} });

  assert.equal(h.els['api-token-secret-value'].textContent, secret);
  assert.ok(!h.els['api-token-secret-value'].innerHTML.includes(secret), 'textContent only');
  assert.ok(!h.els['api-token-reveal'].classList.contains('hidden'));
  assert.equal(h.els['api-token-name'].value, '');
  assert.equal(h.els['api-token-create-btn'].disabled, true);
  assert.equal(h.tokenUi.getSecret(), secret);
  const post = h.calls.find((c) => c.options && c.options.method === 'POST');
  assert.deepEqual(JSON.parse(post.options.body), { name: 'nightly', scope: 'full' });
  assert.ok(h.els['api-token-create-status'].textContent.includes('Copy the secret now'));
});

test('revisiting/leaving the tab clears the secret and re-enables create', async () => {
  const secret = 'sietch_synthetic_fixture_reset_2222';
  const h = buildHarness();
  h.tokenUi._showApiTokenReveal(secret);
  assert.equal(h.tokenUi.getSecret(), secret);
  h.tokenUi.clearApiTokenReveal();
  assert.equal(h.tokenUi.getSecret(), null);
  assert.equal(h.els['api-token-secret-value'].textContent, '');
  assert.ok(h.els['api-token-reveal'].classList.contains('hidden'));
  assert.equal(h.els['api-token-create-btn'].disabled, false);
});

test('copy failure is reported without the secret; success confirms', async () => {
  const secret = 'sietch_synthetic_fixture_copy_3333';
  const failing = buildHarness({
    clipboard: { writeText: async () => { throw new Error('denied ' + secret); } },
    legacyCopyOk: false,
  });
  failing.tokenUi._showApiTokenReveal(secret);
  await failing.tokenUi._copyApiTokenSecret();
  const failText = failing.els['api-token-copy-status'].textContent;
  assert.ok(failText.includes('copy it manually'));
  assert.ok(!failText.includes(secret));
  assert.equal(failing.els['api-token-copy-btn'].disabled, false);

  let copied = null;
  const okH = buildHarness({ clipboard: { writeText: async (v) => { copied = v; } } });
  okH.tokenUi._showApiTokenReveal(secret);
  await okH.tokenUi._copyApiTokenSecret();
  assert.equal(copied, secret);
  assert.ok(okH.els['api-token-copy-status'].textContent.includes('Copied'));
});

test('create validation blocks the request and shows the error', async () => {
  const h = buildHarness();
  h.els['api-token-name'].value = '   ';
  await h.tokenUi._createApiTokenFromForm({ preventDefault() {} });
  assert.equal(h.calls.length, 0, 'no request for an invalid name');
  assert.ok(h.els['api-token-create-status'].textContent.includes('Enter a name'));
  assert.equal(h.tokenUi.getSecret(), null);
});

test('create API failure is surfaced and keeps no secret', async () => {
  const h = buildHarness({ apiImpl: () => { throw Object.assign(new Error('server said no'), { status: 400 }); } });
  h.els['api-token-name'].value = 'nightly';
  await h.tokenUi._createApiTokenFromForm({ preventDefault() {} });
  assert.ok(h.els['api-token-create-status'].textContent.includes('Could not create token: server said no'));
  assert.equal(h.tokenUi.getSecret(), null);
  assert.equal(h.els['api-token-create-btn'].disabled, false);
});

test('non-admin loads nothing', async () => {
  const h = buildHarness({ isAdmin: false });
  await h.tokenUi.loadAdminApiTokens();
  assert.equal(h.calls.length, 0);
});

test('revoke cancels cleanly and only mutates after confirmation', async () => {
  const h = buildHarness({
    confirmResult: false,
    apiImpl: () => ({ tokens: [{ id: 9, name: 'keep me', token_prefix: 'sietch_abcde', scope: 'read', revoked_at: null }] }),
  });
  await h.tokenUi.loadAdminApiTokens();
  await h.tokenUi._revokeApiTokenById(9, h.els['api-token-copy-btn']);
  assert.equal(h.calls.filter((c) => c.path.endsWith('/revoke')).length, 0);
});

test('revoke posts to the right endpoint and refreshes the list', async () => {
  let listCalls = 0;
  const h = buildHarness({
    confirmResult: true,
    apiImpl: (path, options) => {
      if (path.endsWith('/revoke')) return { ok: true };
      listCalls += 1;
      return { tokens: [{ id: 9, name: 'kill me', token_prefix: 'sietch_abcde', scope: 'read', revoked_at: null }] };
    },
  });
  await h.tokenUi.loadAdminApiTokens();
  const before = listCalls;
  await h.tokenUi._revokeApiTokenById(9, h.els['api-token-copy-btn']);
  const revoke = h.calls.find((c) => c.path.endsWith('/revoke'));
  assert.equal(revoke.path, '/api/v2/admin/api-tokens/9/revoke');
  assert.equal(revoke.options.method, 'POST');
  assert.equal(listCalls, before + 1, 'metadata is reloaded after revoking');
  assert.ok(h.toasts.some((t) => t.msg.includes('revoked')));
});

test('already-revoked (404) reports and refreshes instead of hanging', async () => {
  const h = buildHarness({
    confirmResult: true,
    apiImpl: (path) => {
      if (path.endsWith('/revoke')) throw Object.assign(new Error('gone'), { status: 404 });
      return { tokens: [{ id: 9, name: 'stale row', token_prefix: 'sietch_abcde', scope: 'read', revoked_at: null }] };
    },
  });
  await h.tokenUi.loadAdminApiTokens();
  await h.tokenUi._revokeApiTokenById(9, h.els['api-token-copy-btn']);
  assert.ok(h.toasts.some((t) => t.msg.includes('already revoked')));
});

// ── markup: tab, disclosure, create form, reveal panel ───────────────────
test('admin console gets the combined API & MCP tab with an inline SVG icon', () => {
  const tabAt = html.indexOf('data-tab="api-mcp"');
  assert.ok(tabAt > -1, 'the API & MCP tab button must exist');
  const btnRegion = html.slice(tabAt, tabAt + 1400);
  assert.ok(btnRegion.includes('<svg'), 'tab icons must stay inline SVGs (admin convention)');
  assert.ok(btnRegion.includes('class="admin-tab-label">API &amp; MCP<'), 'tab label');
  // the old separate tabs are gone (they redirect in switchAdminTab)
  assert.ok(!html.includes('data-tab="tokens"'));
  assert.ok(!html.includes('data-tab="mcp"'));
});

test('the combined tab starts hidden like every other admin tab', () => {
  assert.ok(html.includes('id="admin-tab-api-mcp" class="admin-tab-content hidden"'));
});

test('scope disclosure says full access by default', () => {
  assert.ok(html.includes('Full access by default'), html.slice(html.indexOf('api-token-scope-notice'), html.indexOf('api-token-scope-notice') + 500));
  assert.ok(html.includes('Optional restrictions'));
  assert.ok(html.includes('api-token-read-only'));
  assert.ok(html.includes('data-api-token-method'));
  assert.ok(html.includes('data-api-token-resource'));
  assert.ok(html.includes('api-token-expires'));
});

test('create form is name-only with an 80-char cap', () => {
  assert.ok(html.includes('id="api-token-create-form"'));
  assert.ok(html.includes('id="api-token-name" class="form-control" maxlength="80"'));
  assert.ok(html.includes('id="api-token-create-btn"'));
  assert.ok(!/api-token-create-form[\s\S]{0,600}<select/i.test(html), 'create form must not offer a scope selector');
});

test('reveal panel warns it is shown once and cannot be recovered', () => {
  assert.ok(html.includes('id="api-token-reveal" class="api-token-reveal hidden"'));
  const at = html.indexOf('id="api-token-reveal"');
  const tab = html.slice(at, at + 2200);
  assert.ok(/cannot be recovered/i.test(tab));
  assert.ok(/only time the secret is shown/i.test(tab));
  assert.ok(/not stored in the browser/i.test(tab));
  assert.ok(tab.includes('id="api-token-copy-btn"'), 'copy button');
  assert.ok(tab.includes('id="api-token-reveal-done"'), 'dismiss button');
  assert.ok(tab.includes('id="api-token-secret-value"'), 'secret target');
  assert.ok(tab.includes('id="api-token-copy-status"'), 'copy status');
});

test('token list has a loading/status region and a container', () => {
  assert.ok(html.includes('id="api-token-list-status"'));
  assert.ok(html.includes('id="api-token-list" class="api-token-list"'));
  const listStatusAt = html.indexOf('id="api-token-list-status"');
  const listStatusTag = html.slice(listStatusAt, html.indexOf('>', listStatusAt));
  assert.ok(listStatusTag.includes('aria-live="polite"'), 'list status must be announced');
});

test('index.html loads the helper before app.js', () => {
  const helperAt = html.indexOf('src="api-token-ui.js');
  const appAt = html.indexOf('src="app.js');
  assert.ok(helperAt > -1, 'api-token-ui.js must be loaded');
  assert.ok(appAt > helperAt, 'helper must precede app.js');
});

// ── app.js wiring and secret-lifecycle guards ────────────────────────────
test('the token UI block exists ahead of switchAdminTab', () => {
  assert.ok(startAt > -1, 'API tokens block marker');
  assert.ok(region.length > 2000, 'token UI block must be present');
});

test('admin console navigation is gated on a real admin session', () => {
  const at = appSrc.indexOf('function openAdminConsoleModal()');
  const body = appSrc.slice(at, at + 700);
  const guard = body.indexOf('state.currentUserIsAdmin !== true');
  const reveal = body.indexOf('modal.classList.remove("hidden")');
  assert.ok(guard > -1, 'openAdminConsoleModal must check isAdmin');
  assert.ok(guard < reveal, 'the admin check must run before the modal is shown');
});

test('data loading is gated again (defence in depth)', () => {
  const fnAt = region.indexOf('async function loadAdminApiTokens()');
  assert.ok(fnAt > -1, 'loadAdminApiTokens must exist');
  const fnBody = region.slice(fnAt, fnAt + 900);
  const guard = fnBody.indexOf('ui.isAdminViewer(state.currentUserIsAdmin)');
  const call = fnBody.indexOf('api("/api/v2/admin/api-tokens")');
  assert.ok(guard > -1, 'loadAdminApiTokens must check isAdminViewer');
  assert.ok(guard < call, 'the admin check must run before the list request');
});

test('API endpoints match the verified backend contract', () => {
  assert.ok(region.includes('api("/api/v2/admin/api-tokens")'), 'GET list endpoint');
  assert.ok(region.includes('method: "POST"'), 'POST create/revoke');
  assert.ok(region.includes('JSON.stringify(req.body)'), 'create body comes from buildCreateRequest');
  assert.ok(region.includes('"/api/v2/admin/api-tokens/" + encodeURIComponent(String(row.id)) + "/revoke"'),
    'revoke endpoint');
});

test('the secret is written with textContent, never innerHTML', () => {
  assert.ok(region.includes('code.textContent = secret'));
  assert.ok(!/innerHTML\s*=\s*[^;]*(secret|_apiTokenSecret)/.test(region),
    'secret must never be interpolated into innerHTML');
});

test('the secret is never persisted, logged, or put in a URL', () => {
  for (const forbidden of [
    'localStorage', 'sessionStorage', 'document.cookie', 'console.log',
    'console.error', 'console.warn', 'console.debug', 'history.pushState',
    'history.replaceState', 'location.href', 'location.hash', 'sendBeacon',
  ]) {
    assert.ok(!region.includes(forbidden), 'token UI must not use ' + forbidden);
  }
  assert.ok(!/showToast\([^)]*secret/i.test(region), 'toasts must not carry the secret');
});

test('the reveal is cleared on tab-away, close, sign-out, and page hide', () => {
  const switchAt = appSrc.indexOf('function switchAdminTab(tab) {');
  const switchBody = appSrc.slice(switchAt, switchAt + 1400);
  assert.ok(switchBody.includes('if (tab !== "api-mcp") clearApiTokenReveal();'));
  assert.ok(switchBody.includes('loadAdminApiTokens()'), 'the combined tab loads the token list');
  const dismiss = appSrc.slice(appSrc.indexOf('[data-admin-console-dismiss]'), appSrc.indexOf('[data-admin-console-dismiss]') + 300);
  assert.ok(dismiss.includes('clearApiTokenReveal()'), 'closing the console clears the secret');
  assert.ok(appSrc.includes('window.addEventListener("pagehide", clearApiTokenReveal)'));
  assert.ok(appSrc.includes('clearApiTokenReveal();\n      try {\n        await fetch("/api/v2/auth/logout"'),
    'sign-out clears the secret');
  assert.ok(region.includes('_apiTokenSecret = null;'), 'clear helper resets the in-memory secret');
});

test('a second create is blocked while an unrevealed secret is on screen', () => {
  assert.ok(region.includes('btn.disabled = !!_apiTokenSecret'));
  assert.ok(region.includes('if (createBtn) createBtn.disabled = true'));
});

test('copy mistakes are surfaced with a manual fallback, not the browser error', () => {
  assert.ok(region.includes('select the token above and copy it manually'));
  assert.ok(region.includes('ui.copySecretToClipboard(secret'));
  assert.ok(!/copy[^\\n]*e\\.message/i.test(region));
});

test('list rendering uses the whitelisted summary, not raw API rows', () => {
  assert.ok(region.includes('_apiTokenRows = ui.summarizeTokenList(data)'));
  assert.ok(region.includes('_apiTokenRows.map(_apiTokenRowHtml)'));
});

test('revoke asks for confirmation and reloads metadata afterwards', () => {
  assert.ok(region.includes('ui.revokeConfirm(row)'));
  assert.ok(region.includes('confirmDialog('));
  assert.ok(region.includes('danger: true'));
  assert.ok(region.includes('btn.textContent = "Revoking…"'));
  assert.ok(region.includes('await loadAdminApiTokens()'));
  assert.ok(region.includes('e.status === 404'), 'already-revoked is handled');
});

test('every API-token element id used by app.js exists in index.html', () => {
  const ids = new Set(
    Array.from(region.matchAll(/\$\("#(api-token-[a-z0-9-]+)"\)/g)).map((m) => m[1])
  );
  assert.ok(ids.size >= 8, 'expected the token UI to reference its elements, found ' + ids.size);
  for (const id of ids) {
    assert.ok(html.includes('id="' + id + '"'), 'missing #' + id + ' in index.html');
  }
});

test('the combined tab button and its content panel are paired', () => {
  assert.ok(html.includes('data-tab="api-mcp"'));
  assert.ok(html.includes('id="admin-tab-api-mcp"'));
  const switchAt = appSrc.indexOf('function switchAdminTab(tab) {');
  const switchBody = appSrc.slice(switchAt, switchAt + 1600);
  assert.ok(switchBody.includes('document.querySelectorAll("#admin-console-modal .admin-tab-content")'));
  assert.ok(switchBody.includes('Promise.resolve(loadAdminMcp()).finally(() => loadAdminApiTokens())'));
  // old tab names still resolve to the combined tab
  assert.ok(switchBody.includes('if (tab === "tokens" || tab === "mcp") tab = "api-mcp";'));
});

test('create/loading/error/empty states exist', () => {
  assert.ok(region.includes('Creating token…'));
  assert.ok(region.includes('Could not create token: '));
  assert.ok(region.includes('Loading tokens…'));
  assert.ok(region.includes('Could not load tokens: '));
  assert.ok(region.includes('Admin access is required to manage API tokens.'));
  assert.ok(region.includes('No API tokens yet.'));
  assert.ok(region.includes('btn.disabled = true'), 'create disables while in flight');
});
