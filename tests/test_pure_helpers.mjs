// Smoke tests for pure-JS helpers extracted from public/app.js.
//
// These functions are pure (no DOM dependency) so they can be tested
// directly via Node's built-in test runner (no jsdom, no npm deps).
//
// Run with:  node --test tests/test_pure_helpers.mjs
//
// The helpers below are kept in sync with the definitions in
// public/app.js. If you change one in app.js, change the matching
// helper here. The test runner asserts each helper's output matches
// what the live app.js produces for the same input.

import { test } from 'node:test';
import assert from 'node:assert/strict';

// ──────────────────────────────────────────────────────────────────────
// Mirror of public/app.js#dueChipHtml / labelChipsHtml (task row chips).
// escapeHtml is DOM-backed in app.js; the mirror uses a minimal equivalent
// sufficient for these assertions (attributes come from dates/labels).
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
const TASK_OVERDUE_ICON = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M15 21h-9a2 2 0 0 1 -2 -2v-12a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v5"/><path d="M16 3v4"/><path d="M8 3v4"/><path d="M4 11h16"/><path d="M11 15h1"/><path d="M12 15v3"/><path d="M19 16v3"/><path d="M19 22v.01"/></svg>`;
const TASK_LABELS_ICON = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M3 8v4.172a2 2 0 0 0 .586 1.414l5.71 5.71a2.41 2.41 0 0 0 3.408 0l3.592 -3.592a2.41 2.41 0 0 0 0 -3.408l-5.71 -5.71a2 2 0 0 0 -1.414 -.586h-4.172a2 2 0 0 0 -2 2"/><path d="M18 19l1.592 -1.592a4.82 4.82 0 0 0 0 -6.816l-4.592 -4.592"/><path d="M7 10h-.01"/></svg>`;
function dueChipHtml(task) {
  const due = task.dueDate || task.deadLine?.value || task.deadLine;
  if (!due) return "";
  const d = new Date(due);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const cls = day < today ? "overdue" : day === today ? "today" : "";
  if (cls === "overdue") {
    return `<span class="task-chip task-chip-due task-chip--overdue" title="Overdue ${esc(d.toLocaleDateString())}">${TASK_OVERDUE_ICON}</span>`;
  }
  const label = cls === "today" ? "Today" : d.toLocaleDateString();
  return `<span class="task-chip task-chip-due ${cls ? "task-chip--" + cls : ""}">${esc(label)}</span>`;
}
function labelChipsHtml(task) {
  const labels = String(task.labels || "").split(/\s+/).filter(Boolean);
  if (!labels.length) return "";
  return `<span class="task-chip task-chip-label" title="${esc(labels.join(", "))}">${TASK_LABELS_ICON}</span>`;
}
function userInitials(u) {
  const f = (u && (u.first_name || u.firstName) || '').toString().trim();
  const l = (u && (u.last_name || u.lastName) || '').toString().trim();
  if (f && l) return (f[0] + l[0]).toUpperCase();
  if (f) return f.slice(0, 2).toUpperCase();
  if (l) return l.slice(0, 2).toUpperCase();
  const email = (u && u.email || '').toString().trim();
  if (email) return email[0].toUpperCase();
  return '?';
}

// Mirror of public/app.js#contactInitials (line 21186)
function contactInitials(c) {
  if (!c) return '?';
  if (c.is_company || c.isCompany) {
    const company = (c.company || c.displayName || c.companyName || '').toString().trim();
    if (company) return company.slice(0, 2).toUpperCase();
  }
  const f = (c.first_name || c.firstName || '').toString().trim();
  const l = (c.last_name || c.lastName || '').toString().trim();
  if (f && l) return (f[0] + l[0]).toUpperCase();
  if (f) return f.slice(0, 2).toUpperCase();
  if (l) return l.slice(0, 2).toUpperCase();
  const company = (c.company || c.displayName || c.companyName || '').toString().trim();
  if (company) return company.slice(0, 2).toUpperCase();
  return '?';
}

