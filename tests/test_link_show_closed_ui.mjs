// Tests: the email inbox "Link to deal" popup can find CLOSED projects, and
// the bookmark preview shows the project's unique ID.
//
// Context (2026-11-03, Ken-approved):
//   - The mail inbox toolbar link icon opens a popup with a search box and a
//     "Link" button. It searched /api/v2/projects with stageType=0, so only
//     OPEN projects matched and a closed project showed "No matches".
//     Fix: an opt-in `include_closed` backend flag + a "Show closed" checkbox
//     that is unchecked by default.
//   - The bookmark sidebar preview header showed the project name but not the
//     project's unique project number. Fix: a shared badge renderer, using the
//     same field/format as the full project page.
//
// Run with: node --test tests/test_link_show_closed_ui.mjs
//
// These are source-level guards on public/app.js + public/index.html, the
// repo's existing convention for DOM-bound code that cannot be unit-tested
// without a full DOM harness.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const appJs = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(ROOT, 'public', 'index.html'), 'utf8');
const stylesCss = fs.readFileSync(path.join(ROOT, 'public', 'styles.css'), 'utf8');
const serverPy = fs.readFileSync(path.join(ROOT, 'server.py'), 'utf8');

/** Extract a top-level function body by name (brace matching). */
function fnBody(src, name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\([^)]*\\)\\s*\\{`).exec(src);
  assert.ok(m, `${name} not found`);
  let start = m.index + m[0].length - 1;
  let depth = 1;
  let i = start + 1;
  while (i < src.length && depth > 0) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') depth--;
    i++;
  }
  assert.equal(depth, 0, `unbalanced braces in ${name}`);
  return src.slice(start, i);
}

// ── Fix 1: the "Show closed" checkbox ──────────────────────────────────────

test('the mail link popup has a "Show closed" checkbox', () => {
  assert.ok(indexHtml.includes('id="mail-link-deal-include-closed"'),
    'the #mail-link-deal-include-closed checkbox is missing');
  assert.ok(indexHtml.includes('Show closed'), 'the "Show closed" label is missing');
});

test('the "Show closed" checkbox is unchecked by default', () => {
  // Capture the whole <input ...> tag (the attributes come in any order).
  const m = /<input\b[^>]*id="mail-link-deal-include-closed"[^>]*>/.exec(indexHtml);
  assert.ok(m, 'the checkbox markup is missing');
  assert.ok(!/\schecked/.test(m[0]),
    'the checkbox must not be pre-checked - the default search is open-only');
  assert.ok(/type="checkbox"/.test(m[0]), 'the control must be a checkbox');
});

test('the checkbox lives inside the mail link dropdown', () => {
  const start = indexHtml.indexOf('id="mail-link-deal-dropdown"');
  assert.ok(start > -1, 'the link dropdown is missing');
  const block = indexHtml.slice(start, start + 1200);
  assert.ok(block.includes('id="mail-link-deal-include-closed"'),
    'the checkbox must be inside the link dropdown');
});

test('ticking the checkbox re-runs the search', () => {
  const at = appJs.indexOf('const dealIncludeClosed = $("#mail-link-deal-include-closed");');
  assert.ok(at > -1, 'the checkbox listener is missing');
  const region = appJs.slice(at, at + 1500);
  assert.ok(region.includes('dealIncludeClosed.addEventListener'), 'no change listener');
  assert.ok(region.includes('searchDealsForMailLink(q)'),
    'toggling the checkbox must re-run the search');
});

test('the search passes include_closed to the backend', () => {
  const body = fnBody(appJs, 'searchDealsForMailLink');
  assert.ok(body.includes('#mail-link-deal-include-closed'),
    'the search must read the checkbox state');
  assert.ok(body.includes('includeClosed'), 'the search must forward includeClosed');
});

test('the shared search helper only sends include_closed when asked', () => {
  const body = fnBody(appJs, 'searchOpportunitiesByTitle');
  assert.ok(body.includes('params.set("include_closed", "true")'),
    'the helper must set include_closed on the request');
  // The option defaults to false so every other caller is unaffected.
  assert.ok(/includeClosed\s*=\s*false/.test(appJs),
    'searchOpportunitiesByTitle must default includeClosed to false');
});

test('a closed result renders a "Closed" badge next to its name', () => {
  const body = fnBody(appJs, 'searchDealsForMailLink');
  assert.ok(body.includes('mail-link-deal-closed-badge'), 'the Closed badge is missing');
  assert.ok(body.includes('badge.textContent = "Closed"'), 'the badge must read "Closed"');
  // The badge is rendered next to the title, not instead of it.
  assert.ok(body.includes('document.createTextNode(opp.title)'),
    'the closed row must still show the project name');
});

test('an open result is unchanged (plain title text)', () => {
  const body = fnBody(appJs, 'searchDealsForMailLink');
  assert.ok(body.includes('b.textContent = opp.title;'),
    'the open row must still be plain title text');
});

test('the "Closed" badge and the checkbox are styled', () => {
  // Require the exact class selector, not just a substring match (a
  // renamed class would otherwise still satisfy a naive `includes`).
  assert.ok(/\.mail-link-deal-closed-badge\s*\{/.test(stylesCss),
    'the badge must be styled');
  assert.ok(/\.mail-link-deal-closed-toggle\s*\{/.test(stylesCss),
    'the toggle must be styled');
  // The generic dropdown `input` rule is width:100%, which would stretch
  // the checkbox - it needs an explicit override.
  assert.ok(
    /\.mail-link-deal-closed-toggle input\[type="checkbox"\]\s*\{[^}]*width:\s*auto/.test(stylesCss),
    'the checkbox must not inherit width:100% from the dropdown input rule'
  );
});

test('the popup resets the checkbox back to unchecked', () => {
  const body = fnBody(appJs, 'resetLinkDealDropdown');
  assert.ok(body.includes('#mail-link-deal-include-closed'), 'the reset must clear the checkbox');
  assert.ok(body.includes('ic.checked = false'), 'the checkbox must be reset to unchecked');
});

test('linking uses the same endpoint for open and closed projects', () => {
  const at = appJs.indexOf('const linkSubmitBtn = $("#mail-link-deal-submit")');
  assert.ok(at > -1, 'the bulk link button must exist');
  const region = appJs.slice(at, at + 2500);
  assert.ok(region.includes('/api/v2/mail/messages/${mid}/link'), 'the link endpoint');
  assert.ok(region.includes('opportunityId: Number(dealId)'), 'the link payload');
  // There must be no closed-specific branch in the link path - a closed
  // project links exactly like an open one.
  assert.ok(!region.includes('isClosed'), 'the link path must not branch on closed');
});

// ── Fix 2: the bookmark preview shows the project ID ───────────────────────

test('a shared project-number badge renderer exists', () => {
  assert.ok(appJs.includes('function renderProjectNumberBadge('),
    'renderProjectNumberBadge must exist');
});

test('the badge uses the same format as the full project page', () => {
  const body = fnBody(appJs, 'renderProjectNumberBadge');
  assert.ok(body.includes('`#${projNum}`'), 'the badge must read #<project number>');
  assert.ok(body.includes('opp-project-number-badge'), 'the badge class must match the project page');
  // Same field: project_number, falling back to the opportunity id.
  assert.ok(body.includes('projectNumber || oppId'), 'the badge must fall back to the opp id');
});

