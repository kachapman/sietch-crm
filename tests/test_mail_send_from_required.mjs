#!/usr/bin/env node
/**
 * Send stays disabled until a From account is selected
 * (compose modal, inline reply/forward, compose tab).
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const appSrc = readFileSync(join(root, "public", "app.js"), "utf8");
const indexHtml = readFileSync(join(root, "public", "index.html"), "utf8");

test("bindFromSelectToSendButton disables Send when no account is selected", () => {
  assert.ok(appSrc.includes("function bindFromSelectToSendButton("));
  assert.match(
    appSrc,
    /sendBtn\.disabled = !selectedFromAccountId\(selectEl\)/,
  );
  assert.ok(appSrc.includes('selectEl.addEventListener("change", sync)'));
});

test("compose modal does not blindly re-enable Send after populating From", () => {
  const start = appSrc.indexOf("async function openComposeModal");
  assert.ok(start >= 0);
  const end = appSrc.indexOf("function setupContactAutocomplete", start);
  const body = appSrc.slice(start, end > start ? end : start + 8000);
  assert.ok(body.includes("bindFromSelectToSendButton(fromSelect, sendBtn0)"));
  assert.ok(!/if \(sendBtn0\) sendBtn0\.disabled = false;/.test(body));
});

test("compose send finally resyncs the From-required disabled state", () => {
  assert.ok(
    appSrc.includes("bindFromSelectToSendButton($('#compose-from-account'), sendBtn)"),
  );
});

test("inline composer binds From to the Send button", () => {
  const start = appSrc.indexOf("replyDiv.querySelector('.inline-reply-cancel')");
  assert.ok(start >= 0);
  const body = appSrc.slice(start, start + 1500);
  assert.ok(body.includes("bindFromSelectToSendButton("));
  assert.ok(body.includes(".inline-reply-from"));
  assert.ok(body.includes(".inline-reply-send"));
});

test("compose-tab Send is bound to the tab From select", () => {
  assert.ok(
    appSrc.includes("bindFromSelectToSendButton(fromSelect, container.querySelector('.compose-tab-send'))"),
  );
  assert.ok(
    appSrc.includes("bindFromSelectToSendButton(container.querySelector('.compose-tab-from'), sendBtn)"),
  );
});

test("app.js cache-buster was bumped with this UI change", () => {
  assert.ok(indexHtml.includes("app.js?v=3.7.62"));
  assert.ok(!indexHtml.includes("app.js?v=3.7.60"));
});