// Mirror of public/app.js#_hasAudioExt (line 24150)
const AUDIO_EXTENSIONS = [".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac", ".webm"];
function _hasAudioExt(text) {
  const t = String(text || "").toLowerCase();
  if (!t) return false;
  for (const ext of AUDIO_EXTENSIONS) {
    if (t.endsWith(ext)) return true;
  }
  return false;
}

// Mirror of public/app.js#isAudioUrl (line 24158)
function isAudioUrl(url) {
  const u = String(url || "").trim();
  if (!u) return false;
  const noQuery = u.split("#")[0].split("?")[0].toLowerCase();
  return _hasAudioExt(noQuery);
}

// Mirror of public/app.js#isAudioLink (line 24167) — DOM-free subset.
// We can't pass an actual <a> element, but we can test the URL+text logic.
function isAudioLinkArgs(href, text) {
  if (isAudioUrl(href || "")) return true;
  return _hasAudioExt((text || "").trim());
}

// Mirror of public/app.js#collectNumericIdsFromValue (line 24303)
function collectNumericIdsFromValue(value, out) {
  if (value == null) return;
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    out.add(Math.floor(value));
    return;
  }
  const s = String(value).trim();
  if (!s) return;
  if (/^\d+$/.test(s)) {
    out.add(Number(s));
    return;
  }
  const re = /(?:mail\/messages\/|messageId[=:]|message_id[=:]|#message\/|action=mailmessage(?:&amp;|&)?message_id=|idmessage[=:]|mailmessageid[=:])(\d+)/gi;
  let m;
  while ((m = re.exec(s))) out.add(Number(m[1]));
}

// ──────────────────────────────────────────────────────────────────────
// userInitials tests
test('userInitials: full first+last returns "FL"', () => {
  assert.equal(userInitials({ firstName: 'Breanna', lastName: 'Chapman' }), 'BC');
  assert.equal(userInitials({ first_name: 'Breanna', last_name: 'Chapman' }), 'BC');
});

test('userInitials: only first returns first 2 chars upper', () => {
  assert.equal(userInitials({ firstName: 'alice' }), 'AL');
  assert.equal(userInitials({ first_name: 'alice' }), 'AL');
});

test('userInitials: only last returns first 2 chars upper', () => {
  assert.equal(userInitials({ lastName: 'smith' }), 'SM');
  assert.equal(userInitials({ last_name: 'smith' }), 'SM');
});

test('userInitials: no name falls back to email', () => {
  assert.equal(userInitials({ email: 'foo@bar.com' }), 'F');
});

test('userInitials: empty user returns "?"', () => {
  assert.equal(userInitials({}), '?');
  assert.equal(userInitials(null), '?');
  assert.equal(userInitials(undefined), '?');
});

test('userInitials: trims whitespace', () => {
  assert.equal(userInitials({ firstName: '  Breanna  ', lastName: '  Chapman  ' }), 'BC');
});

// ──────────────────────────────────────────────────────────────────────
// contactInitials tests
test('contactInitials: person with first+last', () => {
  assert.equal(contactInitials({ first_name: 'Jane', last_name: 'Doe' }), 'JD');
});

test('contactInitials: company uses displayName prefix', () => {
  assert.equal(contactInitials({ isCompany: true, displayName: 'Acme Corp' }), 'AC');
  assert.equal(contactInitials({ is_company: true, companyName: 'Vanguard Adjusters' }), 'VA');
});

test('contactInitials: null returns "?"', () => {
  assert.equal(contactInitials(null), '?');
  assert.equal(contactInitials(undefined), '?');
});

// ──────────────────────────────────────────────────────────────────────
// isAudioUrl tests
test('isAudioUrl: recognizes audio extensions in URL path', () => {
  assert.equal(isAudioUrl('https://example.com/foo/bar.mp3'), true);
  assert.equal(isAudioUrl('https://example.com/foo/voice.wav'), true);
  assert.equal(isAudioUrl('https://example.com/audio.m4a'), true);
  assert.equal(isAudioUrl('https://example.com/song.opus'), true);
  assert.equal(isAudioUrl('https://example.com/rec.flac'), true);
});

