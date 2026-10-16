// Tests for group-tile filter helpers. Mirrors public/app.js — keep in sync.
// Run with:  node --test tests/test_group_filters.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';

// Mirrors of public/app.js
//   effectiveVisibleStageIds (app.js ~3032)
//   tagLookupKey (app.js ~11020)
//   groupFilterSummary additions for the hidden-filter-stage branch.

function effectiveVisibleStageIds(group) {
  if (!group.stageColumnsConfigured) return null;
  const out = new Set((group.visibleStageIds || []).map(String));
  if (group.stageId != null && group.stageId !== "") out.add(String(group.stageId));
  return out;
}

function tagLookupKey(value, catalog) {
  const raw = (typeof value === "string" ? value : String(value?.title ?? value?.Title ?? value?.name ?? value?.Name ?? "")).trim();
  if (!raw) return "";
  const lower = raw.toLowerCase();
  if (catalog?.byTitleLower?.has(lower)) return lower;
  return lower;
}

function groupFilterSummary(group, stages = []) {
  const parts = [];
  if (group.tagTitles?.length) parts.push(`Tags: ${group.tagTitles.join(", ")}`);
  if (group.contactLabel) parts.push(`Contact: ${group.contactLabel}`);
  if (group.stageId) {
    const st = stages.find((s) => String(s.id) === String(group.stageId));
    if (st) parts.push(`Filter stage: ${st.title}`);
  }
  if (group.groupBy === "stage" && group.stageColumnsConfigured && group.visibleStageIds?.length) {
    const n = group.visibleStageIds.length;
    const total = stages.length;
    if (n < total) {
      const filterStageHidden = !group.visibleStageIds.includes(String(group.stageId));
      parts.push(filterStageHidden ? `${n} stage columns (filter column auto-shown)` : `${n} stage columns`);
    }
  }
  if (group.groupBy === "stage" && group.showEmptyStages === false) {
    parts.push("Hiding empty stages");
  }
  if (group.dealStatus && group.dealStatus !== "all") {
    parts.push(group.dealStatus === "open" ? "Open deals" : "Closed deals");
  }
  if (group.showOnlyRedOpportunities) parts.push("Red projects only");
  return parts.join(" · ");
}

// ── effectiveVisibleStageIds ─────────────────────────────────────────

test('pre-configured groups see all stages (null = "all visible")', () => {
  assert.equal(effectiveVisibleStageIds({ stageColumnsConfigured: false }), null);
  assert.equal(effectiveVisibleStageIds({}), null);
});

test('configured groups see only their listed stages', () => {
  const set = effectiveVisibleStageIds({
    stageColumnsConfigured: true,
    visibleStageIds: ['1', '3'],
  });
  assert.deepEqual([...set].sort(), ['1', '3']);
});

test('configured group with empty visibleStageIds + filter stage gets the filter', () => {
  const set = effectiveVisibleStageIds({
    stageColumnsConfigured: true,
    visibleStageIds: [],
    stageId: '5',
  });
  assert.deepEqual([...set], ['5']);
});

test('filter stage is always included even when the column is hidden', () => {
  const set = effectiveVisibleStageIds({
    stageColumnsConfigured: true,
    visibleStageIds: ['1', '2', '3'],
    stageId: '99', // not in the list
  });
  assert.equal(set.has('99'), true);
  assert.equal(set.has('1'), true);
});

test('numeric and string ids both coerce', () => {
  const set = effectiveVisibleStageIds({
    stageColumnsConfigured: true,
    visibleStageIds: [7, '8'],
    stageId: 9,
  });
  assert.deepEqual([...set].sort(), ['7', '8', '9']);
});

// ── tagLookupKey ────────────────────────────────────────────────────

test('tagLookupKey normalizes whitespace + case', () => {
  assert.equal(tagLookupKey('  High Priority  '), 'high priority');
  assert.equal(tagLookupKey('HIGH PRIORITY'), 'high priority');
});

test('tagLookupKey resolves through the catalog', () => {
  const catalog = { byTitleLower: new Map([['high priority', 'high priority']]) };
  assert.equal(tagLookupKey('High Priority', catalog), 'high priority');
});

test('tagLookupKey falls through when no match (still returns lowercased value)', () => {
  const catalog = { byTitleLower: new Map([['hot', 'hot']]) };
  assert.equal(tagLookupKey('Cold', catalog), 'cold');
});

test('tagLookupKey returns empty for empty input', () => {
  assert.equal(tagLookupKey(''), '');
  assert.equal(tagLookupKey(null), '');
  assert.equal(tagLookupKey(undefined), '');
});

// ── groupFilterSummary additions ────────────────────────────────────

test('summary warns when filter stage is hidden by column selector', () => {
  const stages = [{ id: 1, title: 'A' }, { id: 2, title: 'B' }];
  const s = groupFilterSummary({
    groupBy: 'stage',
    stageId: '2',
    stageColumnsConfigured: true,
    visibleStageIds: ['1'], // 2 is hidden but filter selected it
  }, stages);
  assert.match(s, /filter column auto-shown/);
  assert.match(s, /Filter stage: B/);
});

test('summary does not warn when filter stage is visible', () => {
  const stages = [{ id: 1, title: 'A' }, { id: 2, title: 'B' }];
  const s = groupFilterSummary({
    groupBy: 'stage',
    stageId: '1',
    stageColumnsConfigured: true,
    visibleStageIds: ['1', '2'],
  }, stages);
  assert.doesNotMatch(s, /filter column auto-shown/);
});

test('pre-configured group does not show "stage columns" line', () => {
  const stages = [{ id: 1, title: 'A' }, { id: 2, title: 'B' }];
  const s = groupFilterSummary({
    groupBy: 'stage',
    stageColumnsConfigured: false, // means "all visible"
    visibleStageIds: [],
  }, stages);
  assert.doesNotMatch(s, /stage columns/);
});
