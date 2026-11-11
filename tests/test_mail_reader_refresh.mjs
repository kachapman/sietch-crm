// Regression test: an open mail reader (and its Reply/Forward action bar) must
// survive a background inbox sync.
//
// Live bug 2026-11-11 (acceptance run 20261111-062445Z, S12): Playwright had
// resolved `#mail-list .mail-item button[title="Forward"]`, started clicking
// it, and the node was detached because a background sync changed the list
// signature and `renderMailList` did `list.innerHTML = ""`. v2.5.13
// (`shouldSkipMailListRender`) only helped while the signature was unchanged;
// `restoreExpandedState()` then created NEW buttons, which cannot rescue an
// in-flight click.
//
// This test executes the real shipped functions (renderMailList /
// restoreExpandedState / renderMailEmbedPanel) extracted from public/app.js,
// against a tiny dependency-free DOM (the repo has no jsdom — see
// tests/test_template_dropdown.mjs for the same style), so it fails on the
// innerHTML-wipe implementation and passes on the keyed reconcile.
//
// Run with:  node --test tests/test_mail_reader_refresh.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const appSrc = fs.readFileSync(path.join(ROOT, 'public', 'app.js'), 'utf8');

// ── tiny DOM ──────────────────────────────────────────────────────────────
// Only what the mail list + embed panel touch: elements, class/id/data-*,
// a few selectors (.class, #id, tag, [data-id="x"]), listeners, insertBefore
// and isConnected (the property Playwright checks before clicking).

const VOID_TAGS = new Set(['input', 'br', 'img', 'hr', 'meta', 'link', 'source']);

function decodeEntities(text) {
  return String(text).replace(/&(#\d+|#x[0-9a-f]+|[a-z]+);/gi, (m, code) => {
    const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
    if (code[0] !== '#') return named[code.toLowerCase()] ?? m;
    if (code[1] === 'x' || code[1] === 'X') return String.fromCodePoint(parseInt(code.slice(2), 16));
    return String.fromCodePoint(parseInt(code.slice(1), 10));
  });
}

function camelToKebab(name) {
  return String(name).replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
}

