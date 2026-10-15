#!/usr/bin/env node
// Regression tests for the user-reported round 2 fixes (2026-10-14):
//
// - Issue 1: checkbox fields render as a real checkbox icon, never as
//   the text "Yes" / "No" pill (even when the value is a raw
//   "true" / "false" string from legacy / misclassified fields).
// - Issue 2: the contacts tab in the search popup no longer has an
//   Edit button (which previously opened the admin console on the
//   "general" tab).
// - Issue 3: contact history is now pulled from OnlyOffice during
//   the contact sync (entityType=contact). Verified via source-grep
//   because a full end-to-end sync test requires a live OnlyOffice.
// - Issue 4: contact photos are served from avatar_local_path first,
//   so synced contacts with a locally-downloaded avatar actually
//   display it (previously 404'd because the handler derived the path
//   from the remote avatar_url).
// - Issue 5: project page highlight respects dark mode (light +
//   dark CSS rules).
//
// Run with: node --test tests/test_user_round2_fixes.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const projectHtml = readFileSync(join(repoRoot, 'public/project.html'), 'utf8');

// --- Issue 1: checkbox fields render as a real icon, never "Yes"/"No" text ---

test('Issue 1: buildFieldElement no longer creates a field-value-tag for Yes/No', () => {
  // Locate buildFieldElement and verify it no longer creates a
  // <span class="field-value-tag">Yes</span> (or "No") text pill for
  // checkbox-like values. The fix is to always draw the icon.
  const fnStart = appJs.indexOf('function buildFieldElement(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    !/field-value-tag/.test(body),
    'buildFieldElement still creates a field-value-tag span (audit: should always draw the icon)'
  );
  // The legacy "Yes" / "No" strings are still produced by the
  // formatter, but the renderer must never display them as text.
  assert.ok(
    !/["']Yes["']/.test(body) || !/row\.value\s*===\s*["']Yes["']/.test(body),
    'buildFieldElement still has the row.value === "Yes" rendering branch'
  );
  // The renderer should detect boolean-like values via a helper.
  assert.ok(
    /_looksLikeCheckboxValue/.test(body),
    'buildFieldElement should call _looksLikeCheckboxValue to detect legacy boolean values'
  );
});

test('Issue 1: formatCustomFieldValueForDisplay no longer normalizes legacy booleans', () => {
  // Audit: previously this helper normalized "true" -> "Yes" / "false"
  // -> "No" for non-code-3 fields, which then made the renderer
  // display the literal "Yes" / "No" text. The fix is to let the
  // raw value pass through; the renderer detects booleans and draws
  // the icon.
  const fnStart = appJs.indexOf('function formatCustomFieldValueForDisplay(');
  const fnEnd = appJs.indexOf('\n}\n', fnStart);
  const body = appJs.slice(fnStart, fnEnd);
  assert.ok(
    !/_looksLikeCheckboxValue/.test(body),
    'formatCustomFieldValueForDisplay still calls _looksLikeCheckboxValue (should be removed)'
  );
});

test('Issue 1: buildFieldElement uses the checkbox SVG icon (and square SVG for unchecked)', () => {
  // Both SVGs must exist in the production code (renderProjectFieldsDisplay
  // closure contains the _checkboxIcon helper).
  assert.ok(
    /icon-tabler-checkbox/.test(appJs),
    'icon-tabler-checkbox (checked) SVG missing from app.js'
  );
  assert.ok(
    /icon-tabler-square/.test(appJs),
    'icon-tabler-square (unchecked) SVG missing from app.js'
  );
});

// --- Issue 2: contact edit button removed from the search modal ---

test('Issue 2: search popup no longer has the contact edit button', () => {
  // The class name was .search-popup-preview-contact-edit (distinct
  // from the project preview's .search-popup-preview-edit). Both must
  // be gone for contacts.
  assert.ok(
    !/search-popup-preview-contact-edit/.test(appJs),
    'search popup still has the .search-popup-preview-contact-edit class'
  );
  // The handler that opened openAdminConsoleModal("contacts") is gone.
  assert.ok(
    !/openAdminConsoleModal\(["']contacts["']\)/.test(appJs),
    'app.js still has the openAdminConsoleModal("contacts") call'
  );
  // The .search-popup-preview-actions block is still present (used by
  // the project preview at line ~29023), but inside the contact
  // preview container the action block should be gone (we just
  // removed the whole <div class="search-popup-preview-actions">...</div>).
  // Verify by ensuring the contact tab render no longer contains an
  // action block.
  const contactPreviewMatch = appJs.match(
    /tabContent = tabKey;\s*container\.style\.display = "none";[\s\S]{0,2000}search-popup-preview-body"/
  );
  if (contactPreviewMatch) {
    assert.ok(
      !/search-popup-preview-actions/.test(contactPreviewMatch[0]),
      'contact preview container still has .search-popup-preview-actions'
    );
  }
});

// --- Issue 3: contact history is now pulled in the sync (server.py) ---

test('Issue 3: contact sync fetches history via entityType=contact', () => {
  // The new third pass in _handle_sync_pull_contacts (server.py) should
  // issue a GET to the OnlyOffice history endpoint with
  // entityType=contact. This is the server-side change, not a
  // client-side one.
  const serverSrc = readFileSync(join(repoRoot, 'server.py'), 'utf8');
  assert.ok(
    /\/api\/2\.0\/crm\/history\/filter\?entityType=contact/.test(serverSrc) ||
    /entityType=contact&entityId=/.test(serverSrc),
    'server.py contact sync does not call /api/2.0/crm/history/filter?entityType=contact'
  );
});

test('Issue 3: contact history sync writes to history_events with contact_id', () => {
  // The new INSERT must include contact_id in the column list
  // (mirror of the deal-sync pattern that uses opportunity_id).
  // Look for the specific INSERT statement added by this fix.
  const serverSrc = readFileSync(join(repoRoot, 'server.py'), 'utf8');
  // Find the contact-history INSERT block: it should have a column
  // list that starts with contact_id and an INSERT INTO history_events.
  const insertMatch = serverSrc.match(
    /INSERT INTO history_events[\s\S]{0,200}contact_id[\s\S]{0,400}VALUES[\s\S]{0,200}local_id,/
  );
  // Also accept the alternate formatting where the column list spans
  // multiple lines and contact_id is followed by category_id.
  const altMatch = serverSrc.match(
    /INSERT INTO history_events\s*\([\s\S]{0,200}contact_id[\s\S]{0,800}VALUES/
  );
  assert.ok(
    insertMatch || altMatch,
    'server.py contact sync INSERT into history_events does not include contact_id'
  );
});

// --- Issue 4: contact photos served from avatar_local_path first ---

test('Issue 4: _handle_contact_photo_get uses avatar_local_path', () => {
  const serverSrc = readFileSync(join(repoRoot, 'server.py'), 'utf8');
  const fnStart = serverSrc.indexOf('def _handle_contact_photo_get(');
  const fnEnd = serverSrc.indexOf('\n    def ', fnStart);
  const body = serverSrc.slice(fnStart, fnEnd);
  assert.ok(
    /avatar_local_path/.test(body),
    '_handle_contact_photo_get does not use avatar_local_path'
  );
});

test('Issue 4: _handle_contact_photo_get still falls back to avatar_url', () => {
  const serverSrc = readFileSync(join(repoRoot, 'server.py'), 'utf8');
  const fnStart = serverSrc.indexOf('def _handle_contact_photo_get(');
  const fnEnd = serverSrc.indexOf('\n    def ', fnStart);
  const body = serverSrc.slice(fnStart, fnEnd);
  assert.ok(
    /avatar_url/.test(body),
    '_handle_contact_photo_get no longer falls back to avatar_url (should still be the fallback path)'
  );
});

// --- Issue 5: project page highlight — subtle tint, both modes ---

test('Issue 5: highlight opacity is now low (subtle tint, not vivid block)', () => {
  // Audit 2026-10-14 round 3: dropped the vivid 0.65 opacity + forced
  // #000 text color. The user said the previous version was "still
  // too high contrast." Now a single 0.3-opacity tint with text
  // color inherited from the surrounding content.
  assert.ok(
    /rgba\(34,\s*197,\s*94,\s*0\.3\)\s*!important/.test(projectHtml),
    'project.html should use rgba(34, 197, 94, 0.3) for the highlight (subtle tint)'
  );
  // The forced #000 text color is gone — text should inherit.
  assert.ok(
    !/color:\s*#000\s*!important/.test(projectHtml),
    'project.html should not force color: #000 !important on the highlight (it broke dark mode)'
  );
  // The light-only media query is gone — single rule for both modes.
  assert.ok(
    !/@media\s*\(prefers-color-scheme:\s*light\)/.test(projectHtml),
    'project.html should not have a light-only @media block for the highlight'
  );
  // Same fix applied to the dashboard preview (styles.css).
  const stylesCss = readFileSync(join(repoRoot, 'public/styles.css'), 'utf8');
  assert.ok(
    /rgba\(34,\s*197,\s*94,\s*0\.3\)/.test(stylesCss),
    'styles.css .opp-preview-history-reply-body highlight should use 0.3 opacity'
  );
  assert.ok(
    !/color:\s*#000\s*!important/.test(stylesCss),
    'styles.css should not force color: #000 !important on the highlight'
  );
});

// --- Issue 6: checkbox SVG matches font height (≥ 1em) ---

test('Issue 6: checkbox / square SVG is at least 1em tall', () => {
  // Audit 2026-10-14 round 3: bumped from 0.95em to 1.1em — the user
  // said the previous 0.95em was way too small and the icons should
  // "at least match font height."
  const stylesCss = readFileSync(join(repoRoot, 'public/styles.css'), 'utf8');
  // Find the .opp-preview-chk-yes svg / .opp-preview-chk-no svg rule.
  const ruleMatch = stylesCss.match(
    /\.opp-preview-chk-(yes|no)\s+svg\s*\{([^}]+)\}/
  );
  assert.ok(
    ruleMatch,
    'could not find .opp-preview-chk-(yes|no) svg rule in styles.css'
  );
  const body = ruleMatch[2];
  // Must have width: >= 1em and height: >= 1em.
  const widthMatch = body.match(/width:\s*([\d.]+)em/);
  const heightMatch = body.match(/height:\s*([\d.]+)em/);
  assert.ok(widthMatch, 'checkbox SVG width rule missing or not in em');
  assert.ok(heightMatch, 'checkbox SVG height rule missing or not in em');
  assert.ok(
    parseFloat(widthMatch[1]) >= 1.0,
    `checkbox SVG width is ${widthMatch[1]}em, should be ≥ 1.0em`
  );
  assert.ok(
    parseFloat(heightMatch[1]) >= 1.0,
    `checkbox SVG height is ${heightMatch[1]}em, should be ≥ 1.0em`
  );
});
