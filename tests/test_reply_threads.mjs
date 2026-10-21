// Guards for reply threads: batch auto-load, composer-close on post, and
// muted mention colors.
//
// Live incident: opening a preview fired one thread-fetch per event against
// a 20-connection pool and 500'd, so threads never loaded. Auto-load paths
// must use the single batch endpoint. Separately, post/delete re-clicked the
// Reply button to reload, which reopens the composer — post/delete must
// refresh the thread with it closed.
//
// Run with: node --test tests/test_reply_threads.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const appJs = readFileSync(join(root, 'public', 'app.js'), 'utf8');
const phtml = readFileSync(join(root, 'public', 'project.html'), 'utf8');
const css = readFileSync(join(root, 'public', 'styles.css'), 'utf8');

test('dashboard auto-load uses the batch endpoint, not per-event fetches', () => {
  const at = appJs.indexOf('// Auto-load threaded replies for all events');
  assert.ok(at > 0);
  const region = appJs.slice(at, at + 1500);
  assert.ok(region.includes('/history/replies'), 'must hit the batch endpoint');
  assert.ok(!region.includes('/history/${eventId}/replies'),
    'per-event fetch in auto-load bursts the pool');
});

test('project page auto-load uses the batch endpoint', () => {
  assert.ok(phtml.includes('/history/replies'),
    'project page must batch thread auto-load');
});

test('post/delete refresh thread without reopening composer (dashboard)', () => {
  assert.ok(appJs.includes('const loadThread = async () => {'),
    'shared thread loader missing');
  const postAt = appJs.indexOf('showToast("Reply added")');
  const postRegion = appJs.slice(Math.max(0, postAt - 600), postAt);
  assert.ok(postRegion.includes('await loadThread()'),
    'post-success must reload via loadThread');
  assert.ok(!postRegion.includes('replyBtn.click()'),
    'post-success must not re-click (reopens composer)');
});

test('post refreshes thread without reopening composer (project page)', () => {
  assert.ok(phtml.includes('var renderReplyThread = function('),
    'shared thread renderer missing');
  assert.ok(!phtml.includes('replyBtn.click(); // Reload replies'),
    'post-success must not re-click (reopens composer)');
});

test('mention colors muted in both implementations', () => {
  assert.ok(appJs.includes('hsl(${hue}, 45%, 60%)'),
    'dashboard mention color must be the muted value');
  assert.ok(phtml.includes('45%, 60%)'), 'project page mention color must be muted');
  assert.ok(!appJs.includes('hsl(${hue}, 55%, 42%)'),
    'old neon violet must be gone from dashboard');
});

test('mention chip fallbacks muted', () => {
  assert.ok(css.includes('hsl(200, 45%, 60%)'), 'styles.css fallback must be muted');
  assert.ok(phtml.includes('hsl(200, 45%, 60%)'), 'project page fallback must be muted');
});
