#!/usr/bin/env node
// Regression tests for the project modal fixes (2026-10-14).
//
// - Fix E: edit modal title bar removes "Open Project" link + project
//   name and adds an X close button.
// - Fix C: edit modal attaches to the left of the bookmark sidebar /
//   search popup modal (not just the regular preview modal).
// - Fix D: edit / quick-note modal auto-closes when its parent
//   project modal closes (preview, search popup, bookmark).
// - Fix F: checkbox fields render as a checkbox SVG icon (not "true" text).
// - Prior Fix 1: project page reply toolbar has emoji/link/color.
// - Prior Fix 2: add tile modal has no Notes / To-do option.
//
// Run with: node --test tests/test_project_modal_fixes.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'public/index.html'), 'utf8');
const projectHtml = readFileSync(join(repoRoot, 'public/project.html'), 'utf8');
const stylesCss = readFileSync(join(repoRoot, 'public/styles.css'), 'utf8');

// --- Fix E: edit modal title bar ---

test('Fix E: deal-edit modal no longer has #deal-edit-crm-link', () => {
  assert.ok(
    !indexHtml.includes('id="deal-edit-crm-link"'),
    'deal-edit modal still has the removed #deal-edit-crm-link'
  );
});

test('Fix E: deal-edit modal has new X close button', () => {
  const editBlock = indexHtml.match(
    /id="deal-edit-modal"[\s\S]*?<div id="quick-note-modal"|id="deal-edit-modal"[\s\S]*?<\/div>\s*<\/div>\s*<div id="quick-note-modal"/
  );
  const block = editBlock ? editBlock[0] : '';
  assert.ok(block, 'could not locate deal-edit modal block');
  assert.ok(
    block.includes('data-deal-edit-dismiss'),
    'X button missing data-deal-edit-dismiss'
  );
  assert.ok(
    block.includes('modal-header-control'),
    'X button missing modal-header-control class'
  );
  assert.ok(
    /<i\s+class="ti ti-x"\s*><\/i>/.test(block),
    'X button missing ti ti-x icon'
  );
});

test('Fix E: openDealEditModal no longer sets crmLink', () => {
  const fnStart = appJs.indexOf('async function openDealEditModal(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    !body.includes('deal-edit-crm-link'),
    'openDealEditModal still references removed #deal-edit-crm-link'
  );
});

test('Fix E: quick-note modal no longer has #quick-note-crm-link', () => {
  assert.ok(
    !indexHtml.includes('id="quick-note-crm-link"'),
    'quick-note modal still has the removed #quick-note-crm-link'
  );
});

test('Fix E: quick-note modal has new X close button', () => {
  assert.ok(
    /id="quick-note-modal"[\s\S]*?data-quick-note-dismiss[\s\S]*?ti ti-x/.test(indexHtml),
    'quick-note modal X close button missing'
  );
});

test('Fix E: clearQuickNoteOpportunitySelection no longer references crmLink', () => {
  const fnStart = appJs.indexOf('function clearQuickNoteOpportunitySelection(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    !body.includes('quick-note-crm-link'),
    'clearQuickNoteOpportunitySelection still references removed #quick-note-crm-link'
  );
});

test('Fix E: updateQuickNoteOpportunitySelectedUi no longer references crmLink', () => {
  const fnStart = appJs.indexOf('function updateQuickNoteOpportunitySelectedUi(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    !body.includes('quick-note-crm-link'),
    'updateQuickNoteOpportunitySelectedUi still references removed #quick-note-crm-link'
  );
});

// --- Fix C: edit modal attaches to bookmark + search popup ---

test('Fix C: openDealEditModal calls bookmark layout for active bookmark tab', () => {
  const fnStart = appJs.indexOf('async function openDealEditModal(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    body.includes('layoutSideBySideDealEditAndBookmark'),
    'openDealEditModal does not call layoutSideBySideDealEditAndBookmark'
  );
  assert.ok(
    /state\.activeBookmarkTab/.test(body),
    'openDealEditModal does not check state.activeBookmarkTab'
  );
});

test('Fix C: openDealEditModal calls search-popup layout when search popup is open', () => {
  const fnStart = appJs.indexOf('async function openDealEditModal(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    body.includes('layoutSideBySideDealEditAndSearchPopup'),
    'openDealEditModal does not call layoutSideBySideDealEditAndSearchPopup'
  );
  assert.ok(
    body.includes('search-popup-modal'),
    'openDealEditModal does not reference #search-popup-modal'
  );
});

