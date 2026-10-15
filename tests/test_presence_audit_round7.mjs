#!/usr/bin/env node
// Regression tests for audit round 6 item #7 (presence windows + cross-tab close).
//
// The original implementation had three issues that this commit fixes:
//   1. The online / AFD windows were 10 min / 3 h. A user who closed
//      their laptop on Friday at 6 pm would still show as "AFD" with
//      a stale "Reviewing: Project X" auto-status on Monday at 9 am.
//      New windows: online = 180 s, AFD = 900 s.
//   2. Closing one of two tabs blanked the shared lastHeartbeat field
//      and made the user appear offline to everyone for up to 30 s,
//      even though the other tab was still alive. The fix: each
//      tab registers in a localStorage "tab registry"; heartbeats
//      report the live count; the server treats the user as offline
//      only when tabs === 0.
//   3. The cross-tab registry uses a localStorage key
//      "oo_board_presence_tabs_v1" plus a per-tab random id, and the
//      _liveTabCount helper expires entries older than 5 minutes.
//
// Run with: node --test tests/test_presence_audit_round7.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..');
const appJs = readFileSync(join(repoRoot, 'public/app.js'), 'utf8');
const serverPy = readFileSync(join(repoRoot, 'server.py'), 'utf8');
const presenceStorePy = readFileSync(join(repoRoot, 'presence_store.py'), 'utf8');

test('Audit 2026-10-15 (round 7): _handle_presence_get tightens online window from 600s to 180s', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_get(self) -> None:');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  // The new PRESENCE_ONLINE_SECONDS constant must be 180.
  assert.ok(
    /PRESENCE_ONLINE_SECONDS\s*=\s*180/.test(fn),
    '_handle_presence_get should define PRESENCE_ONLINE_SECONDS = 180 (was 600)'
  );
  // The 600s literal should be gone from the online check.
  assert.ok(
    !/< 600\b/.test(fn),
    '_handle_presence_get should no longer use the 600s online threshold'
  );
  // The check itself should reference the constant.
  assert.ok(
    /< PRESENCE_ONLINE_SECONDS/.test(fn),
    '_handle_presence_get should use PRESENCE_ONLINE_SECONDS in the online check'
  );
});

test('Audit 2026-10-15 (round 7): _handle_presence_get tightens AFD window from 10800s to 900s', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_get(self) -> None:');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  assert.ok(
    /PRESENCE_AFD_SECONDS\s*=\s*900/.test(fn),
    '_handle_presence_get should define PRESENCE_AFD_SECONDS = 900 (was 10800)'
  );
  assert.ok(
    !/< 10800\b/.test(fn),
    '_handle_presence_get should no longer use the 10800s AFD threshold'
  );
  assert.ok(
    /< PRESENCE_AFD_SECONDS/.test(fn),
    '_handle_presence_get should use PRESENCE_AFD_SECONDS in the AFD check'
  );
});

