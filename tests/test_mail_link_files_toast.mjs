// Tests: the mail→project link toast reports how many files were copied.
//
// Bug fix 2026-11-03: linking an email to a project reported plain success
// even when NONE of the email's attachments made it into the project's
// Documents tab. The link API now returns filesCopied / filesTotal, and the
// UI must surface them (warning style on a partial copy).
//
// Run with:  node --test tests/test_mail_link_files_toast.mjs
//
// These are source-level guards on public/app.js (the toast code is inside a
// DOM event handler, so it cannot be unit-tested in isolation without a full
// DOM harness — the repo's existing convention for such code).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.join(HERE, '..', 'public', 'app.js');
const src = fs.readFileSync(APP, 'utf8');

test('the bulk mail-link handler reads filesCopied / filesTotal', () => {
  const at = src.indexOf('const linkSubmitBtn = $("#mail-link-deal-submit")');
  assert.ok(at > -1, 'the bulk link button must exist');
  const region = src.slice(at, at + 4000);
  assert.ok(region.includes('filesCopied'), 'the bulk link must read filesCopied');
  assert.ok(region.includes('filesTotal'), 'the bulk link must read filesTotal');
});

test('the bulk mail-link handler shows "N of M files copied"', () => {
  const at = src.indexOf('const linkSubmitBtn = $("#mail-link-deal-submit")');
  const region = src.slice(at, at + 4000);
  assert.ok(
    region.includes('of ${filesTotal} file'),
    'the toast must state how many of the files were copied'
  );
});

test('the bulk mail-link handler warns on a partial copy', () => {
  const at = src.indexOf('const linkSubmitBtn = $("#mail-link-deal-submit")');
  const region = src.slice(at, at + 4000);
  assert.ok(region.includes('filesPartial'), 'a partial copy must be tracked');
  assert.ok(
    region.includes("filesPartial ? 'warning' : null"),
    'a partial copy must use the warning toast style'
  );
});

test('the single-email link (mail preview) shows the file counts', () => {
  const at = src.indexOf('renderEmailPreviewLinkedDeals');
  assert.ok(at > -1, 'the mail preview link helper must exist');
  // The link handler lives further down in the function.
  const region = src.slice(at, at + 20000);
  // Anchor on the actual link call so the guard cannot be satisfied by an
  // unrelated mention of filesTotal elsewhere in the function.
  const callAt = region.indexOf("/api/v2/mail/messages/${messageId}/link");
  assert.ok(callAt > -1, 'the single link call must exist');
  const linkRegion = region.slice(callAt, callAt + 2500);
  assert.ok(linkRegion.includes('filesTotal'), 'the single link must read filesTotal');
  assert.ok(linkRegion.includes('filesCopied'), 'the single link must read filesCopied');
  assert.ok(
    linkRegion.includes("warn ? 'warning' : null"),
    'the single link must use the warning toast style on a partial copy'
  );
  // The toast TEXT must carry the counts — not just the fields being read.
  assert.ok(
    /msg\s*\+=\s*`[^`]*\$\{res\.filesTotal\}/.test(linkRegion),
    'the single-link toast must state how many of the files were copied'
  );
  assert.ok(
    /warn\s*=\s*copied\s*<\s*res\.filesTotal/.test(linkRegion),
    'a partial copy must set the warning flag'
  );
});

test('the warning toast style exists in the stylesheet', () => {
  const css = fs.readFileSync(path.join(HERE, '..', 'public', 'styles.css'), 'utf8');
  assert.ok(css.includes('.toast.warning'), '.toast.warning must be styled');
});

test('the link API response carries the file counts', () => {
  const server = fs.readFileSync(path.join(HERE, '..', 'server.py'), 'utf8');
  const at = server.indexOf('def _handle_mail_link');
  assert.ok(at > -1, '_handle_mail_link must exist');
  const region = server.slice(at, at + 4000);
  assert.ok(region.includes('filesCopied'), 'the link response must carry filesCopied');
  assert.ok(region.includes('filesTotal'), 'the link response must carry filesTotal');
  assert.ok(region.includes('filesSkipped'), 'the link response must carry filesSkipped');
});
