// Guards for task-row chips, task project search dropdown, and drafts UX.
//
// - The project search results dropdown is absolutely positioned; without a
//   positioned ancestor (#task-edit-project) it renders detached/invisible.
// - Draft row clicks must route to the drafts endpoint (mail_outgoing), never
//   to the inbox-message fetch (mail_messages) where ids collide with old mail.
// - Drafts view: cross-account hint, own pagination totals, account-named
//   save toast; endpoint returns other_accounts.
//
// Run with: node --test tests/test_task_mail_drafts.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const appJs = readFileSync(join(root, 'public', 'app.js'), 'utf8');
const css = readFileSync(join(root, 'public', 'styles.css'), 'utf8');
const server = readFileSync(join(root, 'server.py'), 'utf8');

test('task project search dropdown has a positioned ancestor', () => {
  // .contact-results is absolute top:100% — #task-edit-project must be relative.
  const m = css.match(/#task-edit-project\s*\{[^}]*\}/);
  assert.ok(m, '#task-edit-project rule missing');
  assert.ok(m[0].includes('position') && m[0].includes('relative'),
    'dropdown anchor must be position:relative');
});

test('draft row click routes to compose, not the inbox fetch', () => {
  const at = appJs.indexOf('openDraftInComposeById');
  assert.ok(at > 0, 'shared draft-open helper missing');
  // Helper used by both row click and the expand button.
  const uses = appJs.split('openDraftInComposeById(').length - 1;
  assert.ok(uses >= 3, `expected def + 2 call sites, found ${uses}`);
  // Row-click handler checks isDraft before reaching fetchMailMessage.
  const rowClick = appJs.indexOf('// Clicking row expands the email inline');
  const region = appJs.slice(rowClick, rowClick + 1200);
  assert.ok(region.includes('m.isDraft'), 'row click must branch on isDraft');
  assert.ok(region.indexOf('openDraftInComposeById') < region.indexOf('fetchMailMessage'),
    'draft branch must come before the inbox fetch');
});

test('task project chip tooltip carries the project name', () => {
  assert.ok(appJs.includes('`${projName} — open project`'),
    'project chip title must include the name (ellipsis hides it visually)');
});

test('overdue chip is icon-only with dated tooltip', () => {
  assert.ok(appJs.includes('task-chip--overdue" title="Overdue '),
    'overdue chip must keep the date in title');
  assert.ok(!appJs.match(/`Overdue \$\{d\.toLocaleDateString\(\)\}`/),
    'overdue text pill must be gone');
});

test('labels chip is a single icon with list tooltip', () => {
  assert.ok(appJs.includes('title="${escapeHtml(labels.join(", "))}"'),
    'labels must collapse to one icon with a list tooltip');
});

test('drafts view: cross-account hint + own pagination', () => {
  assert.ok(appJs.includes('mail-drafts-hint'), 'hint banner missing');
  assert.ok(appJs.includes('other_accounts'), 'must consume other_accounts');
  const at = appJs.indexOf('// Handle Drafts folder');
  const region = appJs.slice(at, at + 4000);
  assert.ok(region.includes('mailState.total = mailState.messages.length'),
    'drafts must set own totals (no stale inbox pagination)');
  assert.ok(region.includes('renderMailPagination()'), 'drafts must re-render pagination');
});

test('draft save toast names the account', () => {
  assert.ok(appJs.includes("'Draft saved' + (savedAcct?.email ? ` under ${savedAcct.email}` : '')"),
    'save toast must name the account the draft filed under');
});

test('drafts endpoint returns other_accounts', () => {
  const at = server.indexOf('def _handle_mail_drafts_get');
  const region = server.slice(at, at + 2500);
  assert.ok(region.includes('other_accounts'), 'endpoint must return per-account draft counts');
});
