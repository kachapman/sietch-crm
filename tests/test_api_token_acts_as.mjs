// Node tests for the "Posts as" API-token UI (no browser, no npm deps).
//
// Covers the real helpers in public/api-token-ui.js and the source wiring in
// public/app.js + public/index.html:
//   * the create body carries acts_as_user_id only when a user was chosen;
//   * an invalid/blank identity never silently becomes "post as nobody";
//   * the selector lists active users with the signed-in admin first;
//   * the token list renders "posts as …" and the activity tag renders
//     "via API: <name>" (escaped, text-only);
//   * the form reads the select and resets it to the default after create.
//
// Run with: node --test tests/test_api_token_acts_as.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const UI_SRC = readFileSync(join(ROOT, "public", "api-token-ui.js"), "utf8");
const APP_SRC = readFileSync(join(ROOT, "public", "app.js"), "utf8");
const INDEX_SRC = readFileSync(join(ROOT, "public", "index.html"), "utf8");

function loadUi() {
  const sandbox = { globalThis: {}, console };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(UI_SRC, sandbox);
  return sandbox.SietchApiTokenUi;
}

const ui = loadUi();

test("buildCreateRequest sends the chosen identity", () => {
  const out = ui.buildCreateRequest("baggins bot", { actsAsUserId: "7" });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.body.acts_as_user_id, 7);
  assert.equal(out.body.scope, "full");
});

test("buildCreateRequest omits acts_as_user_id when nobody was chosen", () => {
  for (const value of [undefined, null, "", "   "]) {
    const out = ui.buildCreateRequest("default bot", { actsAsUserId: value });
    assert.equal(out.ok, true, out.error);
    assert.equal("acts_as_user_id" in out.body, false,
      "a blank selector must let the server default to the creator");
  }
});

test("buildCreateRequest refuses a non-numeric or non-positive identity", () => {
  for (const bad of ["bilbo", 0, -1, 2.5, {}]) {
    const out = ui.buildCreateRequest("x", { actsAsUserId: bad });
    assert.equal(out.ok, false, `expected refusal for ${String(bad)}`);
    assert.match(out.error, /Posts as/);
  }
});