test('Fix C: openQuickNoteModal also calls bookmark + search-popup layouts', () => {
  const fnStart = appJs.indexOf('async function openQuickNoteModal(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    body.includes('layoutSideBySideDealEditAndBookmark'),
    'openQuickNoteModal does not call layoutSideBySideDealEditAndBookmark'
  );
  assert.ok(
    body.includes('layoutSideBySideDealEditAndSearchPopup'),
    'openQuickNoteModal does not call layoutSideBySideDealEditAndSearchPopup'
  );
});

test('Fix C: deal-edit form is now the scroll container', () => {
  assert.ok(
    /\.modal-card-deal-edit \.modal-form\s*\{[\s\S]*?flex:\s*1 1 auto[\s\S]*?overflow-y:\s*auto/.test(stylesCss),
    'deal-edit .modal-form is not set as a flex scroll container'
  );
});

// --- Fix D: edit / quick-note modal auto-close ---

test('Fix D: _doClosePreviewModal closes deal-edit when attached', () => {
  const fnStart = appJs.indexOf('function _doClosePreviewModal(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /closeDealEditModal\(\)/.test(body),
    '_doClosePreviewModal does not call closeDealEditModal'
  );
  assert.ok(
    /closeQuickNoteModal\(\)/.test(body),
    '_doClosePreviewModal does not call closeQuickNoteModal'
  );
});

test('Fix D: closeSearchPopupModal closes deal-edit + quick-note', () => {
  const fnStart = appJs.indexOf('function closeSearchPopupModal(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /closeDealEditModal\(\)/.test(body),
    'closeSearchPopupModal does not call closeDealEditModal'
  );
  assert.ok(
    /closeQuickNoteModal\(\)/.test(body),
    'closeSearchPopupModal does not call closeQuickNoteModal'
  );
});

test('Fix D: closeBookmarkPreview closes deal-edit + quick-note when same oppId', () => {
  const fnStart = appJs.indexOf('function closeBookmarkPreview(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /closeDealEditModal\(\)/.test(body),
    'closeBookmarkPreview does not call closeDealEditModal'
  );
  assert.ok(
    /closeQuickNoteModal\(\)/.test(body),
    'closeBookmarkPreview does not call closeQuickNoteModal'
  );
  assert.ok(
    /activeBookmarkTab/.test(body) && /dealEdit/.test(body),
    'closeBookmarkPreview should compare state.activeBookmarkTab to state.dealEdit.oppId'
  );
});

// --- Fix F: checkbox rendering ---

test('Fix F: formatCustomFieldValueForDisplay no longer pre-normalizes booleans', () => {
  // Audit 2026-10-14: the prior version normalized "true" -> "Yes" /
  // "false" -> "No" inside the formatter, which then made the
  // renderer display the literal "Yes" / "No" text pill instead of
  // drawing a checkbox icon. The fix is to let the raw value pass
  // through; the renderer (buildFieldElement) detects boolean-like
  // values and draws the icon. This test guards against regressing
  // back to pre-normalization.
  const fnStart = appJs.indexOf('function formatCustomFieldValueForDisplay(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    !/_looksLikeCheckboxValue/.test(body),
    'formatCustomFieldValueForDisplay should NOT call _looksLikeCheckboxValue (renderer handles it)'
  );
  // The formatter should still translate proper code-3 "true"/"1" to
  // "Yes" (the existing behavior) — but it should NOT do that for
  // non-code-3 fields.
  assert.ok(
    /code === 3/.test(body),
    'formatCustomFieldValueForDisplay should still handle code === 3 (the standard checkbox type)'
  );
});

test('Fix F: _looksLikeCheckboxValue recognizes boolean-like strings', () => {
  assert.ok(
    /function _looksLikeCheckboxValue/.test(appJs),
    '_looksLikeCheckboxValue helper missing'
  );
  // The function should at least match true, false, yes, no, 1, 0.
  const fnStart = appJs.indexOf('function _looksLikeCheckboxValue(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  for (const word of ['"true"', '"false"', '"yes"', '"no"', '"1"', '"0"']) {
    assert.ok(
      body.includes(word),
      `_looksLikeCheckboxValue does not check for ${word}`
    );
  }
});

test('Fix F: buildFieldElement uses checkbox SVG icon (not text glyph)', () => {
  // The SVG markup lives in the _checkboxIcon helper (a sibling
  // closure inside renderProjectFieldsDisplay) and the buildFieldElement
  // function calls into it. Look at both.
  const renderStart = appJs.indexOf('function renderProjectFieldsDisplay(');
  const renderEnd = appJs.indexOf('\n}\n', renderStart);
  const block = appJs.slice(renderStart, renderEnd);
  assert.ok(
    /icon-tabler-checkbox/.test(block),
    'renderProjectFieldsDisplay does not contain the checkbox SVG icon'
  );
  assert.ok(
    /icon-tabler-square/.test(block),
    'renderProjectFieldsDisplay does not contain the square (unchecked) SVG icon'
  );
  const fnStart = appJs.indexOf('function buildFieldElement(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const buildBody = appJs.slice(fnStart, fnEnd);
  assert.ok(
    /_checkboxIcon/.test(buildBody),
    'buildFieldElement does not call _checkboxIcon helper'
  );
  // The old ✓/✗ text-glyph fallback should be removed.
  assert.ok(
    !buildBody.includes('\\u2713'),
    'buildFieldElement still has the old ✓ text-glyph fallback'
  );
  assert.ok(
    !buildBody.includes('\\u2717'),
    'buildFieldElement still has the old ✗ text-glyph fallback'
  );
});

test('Fix F: CSS sizes the checkbox icons', () => {
  assert.ok(
    /\.opp-preview-chk-(yes|no)\s+svg\s*\{[\s\S]*?width:\s*0\.95em/.test(stylesCss),
    'CSS does not size the new checkbox SVGs (0.95em)'
  );
});

// --- Prior Fix 1: project page reply toolbar ---

test('Prior Fix 1: project.html event-reply toolbar has emoji button', () => {
  const replyMarkup = projectHtml.match(
    /event-reply-editor[\s\S]*?<\/div>\s*<\/div>/g
  ) || [];
  const found = replyMarkup.some((block) =>
    block.includes('note-format-emoji-btn')
  );
  assert.ok(found, 'event-reply toolbar does not include emoji button');
});

test('Prior Fix 1: project.html event-reply toolbar has link button', () => {
  const replyMarkup = projectHtml.match(
    /event-reply-editor[\s\S]*?<\/div>\s*<\/div>/g
  ) || [];
  const found = replyMarkup.some((block) =>
    block.includes('note-format-link-btn')
  );
  assert.ok(found, 'event-reply toolbar does not include link button');
});

test('Prior Fix 1: project.html event-reply toolbar has color button', () => {
  const replyMarkup = projectHtml.match(
    /event-reply-editor[\s\S]*?<\/div>\s*<\/div>/g
  ) || [];
  const found = replyMarkup.some((block) =>
    block.includes('note-format-color-btn')
  );
  assert.ok(found, 'event-reply toolbar does not include color button');
});

test('Prior Fix 1: project.html has inline handlers for emoji/link/color', () => {
  assert.ok(
    /note-format-emoji-btn[\s\S]{0,2000}insertText/.test(projectHtml),
    'no inline emoji handler in project.html'
  );
  assert.ok(
    /note-format-link-btn[\s\S]{0,2000}createLink/.test(projectHtml),
    'no inline link handler in project.html'
  );
  assert.ok(
    /note-format-color-btn[\s\S]{0,2000}foreColor/.test(projectHtml),
    'no inline color handler in project.html'
  );
});

// --- Prior Fix 2: add tile modal ---

test('Prior Fix 2: add tile modal no longer has Notes / To-do button', () => {
  // Find the add-tile modal block, then look for the removed button id.
  const addTileBlock = indexHtml.match(
    /id="add-tile-modal"[\s\S]*?<\/div>\s*<\/div>\s*<div id="add-tile-/
  );
  const block = addTileBlock ? addTileBlock[0] : '';
  assert.ok(block, 'could not locate add-tile modal block');
  assert.ok(
    !block.includes('id="add-tile-notes"'),
    'add-tile modal still has #add-tile-notes button'
  );
});

test('Prior Fix 2: app.js no longer wires #add-tile-notes click handler', () => {
  assert.ok(
    !/\$\("#add-tile-notes"\)\?\.addEventListener/.test(appJs),
    'app.js still has #add-tile-notes click handler'
  );
});
