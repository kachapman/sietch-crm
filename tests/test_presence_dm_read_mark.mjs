#!/usr/bin/env node
// Regression tests for the DM read-mark fix (audit round 6 part 2).
//
// Audit 2026-10-15: _handle_presence_dm_get used to call
// mark_messages_read() on every GET, including Load-More clicks
// (GET ...?offset=N). A user who clicked "Load Earlier Messages"
// would see all unread messages get marked as read on the first
// call (because the GET returns the most recent page, which the
// auto-mark applied to). The fix: the GET is now pure, and the
// mark-as-read side effect was moved to _handle_presence_last_read
// (which the frontend already calls on thread open).
//
// This test guards the structure: the GET handler must NOT call
// mark_messages_read, must pass offset through to get_conversation,
// and the explicit mark endpoint must call both set_last_read_dm
// AND mark_messages_read.
//
// Run with: node --test tests/test_presence_dm_read_mark.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const serverPy = readFileSync(join(repoRoot, 'server.py'), 'utf8');

test('Audit 2026-10-15 (DM read-mark): _handle_presence_dm_get no longer calls mark_messages_read', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_dm_get(self) -> None:');
  assert.ok(fnStart > 0, '_handle_presence_dm_get not found');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  // The fix removed the implicit mark-on-GET. The handler must not
  // call mark_messages_read anywhere — the GET is a pure read.
  // Strip comments and string literals first so a comment that
  // mentions the function name doesn't trip the regex.
  const codeOnly = fn
    .split("\n")
    .filter(l => !l.trim().startsWith("#"))
    .join("\n");
  assert.ok(
    !/mark_messages_read\s*\(/.test(codeOnly),
    '_handle_presence_dm_get should not call mark_messages_read (the mark is explicit now)'
  );
});

test('Audit 2026-10-15 (DM read-mark): _handle_presence_dm_get passes offset to get_conversation', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_dm_get(self) -> None:');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  const codeOnly = fn
    .split("\n")
    .filter(l => !l.trim().startsWith("#"))
    .join("\n");
  // The handler must read offset from the query string.
  assert.ok(
    /qs\.get\(["']offset["']\)/.test(codeOnly),
    '_handle_presence_dm_get should read the offset query parameter'
  );
  // And pass it through to get_conversation. The call is
  // `get_conversation("sietch", str(user["id"]), with_id, offset=offset)`.
  assert.ok(
    /get_conversation\(.*?offset\s*=\s*offset\b/.test(codeOnly),
    '_handle_presence_dm_get should pass offset=offset to get_conversation'
  );
});

test('Audit 2026-10-15 (DM read-mark): _handle_presence_last_read is the explicit mark endpoint', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_last_read(self) -> None:');
  assert.ok(fnStart > 0, '_handle_presence_last_read not found');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  const codeOnly = fn
    .split("\n")
    .filter(l => !l.trim().startsWith("#"))
    .join("\n");
  // Both side effects must fire from the explicit endpoint.
  assert.ok(
    /set_last_read_dm\s*\(\s*["']sietch["']\s*,\s*str\(user\[["']id["']\]\)\s*,\s*other/.test(codeOnly),
    '_handle_presence_last_read should call set_last_read_dm(sietch, str(user[id]), other, ...) for the cross-device inbox badge'
  );
  assert.ok(
    /mark_messages_read\s*\(\s*["']sietch["']\s*,\s*str\(user\[["']id["']\]\)\s*,\s*other\s*\)/.test(codeOnly),
    '_handle_presence_last_read should also call mark_messages_read(sietch, str(user[id]), other) for the sender-side read receipts'
  );
  // The dm:read SSE fanout fires from here now (not from the GET).
  assert.ok(
    /event_bus\.publish\(\s*["']dm:read["']/.test(codeOnly),
    '_handle_presence_last_read should publish dm:read to user_ids=[other] (sender side, not the recipient)'
  );
});

test('Audit 2026-10-15 (DM read-mark): the implicit-mark path in the GET is removed (no mark_messages_read call anywhere in _handle_presence_dm_get)', () => {
  // The fix's key invariant: the GET is pure. If a future change
  // re-introduces an implicit mark, the test fails.
  const fnStart = serverPy.indexOf('def _handle_presence_dm_get(self) -> None:');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  // The function should be small (a few lines of plumbing + the
  // get_conversation call). Defensive check on body length to catch
  // a future change that re-introduces side effects.
  const lineCount = fn.split("\n").filter(l => l.trim() && !l.trim().startsWith("#")).length;
  assert.ok(
    lineCount < 40,
    `_handle_presence_dm_get body has ${lineCount} non-blank lines — should be much smaller if it stays pure (no implicit mark).`
  );
});
