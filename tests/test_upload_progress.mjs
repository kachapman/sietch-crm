// Tests for batch upload progress (2026-10-22).
//
// Project Photos tab, project Documents tab, and the standalone Documents
// modal upload sequentially with no feedback. A shared helper renders the
// mockup: centered strobing bar + "Uploading i/n" + confirmed/total bytes
// (per completed file — fetch exposes no byte events).
//
// Run with:  node --test tests/test_upload_progress.mjs
//
// Mirrors uploadProgressFrame/formatUploadBytes from public/app.js. If you
// change one, change the other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function formatUploadBytes(n) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = Math.max(0, Number(n) || 0), u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[u]}`;
}
function uploadProgressFrame(done, total, doneBytes, totalBytes) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return {
    pct,
    label: total > 0 ? `Uploading ${done}/${total}` : 'Uploading…',
    bytes: `${formatUploadBytes(doneBytes)} / ${formatUploadBytes(totalBytes)}`,
  };
}

test('byte formatting', () => {
  assert.equal(formatUploadBytes(0), '0.0 B');
  assert.equal(formatUploadBytes(512), '512 B');
  assert.equal(formatUploadBytes(157.3 * 1024 * 1024), '157 MB');
  assert.equal(formatUploadBytes(2 * 1024 * 1024 * 1024), '2.0 GB');
  assert.equal(formatUploadBytes(99.5 * 1024), '99.5 KB');
});

test('mockup case: 8 of 12 with byte counter', () => {
  const st = uploadProgressFrame(8, 12, 8 * 10 * 1024 * 1024, 157.3 * 1024 * 1024);
  assert.equal(st.pct, 67);
  assert.equal(st.label, 'Uploading 8/12');
  assert.ok(st.bytes.includes('/ 157 MB'), st.bytes);
});

test('complete and empty states', () => {
  assert.equal(uploadProgressFrame(12, 12, 100, 100).pct, 100);
  assert.equal(uploadProgressFrame(0, 0, 0, 0).label, 'Uploading…');
  assert.ok(uploadProgressFrame(13, 12, 0, 0).pct <= 100);
});

test('app.js routes all three uploaders through the helper', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  for (const fn of ['uploadProgressFrame', 'renderUploadProgress', 'clearUploadProgress', 'formatUploadBytes']) {
    assert.ok(src.includes(`function ${fn}`), `${fn} missing in app.js`);
  }
  const uses = (src.match(/renderUploadProgress\(/g) || []).length;
  // 1 definition + 3 loops x (initial + per-file) updates, minus defs counted
  // separately: assert at least the three call sites exist.
  assert.ok(uses >= 7, `expected >=7 renderUploadProgress calls, found ${uses}`);
});

test('strobe animation exists in styles (or degrades silently)', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const css = fs.readFileSync(path.join(here, '..', 'public', 'styles.css'), 'utf8');
  assert.ok(css.includes('@keyframes upload-strobe'), 'strobe keyframes missing');
  assert.ok(css.includes('prefers-reduced-motion'), 'reduced-motion fallback missing');
});