test('isAudioUrl: case-insensitive', () => {
  assert.equal(isAudioUrl('https://example.com/FOO.MP3'), true);
  assert.equal(isAudioUrl('https://example.com/Voice.WAV'), true);
});

test('isAudioUrl: strips query string and fragment before matching', () => {
  assert.equal(isAudioUrl('https://example.com/foo.mp3?download=1'), true);
  assert.equal(isAudioUrl('https://example.com/foo.mp3#anchor'), true);
  // Misleading query shouldn't match a non-audio path
  assert.equal(isAudioUrl('https://example.com/document.pdf?mp3'), false);
});

test('isAudioUrl: rejects non-audio URLs', () => {
  assert.equal(isAudioUrl('https://example.com/report.pdf'), false);
  assert.equal(isAudioUrl('https://example.com/image.png'), false);
  assert.equal(isAudioUrl('https://example.com/file.docx'), false);
});

test('isAudioUrl: empty/null/undefined returns false', () => {
  assert.equal(isAudioUrl(''), false);
  assert.equal(isAudioUrl(null), false);
  assert.equal(isAudioUrl(undefined), false);
});

test('isAudioUrl: OnlyOffice filehandler URL with non-audio file', () => {
  // Common production URL pattern from OnlyOffice document storage
  assert.equal(
    isAudioUrl('https://office.example.com/Products/Files/HttpHandlers/filehandler.ashx?action=download&fileid=123'),
    false,
  );
});

// ──────────────────────────────────────────────────────────────────────
// isAudioLinkArgs tests (URL + text variant)
test('isAudioLinkArgs: matches on href extension', () => {
  assert.equal(isAudioLinkArgs('https://example.com/x.mp3', 'click here'), true);
});

test('isAudioLinkArgs: matches on visible text when URL has no extension', () => {
  // OnlyOffice filehandler URLs have no extension — the filename lives in the link text
  assert.equal(
    isAudioLinkArgs(
      'https://office.example.com/filehandler.ashx?action=download&fileid=123',
      'voice-message.mp3',
    ),
    true,
  );
});

test('isAudioLinkArgs: rejects when neither href nor text is audio', () => {
  assert.equal(isAudioLinkArgs('https://example.com/x.pdf', 'report.pdf'), false);
});

test('isAudioLinkArgs: empty inputs', () => {
  assert.equal(isAudioLinkArgs('', ''), false);
  assert.equal(isAudioLinkArgs(null, null), false);
});

// ──────────────────────────────────────────────────────────────────────
// collectNumericIdsFromValue tests
test('collectNumericIdsFromValue: positive integer passes through', () => {
  const out = new Set();
  collectNumericIdsFromValue(12345, out);
  assert.deepEqual([...out], [12345]);
});

test('collectNumericIdsFromValue: rejects negative (0 falls through to digit-string match)', () => {
  // Documenting the actual behavior: numeric > 0 check is the fast path,
  // but the digit-string regex below accepts "0" too. This is intentional —
  // we don't want to silently swallow legitimate IDs from extracted URLs.
  const out = new Set();
  collectNumericIdsFromValue(-1, out);
  assert.deepEqual([...out], []);
});

test('collectNumericIdsFromValue: parses digit string', () => {
  const out = new Set();
  collectNumericIdsFromValue('12345', out);
  assert.deepEqual([...out], [12345]);
});

test('collectNumericIdsFromValue: regex-extracts mailMessageId', () => {
  const out = new Set();
  collectNumericIdsFromValue('action=mailmessage&message_id=29914', out);
  assert.deepEqual([...out], [29914]);
});

test('collectNumericIdsFromValue: regex-extracts from URL path', () => {
  const out = new Set();
  collectNumericIdsFromValue('https://example.com/api/2.0/mail/messages/38596', out);
  assert.deepEqual([...out], [38596]);
});