function parseSelector(selector) {
  // Compound selectors only (no combinators) — all this test needs.
  return String(selector).split(',').map((part) => {
    const s = part.trim();
    const m = s.match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/);
    if (!m) throw new Error('unsupported selector: ' + s);
    const out = { tag: m[1] ? m[1].toUpperCase() : null, classes: [], id: null, attrs: [] };
    for (const piece of m[2].match(/[.#][\w-]+|\[[\w-]+(?:="[^"]*")?\]/g) || []) {
      if (piece[0] === '.') out.classes.push(piece.slice(1));
      else if (piece[0] === '#') out.id = piece.slice(1);
      else {
        const a = piece.slice(1, -1).match(/^([\w-]+)(?:="([^"]*)")?$/);
        out.attrs.push({ name: a[1], value: a[2] });
      }
    }
    return out;
  });
}

class FakeTextNode {
  constructor(text) {
    this.nodeType = 3;
    this.parentNode = null;
    this._text = String(text);
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument.body || node === this.ownerDocument.documentElement;
  }
}

class FakeElement {
  constructor(doc, tagName) {
    this.ownerDocument = doc;
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attrs = new Map();
    this._listeners = new Map();
    this._text = '';
    this._classes = new Set();
    this.style = { cssText: '' };
    this.checked = false;
    this.value = '';
    const el = this;
    this.classList = {
      add: (...names) => { names.forEach((n) => el._classes.add(n)); },
      remove: (...names) => { names.forEach((n) => el._classes.delete(n)); },
      contains: (name) => el._classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !el._classes.has(name) : !!force;
        if (on) el._classes.add(name); else el._classes.delete(name);
        return on;
      },
      get length() { return el._classes.size; },
    };
    this.dataset = new Proxy({}, {
      get: (_t, key) => el.attrs.get('data-' + camelToKebab(key)),
      set: (_t, key, value) => { el.attrs.set('data-' + camelToKebab(key), String(value)); return true; },
    });
  }

  get className() { return Array.from(this._classes).join(' '); }
  set className(v) {
    this._classes.clear();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this._classes.add(c));
  }
  get id() { return this.attrs.get('id') || ''; }
  set id(v) { this.attrs.set('id', String(v)); }
  get title() { return this.attrs.get('title') || ''; }
  set title(v) { this.attrs.set('title', String(v)); }
  get type() { return this.attrs.get('type') || ''; }
  set type(v) { this.attrs.set('type', String(v)); }

  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }

  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.children;
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument.body || node === this.ownerDocument.documentElement;
  }

  get outerHTML() { return this.innerHTML; }

  get innerHTML() {
    return this.children.map((child) => {
      if (child.nodeType === 3) return child.textContent;
      const cls = child.className ? ` class="${child.className}"` : '';
      return `<${child.tagName.toLowerCase()}${cls}>${child.innerHTML}</${child.tagName.toLowerCase()}>`;
    }).join('');
  }

  set innerHTML(html) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._text = '';
    parseHtml(this.ownerDocument, String(html)).forEach((node) => this.appendChild(node));
  }

  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join('');
  }

  set textContent(value) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._text = String(value);
  }

  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === 'class') this.className = value;
  }
  getAttribute(name) {
    if (name === 'class') return this.className || null;
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }
  hasAttribute(name) { return this.attrs.has(name); }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.children.push(node);
    return node;
  }

  insertBefore(node, ref) {
    if (!ref) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    const at = this.children.indexOf(ref);
    if (at < 0) return this.appendChild(node);
    node.parentNode = this;
    this.children.splice(at, 0, node);
    return node;
  }

  prepend(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.children.unshift(node);
    return node;
  }

  removeChild(node) {
    const at = this.children.indexOf(node);
    if (at >= 0) {
      this.children.splice(at, 1);
      node.parentNode = null;
    }
    return node;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }

  removeEventListener(type, fn) {
    const list = this._listeners.get(type) || [];
    this._listeners.set(type, list.filter((x) => x !== fn));
  }

  dispatchEvent(event) {
    const ev = Object.assign({
      type: 'click',
      target: this,
      currentTarget: this,
      preventDefault() {},
      stopPropagation() {},
      stopImmediatePropagation() {},
    }, event || {});
    for (const fn of (this._listeners.get(ev.type) || []).slice()) fn(ev);
    return true;
  }

  click() { return this.dispatchEvent({ type: 'click' }); }

  focus() {}
  select() {}
  scrollIntoView() {}

  matches(selector) {
    return parseSelector(selector).some((s) => {
      if (s.tag && s.tag !== this.tagName) return false;
      if (s.id && s.id !== this.id) return false;
      if (s.classes.some((c) => !this._classes.has(c))) return false;
      return s.attrs.every((a) => this.attrs.get(a.name) === a.value);
    });
  }

  closest(selector) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  querySelectorAll(selector) {
    const groups = parseSelector(selector);
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child.nodeType !== 1) continue;
        if (groups.some((g) => child.matches(selector))) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function parseHtml(doc, html) {
  const roots = [];
  const stack = [{ children: roots, tag: null }];
  const add = (node) => {
    const parent = stack[stack.length - 1];
    const el = parent.el;
    if (el) {
      node.parentNode = el;
      el.children.push(node);
    } else {
      node.parentNode = null;
      parent.children.push(node);
    }
  };

  const tokenRe = /<!--[\s\S]*?-->|<\/\s*([a-zA-Z][\w:-]*)\s*>|<([a-zA-Z][\w:-]*)((?:\s+[\w:-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = tokenRe.exec(html))) {
    if (m[1]) {
      // closing tag
      if (stack.length > 1) stack.pop();
      continue;
    }
    if (m[2]) {
      const el = doc.createElement(m[2]);
      const attrRe = /([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = attrRe.exec(m[3] || ''))) {
        const name = a[1].toLowerCase();
        const value = a[2] ?? a[3] ?? a[4] ?? '';
        el.setAttribute(name, decodeEntities(value));
      }
      add(el);
      const selfClosing = m[4] === '/' || VOID_TAGS.has(el.tagName.toLowerCase());
      if (!selfClosing) stack.push({ el, children: [] });
      continue;
    }
    if (m[5]) {
      const text = decodeEntities(m[5]);
      if (text.trim()) add(new FakeTextNode(text));
    }
  }
  return roots;
}

class FakeDocument {
  constructor() {
    this.nodeType = 9;
    this.documentElement = new FakeElement(this, 'html');
    this.body = new FakeElement(this, 'body');
    this.documentElement.appendChild(this.body);
  }
  createElement(tag) { return new FakeElement(this, tag); }
  createTextNode(text) { return new FakeTextNode(text); }
  querySelector(selector) { return this.body.querySelector(selector); }
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
  addEventListener() {}
  removeEventListener() {}
}

// ── load the shipped functions ────────────────────────────────────────────
// The mail list code is module-scope in app.js, so slice the real source and
// run it in a function with the (pure) helpers stubbed. Nothing here is
// copied from app.js — a drift in the shipped functions is what gets tested.

function slice(startMarker, endMarker) {
  const start = appSrc.indexOf(startMarker);
  assert.ok(start >= 0, `app.js lost ${startMarker}`);
  const end = appSrc.indexOf(endMarker, start);
  assert.ok(end > start, `app.js lost the end of ${startMarker}`);
  return appSrc.slice(start, end);
}

const source = [
  slice('function getMailMessageIsRead(m) {', '// --- Mail modal stability (2026-11-10) ---'),
  slice('function mailSnippetText(m) {', 'function updateMailSelectedInfo() {'),
  slice('function renderMailEmbedPanel(panel, mail, messageId,', 'async function refreshOpenMailPanels() {'),
].join('\n');

function createSandbox() {
  const document = new FakeDocument();
  const mailState = {
    messages: [],
    selected: new Set(),
    expandedIds: new Set(),
    expandedContent: {},
    listRenderCount: 0,
    listSignature: null,
  };
  const mailDashboardReadIds = new Set();
  const replyCalls = [];
  const sentMarkRead = [];
  let pendingMarkRead = null;

  const escapeHtml = (value) => String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const fetchMailMessage = async (id) => {
    const msg = mailState.messages.find((m) => String(m.id) === String(id)) || {};
    return Object.assign({}, msg, { body_text: msg.body_text || 'Body text' });
  };

  const factory = new Function(
    'document', 'mailState', 'mailDashboardReadIds', '$',
    'escapeHtml', 'decodeHtmlEntities', 'formatMailDate', 'mailDisplayTimezone',
    'portalMailMessageUrl', 'renderInlineReply', 'fetchMailMessage',
    'markMailMessageRead', 'saveMailDashboardReadIds', 'openDraftInComposeById',
    'updateMailSelectedInfo', 'normalizeMailMessage', 'formatPreviewDateTime',
    'pickMailBodyForDisplay', 'mailBodyIframeSrcdoc',
    'api', 'openEmailPreviewModal', 'showToast', 'loadMailMessagesForModal',
    'confirm', 'window', 'setTimeout',
    `${source}
     return { renderMailList, restoreExpandedState, renderMailEmbedPanel, mailSnippetText };`,
  );

  const api = factory(
    document,
    mailState,
    mailDashboardReadIds,
    (sel) => document.querySelector(sel),
    escapeHtml,
    (text) => String(text ?? ''),
    (raw) => String(raw == null ? '' : raw),
    () => 'America/New_York',
    () => '',
    (panel, mode, messageId) => { replyCalls.push({ mode, messageId: String(messageId), panel }); },
    fetchMailMessage,
    async (id) => { sentMarkRead.push(String(id)); },
    () => {},
    () => {},
    () => {},
    (mail) => mail,
    (raw) => String(raw == null ? '' : raw),
    () => ({ mode: 'text', content: 'Hello body' }),
    () => '',
    async () => ({}),
    () => {},
    () => {},
    async () => {},
    () => true,
    { innerWidth: 1400, innerHeight: 900 },
    setTimeout,
  );

  const list = document.createElement('div');
  list.id = 'mail-list';
  document.body.appendChild(list);

  return { document, list, mailState, api, replyCalls, sentMarkRead, get pendingMarkRead() { return pendingMarkRead; } };
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

function message(overrides = {}) {
  return Object.assign({
    id: 101,
    from_addr: 'sender@example.com',
    to_addr: 'me@example.com',
    subject: 'Re: roof claim',
    date: '2026-11-11 10:00:00',
    body_text: 'Here are the photos you asked for.',
    read: false,
  }, overrides);
}

function actionButton(detail, title) {
  const btn = detail.querySelectorAll('button').find((b) => b.title === title);
  assert.ok(btn, `no ${title} button in the open reader`);
  return btn;
}

async function openReader(sandbox, id) {
  const item = sandbox.list.querySelector(`.mail-item[data-id="${id}"]`);
  assert.ok(item, 'mail item did not render');
  item.querySelector('.mail-row').click();
  await flush();
  const detail = item.querySelector('.mail-detail');
  assert.equal(detail.classList.contains('hidden'), false, 'reader did not open');
  return { item, detail };
}

// ── tests ─────────────────────────────────────────────────────────────────

test('a background sync keeps the open reader + Reply/Forward nodes connected', async () => {
  const sandbox = createSandbox();
  const original = message();

  // 1. initial list (user-initiated load)
  sandbox.mailState.messages = [original];
  sandbox.api.renderMailList(sandbox.mailState.messages, { rebuild: true });

  // 2. open the reader → real renderMailEmbedPanel creates the action bar
  const { item, detail } = await openReader(sandbox, '101');
  const forward = actionButton(detail, 'Forward');
  const reply = actionButton(detail, 'Reply');

  // 3. a new message arrives: the list signature CHANGES (the v2.5.13 skip
  //    no longer applies) and the background path re-renders + restores.
  const fresh = message({ id: 102, subject: 'New arrival', read: false });
  sandbox.mailState.messages = [fresh, original];
  sandbox.api.renderMailList(sandbox.mailState.messages); // loadMailMessagesForModal(true)
  sandbox.api.restoreExpandedState();                     // the same background path

  // 4. the SAME item and the SAME button nodes must still be in the document
  // Compare identity via a boolean: a failed strictEqual here would make the
  // reporter deep-inspect two whole DOM trees.
  assert.ok(sandbox.list.querySelector('.mail-item[data-id="101"]') === item,
    'the open item must not be replaced');
  assert.equal(forward.isConnected, true, 'Forward was detached by the sync');
  assert.equal(reply.isConnected, true, 'Reply was detached by the sync');
  assert.ok(detail.querySelectorAll('button').filter((b) => b.title === 'Forward')[0] === forward,
    'Forward was recreated instead of kept');
  assert.ok(sandbox.list.querySelector('.mail-item[data-id="102"]'),
    'the new message must still appear');

  // 5. a click after the refresh still runs the original handler
  forward.click();
  reply.click();
  assert.deepEqual(sandbox.replyCalls.map((c) => c.mode), ['forward', 'reply']);
  assert.deepEqual(sandbox.replyCalls.map((c) => c.messageId), ['101', '101']);
});

test('a sync that changes the row itself (read flag, order) still keeps the action bar', async () => {
  const sandbox = createSandbox();
  const first = message();
  const second = message({ id: 102, subject: 'Older', date: '2026-11-10 09:00:00' });

  sandbox.mailState.messages = [first, second];
  sandbox.api.renderMailList(sandbox.mailState.messages, { rebuild: true });
  const { item, detail } = await openReader(sandbox, '101');
  const forward = actionButton(detail, 'Forward');

  // Message 101 becomes read (mark-read / server sync) and moves down the list.
  sandbox.mailState.messages = [second, Object.assign({}, first, { read: true })];
  sandbox.api.renderMailList(sandbox.mailState.messages);
  sandbox.api.restoreExpandedState();

  assert.ok(sandbox.list.querySelector('.mail-item[data-id="101"]') === item,
    'the item node must survive a row update');
  assert.equal(forward.isConnected, true, 'Forward was detached by the row update');
  assert.equal(item.querySelector('.mail-row').classList.contains('mail-row-read'), true,
    'the read flag must still update');
  // order really changed
  const ids = sandbox.list.querySelectorAll('.mail-item').map((el) => el.dataset.id);
  assert.deepEqual(ids, ['102', '101']);
});

test('a user-initiated rebuild is still allowed to replace the list', async () => {
  const sandbox = createSandbox();
  sandbox.mailState.messages = [message()];
  sandbox.api.renderMailList(sandbox.mailState.messages, { rebuild: true });
  const { item } = await openReader(sandbox, '101');

  sandbox.api.renderMailList(sandbox.mailState.messages, { rebuild: true });
  assert.ok(sandbox.list.querySelector('.mail-item[data-id="101"]') !== item,
    'an explicit reload (folder/tab/search/Refresh) may rebuild the list');
});