test('Audit 2026-10-15 (round 7): _handle_presence_get uses lastTabCount for the cross-tab close fix', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_get(self) -> None:');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  // The handler must read lastTabCount from the overlay.
  assert.ok(
    /ov\.get\(["']lastTabCount["']\)/.test(fn),
    '_handle_presence_get should read ov.get("lastTabCount") to know how many tabs are open'
  );
  // The online / afd flags must be ANDed with tabs > 0.
  assert.ok(
    /tabs_open\s*=\s*tab_count\s*>\s*0/.test(fn),
    '_handle_presence_get should compute tabs_open = tab_count > 0'
  );
  // The online flag must be false when tabs_open is false.
  assert.ok(
    /online\s*=\s*tabs_open\s+and\s+bool\(hb/.test(fn),
    '_handle_presence_get should AND online with tabs_open so a no-tab user is offline'
  );
});

test('Audit 2026-10-15 (round 7): _handle_presence_heartbeat reads the tabs field from the body', () => {
  const fnStart = serverPy.indexOf('def _handle_presence_heartbeat(self) -> None:');
  const fnEnd = serverPy.indexOf('\n    def _handle_presence_', fnStart + 1);
  const fn = serverPy.slice(fnStart, fnEnd);
  // Must read payload.tabs.
  assert.ok(
    /payload\.get\(["']tabs["']\)/.test(fn),
    '_handle_presence_heartbeat should read payload.get("tabs")'
  );
  // Must pass it to touch_heartbeat as a keyword arg. Use a permissive
  // pattern because the function call contains nested parens
  // (str(user["id"])) which a simple [^)]* can't span.
  assert.ok(
    /touch_heartbeat\([\s\S]*?tabs\s*=\s*tabs\b/.test(fn),
    '_handle_presence_heartbeat should call touch_heartbeat(..., tabs=tabs)'
  );
});

test('Audit 2026-10-15 (round 7): touch_heartbeat in presence_store accepts and persists lastTabCount', () => {
  const fnStart = presenceStorePy.indexOf('def touch_heartbeat(');
  const fnEnd = presenceStorePy.indexOf('\n\ndef ', fnStart + 1);
  const fn = presenceStorePy.slice(fnStart, fnEnd);
  // The function signature must include tabs.
  assert.ok(
    /tabs:\s*int\s*\|\s*None\s*=\s*None/.test(fn),
    'touch_heartbeat should accept tabs: int | None = None'
  );
  // The body must persist it to the dict. Match the assignment line
  // by pattern (allow any quoting style on the dict key).
  assert.ok(
    /lastTabCount["']?\s*\]\s*=\s*max\(0,\s*int\(tabs\)\)/.test(fn),
    'touch_heartbeat should set lastTabCount = max(0, int(tabs))'
  );
  // The load function must also round-trip it. The assignment line
  // is `presence["lastTabCount"] = int(data.get("lastTabCount") or 0)`
  // — match the assignment + data.get pattern with a permissive regex.
  const loadFn = presenceStorePy.slice(
    presenceStorePy.indexOf('def load_user_presence('),
    presenceStorePy.indexOf('\n\ndef ', presenceStorePy.indexOf('def load_user_presence(') + 1)
  );
  assert.ok(
    /presence\["lastTabCount"\]\s*=\s*int\(data\.get\(["']lastTabCount["']\)\s*or\s*0\)/.test(loadFn),
    'load_user_presence should round-trip lastTabCount'
  );
  // The save function must also write it.
  const saveFn = presenceStorePy.slice(
    presenceStorePy.indexOf('def save_user_presence('),
    presenceStorePy.indexOf('\n\ndef ', presenceStorePy.indexOf('def save_user_presence(') + 1)
  );
  assert.ok(
    /"lastTabCount":\s*max\(0,\s*int\(payload\.get\(["']lastTabCount["']\)\s*or\s*0\)\)/.test(saveFn),
    'save_user_presence should write lastTabCount from the payload'
  );
});

test('Audit 2026-10-15 (round 7): get_portal_presence_snapshot exposes lastTabCount', () => {
  const fnStart = presenceStorePy.indexOf('def get_portal_presence_snapshot(');
  const fnEnd = presenceStorePy.indexOf('\n\n# ', fnStart + 1);
  const fn = presenceStorePy.slice(fnStart, fnEnd);
  assert.ok(
    /["']lastTabCount["']\s*:\s*max\(0,\s*int\(data\.get\(["']lastTabCount["']\)\s*or\s*0\)\)/.test(fn),
    'get_portal_presence_snapshot should include lastTabCount in the returned dict'
  );
});

test('Audit 2026-10-15 (round 7): frontend has the cross-tab registry helpers', () => {
  // Module-level tab registry key.
  assert.ok(
    /_TAB_REGISTRY_KEY\s*=\s*["']oo_board_presence_tabs_v1["']/.test(appJs),
    'public/app.js should define _TAB_REGISTRY_KEY = "oo_board_presence_tabs_v1"'
  );
  // Per-tab id state.
  assert.ok(
    /let _thisTabId\s*=\s*null/.test(appJs),
    'public/app.js should declare `let _thisTabId = null`'
  );
  // The three helper functions.
  assert.ok(/function _registerThisTab\(/.test(appJs), 'public/app.js should define _registerThisTab');
  assert.ok(/function _unregisterThisTab\(/.test(appJs), 'public/app.js should define _unregisterThisTab');
  assert.ok(/function _liveTabCount\(/.test(appJs), 'public/app.js should define _liveTabCount');
  // The helpers must be called at module load.
  assert.ok(/_registerThisTab\(\);/.test(appJs), 'public/app.js should call _registerThisTab() at module load');
  // 5-minute expiry in _liveTabCount.
  assert.ok(
    /cutoff\s*=\s*Date\.now\(\)\s*-\s*5\s*\*\s*60\s*\*\s*1000/.test(appJs),
    '_liveTabCount should expire entries older than 5 minutes (matches the new AFD window)'
  );
});

test('Audit 2026-10-15 (round 7): heartbeat sendBeacon/send include tabs in the body', () => {
  // The visible=true send() should include tabs: _liveTabCount().
  assert.ok(
    /body:\s*JSON\.stringify\(\{\s*visible:\s*true,\s*tabs:\s*_liveTabCount\(\)\s*\}\)/.test(appJs),
    'send() (visible=true) should include tabs: _liveTabCount() in the body'
  );
  // The sendBeacon() for background should also include tabs.
  assert.ok(
    /const payload\s*=\s*JSON\.stringify\(\{\s*visible:\s*false,\s*tabs:\s*_liveTabCount\(\)\s*\}\)/.test(appJs),
    'sendBeacon() (background) should include tabs: _liveTabCount() in the payload'
  );
  // The offline beacon in onUnload should:
  //   1. call _unregisterThisTab() BEFORE building the payload (so
  //      the count reflects the post-unload state — i.e. this tab is
  //      gone, only siblings remain)
  //   2. include tabs: 0 (or _liveTabCount() which is 0 if last tab)
  assert.ok(
    /_unregisterThisTab\(\);[\s\S]{0,200}?navigator\.sendBeacon\(\s*["']\/api\/presence\/heartbeat["']/.test(appJs),
    'onUnload should call _unregisterThisTab() before sending the offline beacon'
  );
  assert.ok(
    /JSON\.stringify\(\{\s*offline:\s*true,\s*tabs:\s*_liveTabCount\(\)\s*\}\)/.test(appJs),
    'onUnload offline beacon should include tabs: _liveTabCount() (=0 for last tab)'
  );
});
