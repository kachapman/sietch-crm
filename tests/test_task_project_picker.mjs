// Guards for the Tasks-tile project picker (live bug 2026-11-10, claudiu).
//
// The picker used to store the chosen project ONLY in the modal's closure
// (`_taskEditProjectId`). `task.opportunityId` / `task.opportunity` kept the
// previous project, and the tile renders from that task object — so the just
// created task appeared linked to the old project, and the next save (opened
// from that stale chip, or a rerender after a checklist edit) wrote the old
// project id to the DB.
//
// Run with: node --test tests/test_task_project_picker.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const appJs = readFileSync(join(root, 'public', 'app.js'), 'utf8');

function pickerRegion() {
  const start = appJs.indexOf('function openTaskPreviewModal(');
  assert.ok(start > 0, 'openTaskPreviewModal missing');
  const end = appJs.indexOf('function taskSortMs(', start);
  assert.ok(end > start, 'could not delimit task modal');
  return appJs.slice(start, end);
}

const modal = pickerRegion();

test('picked project updates the task object, not just the closure', () => {
  const i = modal.indexOf('function _applyTaskProjectSelection(');
  assert.ok(i > 0, '_applyTaskProjectSelection helper missing');
  const helper = modal.slice(i, modal.indexOf('\n  }', i) + 4);
  assert.ok(helper.includes('_taskEditProjectId = id'), 'closure id not updated');
  assert.ok(helper.includes('task.opportunityId = id'), 'task.opportunityId not updated');
  assert.ok(helper.includes('task.opportunity = { title:'), 'task.opportunity title not updated');
});

test('both picker click handlers (initial + re-bound) use the helper', () => {
  const uses = modal.split('_applyTaskProjectSelection(item)').length - 1;
  assert.ok(uses >= 2, `expected both result click handlers to call the helper, found ${uses}`);
  assert.ok(!/_taskEditProjectId = Number\(item\.dataset\.oppId\)/.test(modal),
    'raw closure-only assignment must be gone (it loses the project on rerender)');
});

test('result ids come from the clicked row, never the row index', () => {
  const rowMaker = (modal.match(/data-opp-id="\$\{o\.id\}"/g) || []).length;
  assert.ok(rowMaker >= 2, 'result rows must render data-opp-id from o.id');
  assert.ok(!/data-opp-id="\$\{(?:i|index|idx)\}"/.test(modal),
    'result rows must not use the array index as the project id');
  const helper = modal.slice(modal.indexOf('function _applyTaskProjectSelection('));
  assert.ok(helper.includes('item.dataset.oppId'), 'selection must read the clicked row id');
});

test('create path copies persisted fields back onto the task object', () => {
  const i = modal.indexOf('const body = Object.assign(gatherFields(), updates)');
  assert.ok(i > 0, 'create body construction changed');
  const after = modal.slice(i, i + 1200);
  assert.ok(after.includes('Object.assign(task, body)'),
    'after create the task object must receive the persisted fields (title/project)');
});

test('gatherFields sends the chosen project id', () => {
  const i = modal.indexOf('const gatherFields = () =>');
  assert.ok(i > 0, 'gatherFields missing');
  const body = modal.slice(i, modal.indexOf('\n  };', i));
  assert.ok(body.includes('opportunityId: _taskEditProjectId'),
    'create/update body must send the chosen project id');
});