test("restrictions stay unchanged alongside posts-as", () => {
  const out = ui.buildCreateRequest("ro bot", {
    readOnly: true, methods: ["GET"], resources: ["projects"], actsAsUserId: 7,
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.body.scope, "read");
  assert.equal(out.body.read_only, true);
  assert.equal(out.body.acts_as_user_id, 7);
  // vm context: compare copies, not cross-realm objects
  assert.equal(JSON.stringify(out.body.restrictions),
    JSON.stringify({ methods: ["GET"], resources: ["projects"] }));
});

test("actsAsOptions lists active users, signed-in admin first", () => {
  const users = [
    { id: 7, displayName: "Mr. Baggins", isActive: true },
    { id: 1, displayName: "Ken", isActive: true },
    { id: 9, displayName: "Gone", isActive: false },
    { id: 7, displayName: "Mr. Baggins (dupe)", isActive: true },
    { id: 4, email: "no-name@example.com", isActive: true },
  ];
  const opts = ui.actsAsOptions(users, 1);
  // signed-in admin first, then everyone else by label ("Mr. Baggins" < "no-name@…")
  assert.equal(JSON.stringify(opts.map((o) => o.value)), JSON.stringify(["1", "7", "4"]));
  assert.match(opts[0].label, /\(you\)$/);
  assert.equal(opts.some((o) => o.value === "9"), false, "inactive users are not offered");
  assert.equal(opts.filter((o) => o.value === "7").length, 1, "deduped");
});

test("actsAsOptions survives junk input", () => {
  assert.equal(ui.actsAsOptions(null, 1).length, 0);
  assert.equal(ui.actsAsOptions([null, {}, { id: 0 }], 1).length, 0);
});

test("summarizeToken exposes the identity and describeActsAs renders it", () => {
  const row = ui.summarizeToken({
    id: 3, name: "baggins bot", token_prefix: "sietch_ab",
    scope: "full", created_by_email: "ken@example.com",
    acts_as_user_id: 7, acts_as_email: "bilbo@example.com",
    acts_as_display_name: "Mr. Baggins",
  });
  assert.equal(row.actsAsUserId, 7);
  assert.equal(row.actsAsName, "Mr. Baggins");
  assert.equal(ui.describeActsAs(row),
    "posts as Mr. Baggins <bilbo@example.com>");
});

test("describeActsAs falls back to the creator for pre-047 rows", () => {
  const row = ui.summarizeToken({
    id: 3, name: "old", token_prefix: "sietch_ab", scope: "full",
    created_by_email: "ken@example.com",
  });
  assert.equal(row.actsAsUserId, null);
  assert.equal(ui.describeActsAs(row), "posts as ken@example.com");
});

test("summarizeToken ignores unexpected fields (no token/hash leak)", () => {
  const row = ui.summarizeToken({
    id: 1, name: "x", token: "sietch_secret", token_hash: "deadbeef",
    acts_as_user_id: 2,
  });
  assert.equal("token" in row, false);
  assert.equal("token_hash" in row, false);
});

test("readCreateOptionsFromForm picks up the select value", () => {
  const doc = {
    getElementById(id) {
      if (id === "api-token-acts-as") return { value: "7" };
      if (id === "api-token-read-only") return { checked: false };
      if (id === "api-token-expires") return { value: "" };
      return null;
    },
    querySelectorAll() { return []; },
  };
  const opts = ui.readCreateOptionsFromForm(doc);
  assert.equal(opts.actsAsUserId, "7");
  assert.equal(opts.readOnly, false);
});

test("readCreateOptionsFromForm treats an empty select as the default", () => {
  const doc = {
    getElementById(id) {
      if (id === "api-token-acts-as") return { value: "" };
      if (id === "api-token-read-only") return { checked: false };
      if (id === "api-token-expires") return { value: "" };
      return null;
    },
    querySelectorAll() { return []; },
  };
  assert.equal(ui.readCreateOptionsFromForm(doc).actsAsUserId, null);
});

test("clearCreateOptionsForm resets the selector to the stored default", () => {
  const select = { value: "7", dataset: { defaultUserId: "1" } };
  const doc = {
    getElementById(id) { return id === "api-token-acts-as" ? select : null; },
    querySelectorAll() { return []; },
  };
  ui.clearCreateOptionsForm(doc);
  assert.equal(select.value, "1");
});

test("the admin form offers a 'Posts as' selector", () => {
  assert.match(INDEX_SRC, /id="api-token-acts-as"/);
  assert.match(INDEX_SRC, /<label for="api-token-acts-as"/);
  // It sits inside the create form, before the submit button's section.
  const form = INDEX_SRC.slice(INDEX_SRC.indexOf('id="api-token-create-form"'),
    INDEX_SRC.indexOf("</form>", INDEX_SRC.indexOf('id="api-token-create-form"')));
  assert.match(form, /id="api-token-acts-as"/);
});

test("app.js loads the user list and defaults the selector to the admin", () => {
  assert.match(APP_SRC, /function _loadApiTokenActsAsUsers/);
  assert.match(APP_SRC, /api\("\/api\/v2\/users"\)/);
  assert.match(APP_SRC, /select\.dataset\.defaultUserId = defaultId/);
  const fn = APP_SRC.slice(APP_SRC.indexOf("async function _loadApiTokenActsAsUsers"),
    APP_SRC.indexOf("async function loadAdminApiTokens"));
  assert.match(fn, /select\.value = defaultId/);
  assert.match(fn, /escapeHtml\(o\.value\)/);
  assert.match(fn, /escapeHtml\(o\.label\)/);
});

test("the token list shows the identity each token posts as", () => {
  const fn = APP_SRC.slice(APP_SRC.indexOf("function _apiTokenRowHtml"),
    APP_SRC.indexOf("async function _loadApiTokenActsAsUsers"));
  assert.match(fn, /describeActsAs/);
  assert.match(fn, /escapeHtml\(actsAs\)/);
});

test("history activity renders the via-API tag text-only", () => {
  assert.match(APP_SRC, /function historyEventApiTokenName/);
  assert.match(APP_SRC, /function viaApiTagHtml/);
  assert.match(APP_SRC, /className = "via-api-tag"/);
  assert.match(APP_SRC, /tag\.textContent = "via API: " \+ tokenName/);
  // Escaped in the template-string path (notes), never innerHTML.
  assert.match(APP_SRC, /escapeHtml\(clean\)/);
  assert.doesNotMatch(APP_SRC, /innerHTML\s*=\s*[^;]*via API/);
});

test("note cards show the tag from the backend field", () => {
  const fn = APP_SRC.slice(APP_SRC.indexOf("function _notesRenderCard"),
    APP_SRC.indexOf("// ═", APP_SRC.indexOf("function _notesRenderCard")));
  assert.match(fn, /viaApiTagHtml\(note\.api_token_name\)/);
});

test("the tag is styled and does not become a clickable control", () => {
  const css = readFileSync(join(ROOT, "public", "styles.css"), "utf8");
  const block = css.slice(css.indexOf(".via-api-tag"), css.indexOf("}", css.indexOf(".via-api-tag")));
  assert.match(block, /font-size/);
  assert.match(block, /color: var\(--muted\)/);
  assert.equal(/cursor:\s*pointer/.test(block), false);
});

test("cache-busters were bumped for the changed assets", () => {
  assert.match(INDEX_SRC, /app\.js\?v=3\.7\.52/);
  assert.match(INDEX_SRC, /styles\.css\?v=3\.7\.23/);
});
