// Regression: the standalone project page (public/project.html) showed
// "No history events" for every project after GET /api/v2/projects/{id}/history
// started answering with a paged envelope {events, total, count, startIndex}
// (2026-11-10). The page read `histRaw.data || histRaw.rows || histRaw`, which
// for the envelope is the object itself (no .length), so renderEvents() took
// its empty branch. Data and API were intact.
//
// Run with:  node --test tests/test_project_page_history_envelope.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, "..", "public", "project.html"), "utf8");
const appJs = fs.readFileSync(path.join(here, "..", "public", "app.js"), "utf8");

function loadExtractor() {
  const start = html.indexOf("function extractHistoryEvents(");
  assert.ok(start > 0, "extractHistoryEvents missing from project.html");
  const end = html.indexOf("function renderEvents(", start);
  const src = html.slice(start, end);
  const ctx = {};
  vm.createContext(ctx);
  vm.runInContext(src + "\nthis.fn = extractHistoryEvents;", ctx);
  return ctx.fn;
}

const note = { id: 1, categoryId: 8, category: { id: 8, title: "Note" }, content: "<p>hi</p>", isSystemEvent: false, attachments: [] };
const email = {
  id: 2, categoryId: 3, category: { id: 3, title: "Email" },
  content: JSON.stringify({ type: "email_snapshot", from: "a@b.c", subject: "S", message_id: 7 }),
  isSystemEvent: false, attachments: [],
};
const system = { id: 3, categoryId: null, category: { id: null, title: null }, content: "Stage changed from A to B", isSystemEvent: true, attachments: [] };
const all = [note, email, system];

test("paged envelope (current server shape) yields every event: note, email, system", () => {
  const ex = loadExtractor();
  const out = ex({ events: all, total: 3, count: 3, startIndex: 0 });
  assert.deepEqual(Array.from(out, (e) => e.id), [1, 2, 3]);
});

test("legacy shapes still work", () => {
  const ex = loadExtractor();
  assert.equal(ex(all).length, 3);
  assert.equal(ex({ data: all }).length, 3);
  assert.equal(ex({ rows: all }).length, 3);
  assert.equal(ex({ response: all }).length, 3);
  assert.equal(ex({ response: { events: all } }).length, 3);
});

test("empty / junk responses give an empty list, not a throw", () => {
  const ex = loadExtractor();
  assert.equal(ex(null).length, 0);
  assert.equal(ex(undefined).length, 0);
  assert.equal(ex({ events: [], total: 0 }).length, 0);
  assert.equal(ex("x").length, 0);
});

test("project.html routes both history fetches through the extractor", () => {
  assert.ok(!html.includes("histRaw.data || histRaw.rows"), "old envelope-blind read still present");
  const uses = html.split("renderEvents(extractHistoryEvents(histRaw))").length - 1;
  assert.equal(uses, 2, "initial load and post-note reload must both unwrap the envelope");
});

test("notification drawer note expand unwraps the envelope too", () => {
  assert.ok(
    appJs.includes("unwrapHistoryEvents(await api(`/api/v2/projects/${oppId}/history`))"),
    "drawer still treats the history response as a bare array",
  );
});
