// Tests for the Admin → MCP console section (public/app.js + index.html).
//
// The MCP admin UI is a thin client over /api/v2/admin/mcp/*; these are
// source-level guards (the flows need a live admin session + a real MCP
// client, which tests/test_mcp_server.py covers at the protocol level).
//
// Run with:  node --test tests/test_mcp_admin.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const appSrc = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(ROOT, 'public', 'index.html'), 'utf8');

function region(marker, length = 9000) {
  const at = appSrc.indexOf(marker);
  assert.ok(at >= 0, `marker not found: ${marker}`);
  return appSrc.slice(at, at + length);
}

test('API Tokens and MCP are ONE admin tab ("API & MCP")', () => {
  assert.ok(indexHtml.includes('data-tab="api-mcp"'));
  assert.ok(indexHtml.includes('id="admin-tab-api-mcp"'));
  // the separate tabs are gone
  assert.ok(!indexHtml.includes('data-tab="tokens"'));
  assert.ok(!indexHtml.includes('data-tab="mcp"'));
  assert.ok(!indexHtml.includes('id="admin-tab-tokens"'));
  assert.ok(!indexHtml.includes('id="admin-tab-mcp"'));
});

test('MCP controls sit above the token list; audit is in the same tab', () => {
  const tab = indexHtml.slice(indexHtml.indexOf('id="admin-tab-api-mcp"'));
  const end = tab.indexOf('id="admin-tab-photo-reports"');
  const section = tab.slice(0, end === -1 ? undefined : end);
  for (const id of ['mcp-enabled', 'mcp-url', 'mcp-snippet', 'api-token-create-form',
                    'api-token-list', 'mcp-proposals', 'mcp-audit', 'mcp-export-btn']) {
    assert.ok(section.includes('id="' + id + '"'), 'missing #' + id + ' in the combined tab');
  }
  const pos = (id) => section.indexOf('id="' + id + '"');
  assert.ok(pos('mcp-enabled') < pos('api-token-list'), 'MCP switch must be above the token list');
  assert.ok(pos('mcp-url') < pos('api-token-list'), 'connection URL must be above the token list');
  assert.ok(pos('api-token-list') < pos('mcp-audit'), 'audit log sits below the token list');
});

test('the old tab names redirect to the combined tab', () => {
  const at = appSrc.indexOf('function switchAdminTab(tab) {');
  const body = appSrc.slice(at, at + 1600);
  assert.ok(body.includes('if (tab === "tokens" || tab === "mcp") tab = "api-mcp";'));
  assert.ok(body.includes('Promise.resolve(loadAdminMcp()).finally(() => loadAdminApiTokens())'));
});

test('enable/disable calls the settings endpoint', () => {
  const body = region('async function loadAdminMcp', 2500);
  assert.ok(body.includes('/api/v2/admin/mcp/settings'));
  const bind = region('function bindMcpControls', 2500);
  assert.ok(bind.includes('method: "PUT"'));
  assert.ok(bind.includes('enabled: toggle.checked'));
});

test('the config snippet never contains a real token', () => {
  const body = region('function mcpSnippet', 900);
  assert.ok(body.includes('<YOUR_SIETCH_API_TOKEN>'));
  // no literal sietch_ token value is embedded anywhere in the UI
  assert.ok(!/sietch_[A-Za-z0-9_-]{20,}/.test(appSrc), 'a real-looking token literal is present');
});

test('proposals can be approved and rejected from the UI', () => {
  const body = region('async function loadMcpProposals', 4000);
  assert.ok(body.includes('data-mcp-approve'));
  assert.ok(body.includes('data-mcp-reject'));
  const decide = region('async function decideMcpProposal', 1200);
  assert.ok(decide.includes('/api/v2/admin/mcp/proposals/${id}/${decision}'));
  assert.ok(decide.includes('method: "POST"'));
});

test('per-token allowlist is inline in each token row and defaults to all tools', () => {
  const row = region('function _apiTokenAllowlistHtml', 2500);
  assert.ok(row.includes('data-mcp-token'));
  assert.ok(row.includes('data-mcp-save-tools'));
  const save = region('async function _saveTokenMcpTools', 1500);
  assert.ok(save.includes('/api/v2/admin/mcp/tokens/'));
  assert.ok(save.includes('picked.length === boxes.length ? null : picked'),
    'unchecking everything must mean "all tools" (null), not an empty allowlist');
  // no separate per-token allowlist list remains
  assert.ok(!appSrc.includes('renderMcpTokenTools'));
  assert.ok(!indexHtml.includes('id="mcp-token-tools"'));
});

test('each token row shows the posts-as user and its restrictions', () => {
  const row = region('function _apiTokenRowHtml', 2500);
  assert.ok(row.includes('"posts as "'));
  assert.ok(row.includes('describeRestrictions'));
  assert.ok(row.includes('_apiTokenAllowlistHtml(row)'));
});

test('the audit log renders time, token, tool, mode, result and args', () => {
  const body = region('async function loadMcpAudit', 3000);
  for (const field of ['createdAt', 'tokenName', 'tool', 'mode', 'status', 'argsSummary']) {
    assert.ok(body.includes(field), `audit table is missing ${field}`);
  }
});

test('decisions are exportable as JSONL', () => {
  const bind = region('function bindMcpControls', 2500);
  assert.ok(bind.includes('/api/v2/admin/mcp/training-export'));
});

test('the MCP UI never prints a token secret', () => {
  const start = appSrc.indexOf('// ── Admin → MCP');
  const end = appSrc.indexOf('function switchAdminTab');
  const section = appSrc.slice(start, end);
  assert.ok(!section.includes('api_token_secret'), 'the MCP section must not read the one-time secret');
  assert.ok(!section.includes('Authorization: "Bearer sietch_'));
  assert.ok(section.includes('escapeHtml('), 'rendered proposal/audit data must be escaped');
});
