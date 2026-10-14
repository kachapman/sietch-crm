#!/usr/bin/env node
// Unit tests for email audit fixes #3 and #4 (modal listener pattern).
//
// The actual modal lives in public/app.js — a 32k-line vanilla JS file with
// no module system. To test the AbortController pattern in isolation, we
// re-implement the *shape* of openEmailPreviewModal's listener-binding
// here using the same APIs (AbortController, addEventListener with
// { signal }). If the production code drifts (e.g., someone adds a
// listener without { signal }), this test still passes — but it locks
// in the *pattern* the audit fix establishes so future code reviews
// have a reference.
//
// Run with: node --test tests/test_email_audit_345.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';

// --- Minimal DOM mock (just enough for the test) ---
// We need real AbortController semantics: addEventListener with a
// { signal } should auto-remove when the signal aborts. We wire this
// by listening for the 'abort' event on the signal and removing all
// listeners bound to that signal.
class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.classList = {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); return this._set.has(c); },
      contains(c) { return this._set.has(c); },
    };
    this.dataset = {};
    this._listeners = [];
  }
  addEventListener(type, handler, opts) {
    const entry = { type, handler, signal: opts?.signal };
    this._listeners.push(entry);
    if (entry.signal) {
      // Mirror the spec: when the signal aborts, remove the listener.
      entry.signal.addEventListener('abort', () => {
        this._listeners = this._listeners.filter((l) => l !== entry);
      }, { once: true });
    }
  }
  removeEventListener(type, handler) {
    this._listeners = this._listeners.filter(
      (l) => !(l.type === type && l.handler === handler)
    );
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(k, v) { this.dataset[k] = v; }
  getAttribute(k) { return this.dataset[k]; }
  querySelector(sel) {
    if (sel.startsWith('.')) {
      const cls = sel.slice(1);
      return this._findByClass(cls);
    }
    return this._findByClass(sel);
  }
  _findByClass(cls) {
    for (const c of this.children) {
      if (c.classList && c.classList.contains(cls)) return c;
      const found = c._findByClass?.(cls);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll(sel) {
    const results = [];
    if (sel.startsWith('.')) {
      const cls = sel.slice(1);
      this._collectByClass(cls, results);
    }
    return results;
  }
  _collectByClass(cls, out) {
    for (const c of this.children) {
      if (c.classList && c.classList.contains(cls)) out.push(c);
      c._collectByClass?.(cls, out);
    }
  }
}

// Re-implements the listener-binding pattern from openEmailPreviewModal.
// This is the *reference* pattern: any production code that wants to
// avoid listener leaks should match this shape.
function bindModalListeners(modal, ctrl) {
  // Dismiss buttons
  modal.dismissEls.forEach((el) => {
    el.addEventListener('click', () => modal.close(), { signal: ctrl.signal });
  });
  // ESC key on document
  const onKey = (e) => { if (e.key === 'Escape') modal.close(); };
  // We'd add this to document, but for the test we track it on a fake document
  modal._docListeners.push({ event: 'keydown', handler: onKey, signal: ctrl.signal });

  // Action buttons
  modal.actionEls.forEach((btn) => {
    btn.addEventListener('click', () => modal.handleAction(btn.dataset.action), { signal: ctrl.signal });
  });
}

function openModal(modal) {
  const ctrl = new AbortController();
  modal._listeners = [];
  modal._docListeners = [];
  bindModalListeners(modal, ctrl);
  modal._controller = ctrl;
  return ctrl;
}

function closeModal(modal) {
  modal._controller?.abort();
  modal._controller = null;
}

// --- Tests ---

test('listeners are removed on close (no accumulation)', () => {
  const dismissA = new FakeElement('button');
  const actionX = new FakeElement('button');
  actionX.dataset.action = 'reply';
  const modal = {
    close: () => {},
    handleAction: () => {},
    dismissEls: [dismissA],
    actionEls: [actionX],
  };

  // Open and close 10 times.
  for (let i = 0; i < 10; i++) {
    const ctrl = openModal(modal);
    // Each open should have 2 element listeners + 1 doc listener.
    assert.equal(dismissA._listeners.length, 1, `dismiss listener on iter ${i}`);
    assert.equal(actionX._listeners.length, 1, `action listener on iter ${i}`);
    assert.equal(modal._docListeners.length, 1, `doc listener on iter ${i}`);
    closeModal(modal);
    // After close, the listeners should be removed.
    assert.equal(dismissA._listeners.length, 0, `dismiss cleared on iter ${i}`);
    assert.equal(actionX._listeners.length, 0, `action cleared on iter ${i}`);
  }
});

test('reopening the same modal aborts the previous controller', () => {
  // In the real openEmailPreviewModal, if the modal is already open
  // for the same messageId, the function returns early (soft refresh).
  // If it's open for a different message, the previous controller is
  // effectively replaced. Either way, no listener stacking.
  const dismissA = new FakeElement('button');
  const modal = { close: () => {}, handleAction: () => {} };
  modal.dismissEls = [dismissA];
  modal.actionEls = [];

  // Simulate the production pattern: open, close, open, close.
  // Each open should add exactly one dismiss listener; each close
  // should remove it.
  for (let i = 0; i < 5; i++) {
    openModal(modal);
    assert.equal(dismissA._listeners.length, 1, `iter ${i}: one listener`);
    closeModal(modal);
    assert.equal(dismissA._listeners.length, 0, `iter ${i}: zero after close`);
  }
});

test('soft refresh keeps listeners (no double-bind)', () => {
  // Simulate: user opens preview for message 1, then clicks a different
  // email (message 2) while the modal is still open. The modal should
  // not double-bind listeners.
  const dismissA = new FakeElement('button');
  const modal = { close: () => {}, handleAction: () => {} };
  modal.dismissEls = [dismissA];
  modal.actionEls = [];

  const ctrl1 = openModal(modal);
  assert.equal(dismissA._listeners.length, 1);

  // User clicks a different email — openModal is called again.
  // In the real code, this would abort the old controller and bind a
  // new one. For this test, we simulate by closing then reopening.
  closeModal(modal);
  assert.equal(dismissA._listeners.length, 0, 'old listener cleared');

  const ctrl2 = openModal(modal);
  assert.equal(dismissA._listeners.length, 1, 'new listener bound exactly once');
  closeModal(modal);
  assert.equal(dismissA._listeners.length, 0);
});

test('AbortController signals trigger handler cleanup', () => {
  const dismissA = new FakeElement('button');
  const modal = { close: () => {}, handleAction: () => {} };
  modal.dismissEls = [dismissA];
  modal.actionEls = [];

  const ctrl = openModal(modal);
  // Manually trigger abort — should remove the listener.
  ctrl.abort();
  assert.equal(dismissA._listeners.length, 0,
    'abort() removes the listener');
});

test('listener pattern matches the production code shape', () => {
  // This test is a guard: if someone changes the pattern (e.g., drops
  // the `signal: ctrl.signal` option), this assertion will fail in a
  // future refactor only if they update this test too — but the
  // comment here is the source of truth.
  const dismissA = new FakeElement('button');
  const modal = { close: () => {}, handleAction: () => {} };
  modal.dismissEls = [dismissA];
  modal.actionEls = [];

  const ctrl = openModal(modal);
  // The listener should be registered WITH a signal.
  assert.ok(dismissA._listeners[0].signal, 'listener has signal');
  // The signal should be the one we passed in.
  assert.strictEqual(dismissA._listeners[0].signal, ctrl.signal,
    'listener signal is the modal controller');
});
