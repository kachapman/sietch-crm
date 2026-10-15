#!/usr/bin/env node
// Regression tests for the presence/DM SSE wire-up (audit round 6).
//
// Audit 2026-10-15: the frontend's `case "dm:new":`, `case "dm:read":`,
// and `case "presence:update":` SSE handlers were fully implemented
// but nothing on the server ever published those event types, so the
// handlers were dead code and DMs surfaced only via polling. This
// test guards the structure: each publish point must be present in
// the source, the dead handlers must reach all three DM containers,
// and the presence:update handler must merge into state.presenceData.users
// (the original bug was calling findIndex on the envelope, not the
// users array).
//
// Run with: node --test tests/test_presence_sse_wireup.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const serverPy = readFileSync(join(repoRoot, 'server.py'), 'utf8');

test('Audit 2026-10-15 (presence SSE): server publishes dm:new from _handle_presence_dm_post', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_dm_post(self) -> None:');
  assert.ok(fnStart > 0, '_handle_presence_dm_post not found');
  const fnEnd = serverPy.indexOf('\n    def _handle_timesheet_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  // Must publish dm:new after append_dm.
  assert.ok(
    /event_bus\.publish\(\s*["']dm:new["']/.test(fn),
    '_handle_presence_dm_post should call event_bus.publish("dm:new", ...) after append_dm'
  );
  // Must target only the recipient (the sender's UI already knows).
  assert.ok(
    /event_bus\.publish\(\s*["']dm:new["'][\s\S]{0,400}?user_ids=\[to_id\]/.test(fn),
    '_handle_presence_dm_post should publish dm:new with user_ids=[to_id] (only the recipient)'
  );
  // The publish must not block the response on a failure.
  assert.ok(
    /except\s+Exception:[\s\S]{0,200}?pass/.test(fn),
    '_handle_presence_dm_post should wrap the publish in a try/except so it cannot break the POST response'
  );
});

test('Audit 2026-10-15 (presence SSE): server publishes dm:read from _handle_presence_last_read (explicit mark, not implicit GET)', () => {
  // After the audit round 6 part 2 fix, the implicit mark-on-GET
  // was removed (it was over-marking on Load-More clicks). The
  // dm:read SSE publish now fires from the explicit mark endpoint
  // (_handle_presence_last_read), not from the GET. The
  // _handle_presence_dm_get should NOT publish dm:read anymore.
  const getFnStart = serverPy.indexOf('def _handle_presence_dm_get(self) -> None:');
  assert.ok(getFnStart > 0, '_handle_presence_dm_get not found');
  const getFnEnd = serverPy.indexOf('\n    def _handle_presence_', getFnStart + 1);
  const getFnCode = serverPy.slice(getFnStart, getFnEnd)
    .split("\n").filter(l => !l.trim().startsWith("#")).join("\n");
  assert.ok(
    !/event_bus\.publish\(\s*["']dm:read["']/.test(getFnCode),
    '_handle_presence_dm_get should NOT publish dm:read anymore (it was the implicit mark that over-marked on Load-More)'
  );
  // And the publish IS in the explicit mark endpoint.
  const lastReadFnStart = serverPy.indexOf('def _handle_presence_last_read(self) -> None:');
  assert.ok(lastReadFnStart > 0, '_handle_presence_last_read not found');
  const lastReadFnEnd = serverPy.indexOf('\n    def _handle_presence_', lastReadFnStart + 1);
  const lastReadFn = serverPy.slice(lastReadFnStart, lastReadFnEnd)
    .split("\n").filter(l => !l.trim().startsWith("#")).join("\n");
  assert.ok(
    /event_bus\.publish\(\s*["']dm:read["']/.test(lastReadFn),
    '_handle_presence_last_read should call event_bus.publish("dm:read", ...) after the explicit mark'
  );
  // The recipient is the OTHER party (the sender of the messages that
  // just got marked read), not the current user.
  assert.ok(
    /event_bus\.publish\(\s*["']dm:read["'][\s\S]{0,400}?user_ids=\[other\]/.test(lastReadFn),
    '_handle_presence_last_read should publish dm:read with user_ids=[other] (notify the other party)'
  );
});

test('Audit 2026-10-15 (presence SSE): server publishes presence:update from _handle_presence_status', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_status(self) -> None:');
  assert.ok(fnStart > 0, '_handle_presence_status not found');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  assert.ok(
    /event_bus\.publish\(\s*["']presence:update["']/.test(fn),
    '_handle_presence_status should call event_bus.publish("presence:update", ...) after set_status'
  );
  // The presence:update event has no user_ids filter → broadcasts to
  // everyone with the team modal/tile open. The frontend merges by
  // user_id from the payload.
  assert.ok(
    /event_bus\.publish\(\s*["']presence:update["'][\s\S]{0,400}?user_id/.test(fn),
    '_handle_presence_status should include user_id in the presence:update payload'
  );
});

test('Audit 2026-10-15 (presence SSE): handleNewDM refreshes all three DM containers', () => {
  const fnStart = appJs.indexOf('function handleNewDM(');
  assert.ok(fnStart > 0, 'handleNewDM not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);
  // All three log selectors must be queried.
  assert.ok(/#presence-dm-log/.test(fn), 'handleNewDM should query #presence-dm-log (modal)');
  assert.ok(/#presence-tile-dm-log/.test(fn), 'handleNewDM should query #presence-tile-dm-log (tile inline)');
  assert.ok(/#dm-popup-log/.test(fn), 'handleNewDM should query #dm-popup-log (popup)');
  // Should re-fetch the snapshot so myRecentDms is fresh.
  assert.ok(/fetchPresenceSnapshot/.test(fn), 'handleNewDM should call fetchPresenceSnapshot to refresh the inbox');
  // The original popup-only fetch is gone (the new version refreshes all 3).
  assert.ok(
    !/const\s+log\s*=\s*\$?\(\s*["']#dm-popup-log["']\s*\);/.test(fn),
    'handleNewDM should not be popup-only anymore — it must refresh all three containers'
  );
});

test('Audit 2026-10-15 (presence SSE): handleDMRead refreshes all three DM containers', () => {
  const fnStart = appJs.indexOf('function handleDMRead(');
  assert.ok(fnStart > 0, 'handleDMRead not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);
  assert.ok(/#presence-dm-log/.test(fn), 'handleDMRead should query #presence-dm-log (modal)');
  assert.ok(/#presence-tile-dm-log/.test(fn), 'handleDMRead should query #presence-tile-dm-log (tile inline)');
  assert.ok(/#dm-popup-log/.test(fn), 'handleDMRead should query #dm-popup-log (popup)');
  // The original popup-only lookup is gone.
  assert.ok(
    !/const\s+log\s*=\s*\$?\(\s*["']#dm-popup-log["']\s*\);/.test(fn),
    'handleDMRead should not be popup-only anymore — it must refresh all three containers'
  );
});

test('Audit 2026-10-15 (presence SSE): handlePresenceUpdate merges into state.presenceData.users (fix the envelope bug)', () => {
  const fnStart = appJs.indexOf('function handlePresenceUpdate(');
  assert.ok(fnStart > 0, 'handlePresenceUpdate not found');
  const fnEnd = appJs.indexOf('\nfunction ', fnStart + 1);
  const fn = appJs.slice(fnStart, fnEnd);
  // The original bug was calling findIndex on state.presenceData (the
  // envelope {users: [...], me, ...}). The fix looks inside .users.
  assert.ok(
    /state\.presenceData\.users[\s\S]{0,200}?findIndex/.test(fn),
    'handlePresenceUpdate should call findIndex on state.presenceData.users (not the envelope)'
  );
  // The Object.assign must merge fields that don't include avatar/lastSeen
  // (the SSE payload doesn't carry those; clobbering them would blank
  // the avatar dot).
  assert.ok(
    /Object\.assign\(users\[idx\][\s\S]{0,200}?status[\s\S]{0,200}?inferred[\s\S]{0,200}?autoStatus/.test(fn),
    'handlePresenceUpdate should Object.assign only status/inferred/autoStatus (not clobber avatar/lastSeen)'
  );
});
