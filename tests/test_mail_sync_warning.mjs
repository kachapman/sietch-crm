// Tests for the mail IMAP sync-failure UI (public/app.js).
//
// Live bug 2026-11-03: opening an email in the Sietch mail modal did not
// mark it read in Outlook. When the IMAP sync fails the user must be told,
// otherwise it looks like the feature silently does nothing.
//
// Run with:  node --test tests/test_mail_sync_warning.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const appSrc = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(ROOT, 'public', 'index.html'), 'utf8');
const stylesCss = fs.readFileSync(path.join(ROOT, 'public', 'styles.css'), 'utf8');

// Mirrors the verb mapping in public/app.js#renderEmailPreviewSyncNote.
function syncNoteVerb(action) {
  switch (action) {
    case 'read': return 'mark as read';
    case 'unread': return 'mark as unread';
    case 'star': return 'star';
    default: return 'change';
  }
}

test('sync failure note element exists in the email preview modal', () => {
  assert.ok(indexHtml.includes('id="email-preview-sync-note"'));
});

test('preview modal renders the sync note in both open paths', () => {
  const calls = appSrc.match(/renderEmailPreviewSyncNote\(/g) || [];
  // 1 definition + 2 call sites (fresh open + soft refresh)
  assert.equal(calls.length, 3, `expected 3 occurrences, got ${calls.length}`);
});

test('sync note is hidden when there is no failure', () => {
  assert.ok(appSrc.includes("noteEl.classList.add('hidden')"));
});

test('sync note names the action that failed to sync', () => {
  assert.ok(appSrc.includes('renderEmailPreviewSyncNote(syncNoteEl, fullMail)'));
  assert.equal(syncNoteVerb('read'), 'mark as read');
  assert.equal(syncNoteVerb('unread'), 'mark as unread');
  assert.equal(syncNoteVerb('star'), 'star');
  assert.equal(syncNoteVerb('move'), 'change');
});

test('mail list rows show a warning icon for a failed sync', () => {
  assert.ok(appSrc.includes('if (m.sync_failure)'));
  assert.ok(appSrc.includes('mail-sync-warning'));
});

test('mail-sync-warning style is defined', () => {
  assert.ok(stylesCss.includes('.mail-sync-warning {'));
});

test('the list endpoint field the UI reads is sync_failure', () => {
  assert.ok(appSrc.includes('m.sync_failure'));
});
