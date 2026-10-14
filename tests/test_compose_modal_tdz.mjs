#!/usr/bin/env node
// Regression test for the compose-modal close button bug.
//
// The bug: `bindComposeEmailModal` in public/app.js used the local
// `fromSelect` variable (at line 22168) before declaring it with `const`
// (at line 22174). That's a temporal-dead-zone ReferenceError, so the
// function aborted mid-way on every open. The X close button, the
// Cancel button, and the Escape key handler were all below the throw
// point and were never bound, so clicking them did nothing.
//
// This test reads public/app.js, locates the `bindComposeEmailModal`
// function body, and verifies that every use of `fromSelect` inside it
// comes AFTER its `const fromSelect` declaration. Catches the bug
// class (TDZ) and would have caught the original regression in commit
// 31d2214 ("Microsoft Graph sendMail for Outlook SMTP-locked accounts")
// where the new graph-banner section was inserted above the existing
// `const fromSelect` declaration.
//
// Run with: node --test tests/test_compose_modal_tdz.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const appJs = readFileSync(
  new URL('../public/app.js', import.meta.url),
  'utf8'
);

// --- Locate the function body ---
//
// We find the line `function bindComposeEmailModal() {` and walk
// forward, tracking brace depth, until we exit the function.
function findFunctionBody(source, fnName) {
  const startRe = new RegExp(`^function\\s+${fnName}\\s*\\([^)]*\\)\\s*\\{`, 'm');
  const startMatch = startRe.exec(source);
  if (!startMatch) return null;
  const startIdx = startMatch.index + startMatch[0].length;
  let depth = 1;
  for (let i = startIdx; i < source.length; i++) {
    const ch = source[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return source.slice(startIdx, i);
    }
  }
  return null;
}

const body = findFunctionBody(appJs, 'bindComposeEmailModal');
assert.ok(body, 'bindComposeEmailModal function not found in public/app.js');

const lines = body.split('\n');

// --- Track `const fromSelect` / `let fromSelect` declarations and uses ---
//
// We scan line-by-line, tracking:
//  - "declared" boolean: whether the `const fromSelect` has been seen
//  - "uses" array: each use of `fromSelect` (not preceded by `const`/`let`/`var`/`function`)
//
// Heuristics to avoid false positives:
//  - Strip line comments (// to end of line) and block comments before scanning
//  - Strip string literals (single/double/backtick) before scanning
//  - Only count `fromSelect` as a use when it's a standalone identifier
//    (not part of a longer word like `composeFromSelect` or
//    `fromSelectList`)

function stripCommentsAndStrings(line) {
  // Remove line comments
  let i = line.indexOf('//');
  if (i >= 0) line = line.slice(0, i);
  // Remove string literals (single, double, backtick) — naive but
  // sufficient for this scan; doesn't need to be a full lexer.
  line = line.replace(/'(?:\\.|[^'\\])*'/g, "''");
  line = line.replace(/"(?:\\.|[^"\\])*"/g, '""');
  line = line.replace(/`(?:\\.|[^`\\])*`/g, '``');
  return line;
}

const usesBeforeDecl = [];
let declared = false;
let scannedPastDecl = false;

for (let i = 0; i < lines.length; i++) {
  const raw = lines[i];
  const line = stripCommentsAndStrings(raw);

  // Detect `const fromSelect` or `let fromSelect` (not `var` — var
  // hoists and is fine, but app.js uses const).
  const declMatch = line.match(/(?:^|[\s{;,(])(const|let)\s+fromSelect\b/);
  if (declMatch) {
    declared = true;
    continue;
  }

  // Once we've seen the declaration, we don't care about uses after it.
  // Only collect uses that appear BEFORE the declaration.
  if (declared) {
    scannedPastDecl = true;
    continue;
  }

  // Detect uses that happen before the declaration. A use is a
  // `fromSelect` identifier NOT preceded by an identifier character
  // (so we don't match `myFromSelect` or `fromSelectList`) and NOT
  // preceded by `.` (member access).
  const tokenRe = /(?<![A-Za-z0-9_$.])fromSelect\b/g;
  let m;
  while ((m = tokenRe.exec(line)) !== null) {
    usesBeforeDecl.push({ lineNum: i + 1, line: raw.trim() });
  }
  // Suppress unused-var warning; kept for future debugging.
  void scannedPastDecl;
}

// --- Report ---
test('bindComposeEmailModal: no use-before-declaration of fromSelect', () => {
  // The real regression check: if fromSelect is referenced before
  // its declaration, that's a TDZ ReferenceError and the function
  // aborts, breaking the dismiss/Escape/auto-save handlers.
  if (usesBeforeDecl.length > 0) {
    assert.fail(
      `fromSelect is used ${usesBeforeDecl.length} time(s) before its ` +
      `const/let declaration. This is a TDZ ReferenceError that silently ` +
      `aborts bindComposeEmailModal — breaking the close button, Cancel, ` +
      `Escape key, and auto-save timer.\n` +
      `First uses:\n` +
      usesBeforeDecl.slice(0, 5).map((u) => `  line ${u.lineNum}: ${u.line}`).join('\n')
    );
  }
  assert.ok(declared, 'fromSelect must be declared inside the function');
});

test('bindComposeEmailModal: fromSelect is declared exactly once', () => {
  let count = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = stripCommentsAndStrings(lines[i]);
    if (/(?:^|[\s{;,(])(const|let)\s+fromSelect\b/.test(line)) {
      count++;
    }
  }
  assert.equal(count, 1,
    `fromSelect should be declared exactly once, found ${count}`);
});

test('bindComposeEmailModal: dismiss handler is registered', () => {
  // The fix is meaningful only if the dismiss handler actually runs
  // to completion. Verify the line `modal.querySelectorAll('[data-compose-email-dismiss]')`
  // exists in the function body.
  assert.ok(
    body.includes("[data-compose-email-dismiss]"),
    'bindComposeEmailModal should bind the [data-compose-email-dismiss] handler'
  );
});

test('bindComposeEmailModal: Escape key handler is registered', () => {
  // Verify the Escape handler line is in the function body.
  assert.ok(
    /e\.key\s*===\s*['"]Escape['"]/.test(body),
    'bindComposeEmailModal should bind the Escape key handler'
  );
});

test('bindComposeEmailModal: auto-save timer is started', () => {
  // Verify the setInterval for auto-save is in the function body.
  assert.ok(
    /setInterval\(/.test(body),
    'bindComposeEmailModal should start the auto-save timer'
  );
});