test('the badge is inserted before the header action buttons', () => {
  const body = fnBody(appJs, 'renderProjectNumberBadge');
  assert.ok(body.includes('bookmark-preview-actions'),
    'the bookmark header actions must be a known insertion point');
  assert.ok(body.includes('insertBefore(badge, actionsEl)'),
    'the badge must be placed next to the title, before the action buttons');
});

test('the project preview modal uses the shared renderer', () => {
  const body = fnBody(appJs, 'openOpportunityPreviewModal');
  assert.ok(body.includes('renderProjectNumberBadge(titleEl, data.opp.project_number, id)'),
    'the project preview modal must use the shared badge renderer');
});

test('the bookmark preview shows the project number in every render path', () => {
  // cached path + freshly fetched path
  const body = fnBody(appJs, 'activateBookmarkTab');
  assert.ok(body.includes('renderProjectNumberBadge(titleEl, deal._cachedData.opp?.project_number, id)'),
    'the cached bookmark path must render the badge');
  assert.ok(body.includes('renderProjectNumberBadge(titleEl, data.opp?.project_number, id)'),
    'the fetched bookmark path must render the badge');
  // explicit refresh path
  const refresh = fnBody(appJs, 'refreshBookmarkTab');
  assert.ok(refresh.includes('renderProjectNumberBadge(titleEl, data.opp?.project_number, id)'),
    'the bookmark refresh path must render the badge');
});

// ── Backend flag ───────────────────────────────────────────────────────────

test('the backend accepts an opt-in include_closed flag', () => {
  assert.ok(serverPy.includes('def _query_bool('), '_query_bool helper must exist');
  assert.ok(serverPy.includes('_query_bool(qs, "include_closed", "includeClosed")'),
    '_build_projects_where must read the include_closed flag');
});

test('the backend default is unchanged (equality filter)', () => {
  const at = serverPy.indexOf('def _build_projects_where');
  const body = serverPy.slice(at, at + 6000);
  assert.ok(body.includes('where.append("o.stage_type = %s")'),
    'the default branch must keep the equality filter');
  assert.ok(body.includes('o.stage_type = ANY(%s)'),
    'the widened branch must use ANY so closed stages can be included');
});
