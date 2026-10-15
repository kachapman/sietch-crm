#!/usr/bin/env node
// Regression tests for the documents modal fixes (2026-10-14):
//
// - Issue 1: documents modal pagination — the server endpoints
//   /api/v2/documents/personal and /api/v2/documents/company now
//   support limit + offset and return a total count; the frontend
//   sends them and renders a pager.
// - Issue 2: sidebar tree "Company gets bumped into the child
//   directory area" — the .documents-folder-tree now has a
//   bottom border + background so it's clearly its own panel,
//   and the scope click handler clears the previous tree's
//   innerHTML to avoid showing stale Company data during the
//   fetch round-trip.
//
// Run with: node --test tests/test_documents_pagination.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const serverPy = readFileSync(join(repoRoot, 'server.py'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'public/styles.css'), 'utf8');

// --- Issue 1: backend pagination ---

test('Issue 1: _handle_documents_personal accepts limit and offset', () => {
  const fnStart = serverPy.indexOf('def _handle_documents_personal(');
  const fnEnd = serverPy.indexOf('\n    def ', fnStart);
  const body = serverPy.slice(fnStart, fnEnd);
  assert.ok(
    /qs\.get\(["']limit["']/.test(body),
    '_handle_documents_personal does not read limit query param'
  );
  assert.ok(
    /qs\.get\(["']offset["']/.test(body),
    '_handle_documents_personal does not read offset query param'
  );
  assert.ok(
    /ORDER BY uploaded_at DESC LIMIT %s OFFSET %s/.test(body),
    '_handle_documents_personal SQL does not apply LIMIT/OFFSET'
  );
  assert.ok(
    /["']total["']:\s*total/.test(body),
    '_handle_documents_personal response does not include total'
  );
});

test('Issue 1: _handle_documents_company accepts limit and offset', () => {
  const fnStart = serverPy.indexOf('def _handle_documents_company(');
  const fnEnd = serverPy.indexOf('\n    def ', fnStart);
  const body = serverPy.slice(fnStart, fnEnd);
  assert.ok(
    /qs\.get\(["']limit["']/.test(body),
    '_handle_documents_company does not read limit query param'
  );
  assert.ok(
    /qs\.get\(["']offset["']/.test(body),
    '_handle_documents_company does not read offset query param'
  );
  assert.ok(
    /ORDER BY uploaded_at DESC LIMIT %s OFFSET %s/.test(body),
    '_handle_documents_company SQL does not apply LIMIT/OFFSET'
  );
  assert.ok(
    /["']total["']:\s*total/.test(body),
    '_handle_documents_company response does not include total'
  );
});

// --- Issue 1: frontend sends limit + offset ---

test('Issue 1: loadDocumentsList sends limit + offset for personal', () => {
  const fnStart = appJs.indexOf('async function loadDocumentsList(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  const personalSection = body.match(
    /if \(docsState\.scope === "mydocs"\)[\s\S]*?else if \(docsState\.scope === "company"\)/
  );
  assert.ok(personalSection, 'loadDocumentsList: cannot find personal scope block');
  assert.ok(
    /pageSize/.test(personalSection[0]),
    'loadDocumentsList personal scope: does not send pageSize as limit'
  );
  assert.ok(
    /offset/.test(personalSection[0]),
    'loadDocumentsList personal scope: does not send offset'
  );
});

test('Issue 1: loadDocumentsList sends limit + offset for company', () => {
  const fnStart = appJs.indexOf('async function loadDocumentsList(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  const companySection = body.match(
    /else if \(docsState\.scope === "company"\)[\s\S]*?else \{/
  );
  assert.ok(companySection, 'loadDocumentsList: cannot find company scope block');
  assert.ok(
    /pageSize/.test(companySection[0]),
    'loadDocumentsList company scope: does not send pageSize as limit'
  );
  assert.ok(
    /offset/.test(companySection[0]),
    'loadDocumentsList company scope: does not send offset'
  );
});

test('Issue 1: loadDocumentsList captures total from response', () => {
  const fnStart = appJs.indexOf('async function loadDocumentsList(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /docsState\.total\s*=\s*typeof data\.total === "number"/.test(body),
    'loadDocumentsList does not capture docsState.total from response'
  );
});

// --- Issue 1: pager UI ---

test('Issue 1: renderDocumentsList renders a pager when total > pageSize', () => {
  const fnStart = appJs.indexOf('function renderDocumentsList(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /documents-pager-prev/.test(body),
    'renderDocumentsList does not render the prev/next pager buttons'
  );
  assert.ok(
    /documents-pager-next/.test(body),
    'renderDocumentsList does not render the next pager button'
  );
  assert.ok(
    /Page \$\{page \+ 1\} \/ \$\{pageCount\}/.test(body),
    'renderDocumentsList does not render the page X / Y label'
  );
});

test('Issue 1: pager is only shown when total > pageSize', () => {
  const fnStart = appJs.indexOf('function renderDocumentsList(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  const pagerBlock = body.match(/if \(total > pageSize\)\s*\{[\s\S]*?documents-pager/);
  assert.ok(pagerBlock, 'pager rendering is not gated on total > pageSize');
});

test('Issue 1: pager buttons have working click handlers', () => {
  const fnStart = appJs.indexOf('function bindDocumentsListEvents(');
  // The function is large; take a generous slice.
  const events = appJs.slice(fnStart, fnStart + 10000);
  assert.ok(
    /documents-pager-prev/.test(events) && /docsState\.page -= 1/.test(events),
    'pager prev button is not wired to decrement docsState.page'
  );
  assert.ok(
    /documents-pager-next/.test(events) && /docsState\.page \+= 1/.test(events),
    'pager next button is not wired to increment docsState.page'
  );
});

test('Issue 1: docsState.page resets on scope change + folder nav + search', () => {
  // 1. openDocumentsModal reset
  const openFn = appJs.slice(
    appJs.indexOf('function openDocumentsModal('),
    appJs.indexOf('function openDocumentsModal(') + 2000,
  );
  assert.ok(
    /docsState\.page\s*=\s*0/.test(openFn),
    'openDocumentsModal does not reset docsState.page = 0'
  );
  // 2. scope click reset
  // Find the click handler (the one that has `forEach(btn => {` with
  // a body, not the simple `forEach(b => b.classList.toggle(...))`).
  const scopePos = appJs.indexOf('document.querySelectorAll(".documents-scope-btn").forEach(btn => {');
  const bindFn = appJs.slice(scopePos, scopePos + 2500);
  assert.ok(
    scopePos > 0 && /docsState\.page\s*=\s*0/.test(bindFn),
    'scope click handler does not reset docsState.page = 0'
  );
  // 3. search input reset
  const searchPos = appJs.indexOf('docsState.searchQuery = searchInput.value.trim()');
  const searchFn = appJs.slice(searchPos, searchPos + 500);
  assert.ok(
    /docsState\.page\s*=\s*0/.test(searchFn),
    'search input handler does not reset docsState.page = 0'
  );
  // 4. folder nav reset — count all `docsState.page = 0` occurrences
  // inside the navigateTo* functions and the renderFolderTree tree-row
  // click handler. Expected 4 (3 navigateTo* + 1 tree-row click).
  const fnA = appJs.indexOf('function navigateToFolder(');
  const fnB = appJs.indexOf('function navigateToRoot(');
  const fnC = appJs.indexOf('function navigateToBreadcrumb(');
  const fnD = appJs.indexOf('function updateFolderTreeHighlight(');
  assert.ok(fnA > 0 && fnB > 0 && fnC > 0 && fnD > 0, 'could not locate folder-nav functions');
  const folderNavSlice = appJs.slice(fnA, fnD);
  const matches = folderNavSlice.match(/docsState\.page\s*=\s*0/g) || [];
  assert.ok(
    matches.length >= 4,
    `folder nav + tree-row click: expected at least 4 docsState.page = 0 resets, found ${matches.length}`
  );
});

test('Issue 1: docsState has page + pageSize + total fields', () => {
  const stateMatch = appJs.match(
    /const docsState = \{([\s\S]*?)\n\};/
  );
  assert.ok(stateMatch, 'docsState declaration not found');
  const body = stateMatch[1];
  assert.ok(/\bpage:\s*0/.test(body), 'docsState.page not initialized');
  assert.ok(/\bpageSize:\s*200/.test(body), 'docsState.pageSize not initialized to 200');
  assert.ok(/\btotal:\s*0/.test(body), 'docsState.total not initialized');
});

// --- Issue 1: pager CSS ---

test('Issue 1: .documents-pager has visual styling', () => {
  const cssBlock = stylesCss.match(/\.documents-pager\s*\{[^}]+\}/);
  assert.ok(cssBlock, '.documents-pager CSS rule missing');
  assert.ok(
    /display:\s*flex/.test(cssBlock[0]) || /display:\s*block/.test(cssBlock[0]),
    '.documents-pager CSS has no display rule'
  );
});

// --- Issue 2: sidebar tree visual fix ---

test('Issue 2: .documents-folder-tree has a bottom border', () => {
  // The base rule for .documents-folder-tree is the one with the
  // border / padding / background. There's also a collapsed-state
  // rule earlier in the file (.documents-sidebar-collapsed .documents-folder-tree)
  // that we don't want to match. Use a more specific regex.
  const allBlocks = stylesCss.match(/\.documents-folder-tree\s*\{([^}]+)\}/g) || [];
  // The base rule is the longest one (it has the most properties).
  const baseRule = allBlocks.reduce((a, b) => (b.length > a.length ? b : a), "");
  assert.ok(baseRule, '.documents-folder-tree base CSS rule missing');
  assert.ok(
    /border-top/.test(baseRule),
    '.documents-folder-tree lost its top border'
  );
  assert.ok(
    /border-bottom/.test(baseRule),
    '.documents-folder-tree missing bottom border (the fix)'
  );
  assert.ok(
    /background/.test(baseRule),
    '.documents-folder-tree missing background (the fix)'
  );
});

test('Issue 2: scope click handler clears the previous tree container', () => {
  // Find the click handler (the one with `forEach(btn => {`, not the
  // simple `forEach(b => b.classList.toggle(...))`).
  const scopePos = appJs.indexOf('document.querySelectorAll(".documents-scope-btn").forEach(btn => {');
  const scopeHandler = appJs.slice(scopePos, scopePos + 1800);
  assert.ok(
    /\.documents-folder-tree[\s\S]{0,200}\.innerHTML\s*=\s*["']["']/.test(scopeHandler),
    'scope click handler does not clear the previous tree innerHTML'
  );
});