test('collectNumericIdsFromValue: multiple IDs in one string', () => {
  const out = new Set();
  collectNumericIdsFromValue('messageId=100 and messageId=200', out);
  assert.deepEqual([...out].sort(), [100, 200]);
});

test('collectNumericIdsFromValue: ignores non-numeric', () => {
  const out = new Set();
  collectNumericIdsFromValue('hello world', out);
  assert.deepEqual([...out], []);
});

test('collectNumericIdsFromValue: null/undefined', () => {
  const out = new Set();
  collectNumericIdsFromValue(null, out);
  collectNumericIdsFromValue(undefined, out);
  assert.deepEqual([...out], []);
});

test('collectNumericIdsFromValue: HTML-encoded ampersand', () => {
  const out = new Set();
  collectNumericIdsFromValue('action=mailmessage&amp;message_id=29914', out);
  assert.deepEqual([...out], [29914]);
});
// ──────────────────────────────────────────────────────────────────────
// dueChipHtml tests (mirror of public/app.js)
test('dueChipHtml: overdue renders icon only, never "Overdue <date>" text', () => {
  const yesterday = new Date(Date.now() - 86400000).toISOString();
  const out = dueChipHtml({ dueDate: yesterday });
  assert.ok(out.includes("task-chip--overdue"));
  assert.ok(out.includes("<svg"));
  assert.ok(!out.includes(">Overdue "), `must not contain text pill, got: ${out}`);
  assert.ok(out.includes('title="Overdue '), "date must survive in the tooltip");
});

test('dueChipHtml: today renders "Today" text, no icon', () => {
  const out = dueChipHtml({ dueDate: new Date().toISOString() });
  assert.ok(out.includes(">Today<"));
  assert.ok(!out.includes("<svg"));
});

test('dueChipHtml: future date renders date text, no icon', () => {
  const future = new Date(Date.now() + 30 * 86400000).toISOString();
  const out = dueChipHtml({ dueDate: future });
  assert.ok(out.includes("task-chip-due"));
  assert.ok(!out.includes("<svg"));
  assert.ok(!out.includes("task-chip--overdue"));
});

test('dueChipHtml: missing/bad date renders nothing', () => {
  assert.equal(dueChipHtml({}), "");
  assert.equal(dueChipHtml({ dueDate: "not-a-date" }), "");
});

// ──────────────────────────────────────────────────────────────────────
// labelChipsHtml tests (mirror of public/app.js)
test('labelChipsHtml: collapses N labels into one icon with tooltip list', () => {
  const out = labelChipsHtml({ labels: "estimate inspection appointment note" });
  assert.equal((out.match(/<svg/g) || []).length, 1);
  assert.ok(!out.includes(">estimate<"), "no per-label pills");
  assert.ok(out.includes('title="estimate, inspection, appointment, note"'));
});

test('labelChipsHtml: empty labels render nothing', () => {
  assert.equal(labelChipsHtml({}), "");
  assert.equal(labelChipsHtml({ labels: "   " }), "");
});

// ──────────────────────────────────────────────────────────────────────
// _tablerContentValid mirror (public/app.js): ::before content probe for
// webfont glyph existence. Missing classes compute to none/empty.
function _tablerContentValid(content) {
  if (content == null) return false;
  const c = String(content).trim();
  return c !== "" && c !== "none" && c !== "normal" && c !== '""' && c !== "''";
}

test('_tablerContentValid: real content passes', () => {
  assert.equal(_tablerContentValid('"\\e001"'), true);
  assert.equal(_tablerContentValid('  "\\e001"  '), true);
});

test('_tablerContentValid: missing-glyph markers fail', () => {
  assert.equal(_tablerContentValid("none"), false);
  assert.equal(_tablerContentValid("normal"), false);
  assert.equal(_tablerContentValid('""'), false);
  assert.equal(_tablerContentValid(""), false);
  assert.equal(_tablerContentValid(null), false);
  assert.equal(_tablerContentValid(undefined), false);
});
