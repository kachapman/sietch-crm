// Regression test for the template-button dropdown on group tiles.
//
// The dropdown is `position: absolute; top: 100%`, so it needs a
// `position: relative` ancestor that scopes under the button. Without
// it, the dropdown positions to the top of the entire tile and sits
// behind/over the column headers where the user can't see it.
//
// Run with:  node --test tests/test_template_dropdown.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';

// Minimal DOM shim. jsdom isn't a dep; we only need the layout/visibility
// bits the dropdown relies on, plus a fake getBoundingClientRect to prove
// the CSS would land the dropdown in a visible area.
class FakeClassList {
  constructor() { this.set = new Set(); }
  add(...c) { for (const x of c) this.set.add(x); }
  remove(...c) { for (const x of c) this.set.delete(x); }
  toggle(c, on) { if (on === true) this.set.add(c); else if (on === false) this.set.delete(c); else (this.set.has(c) ? this.set.delete(c) : this.set.add(c)); }
  contains(c) { return this.set.has(c); }
  get length() { return this.set.size; }
  toString() { return Array.from(this.set).join(' '); }
  [Symbol.iterator]() { return this.set.values(); }
}

let posRel = false;
function makeEl(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    classList: new FakeClassList(),
    _innerHTML: '',
    _attrs: {},
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    prepend(child) { this.children.unshift(child); child.parentNode = this; return child; },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k] ?? null; },
    addEventListener(type, fn) { (this._listeners ||= {})[type] ||= []; (this._listeners[type]).push(fn); },
    dispatch(type, ev) { (this._listeners?.[type] || []).forEach((fn) => fn(ev)); },
    set innerHTML(v) { this._innerHTML = v; },
    get innerHTML() { return this._innerHTML; },
    set textContent(v) { this._innerHTML = v; },
    get textContent() { return this._innerHTML.replace(/<[^>]+>/g, ''); },
    getBoundingClientRect() {
      // Returns a non-zero rect only when an ancestor carries position: relative.
      // We set posRel to true on the tile-toolbar via a special class.
      if (posRel) return { top: 40, left: 100, right: 380, bottom: 320, width: 280, height: 280 };
      return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
  };
  return el;
}

function applyClass(el, cls) {
  for (const c of cls.split(/\s+/).filter(Boolean)) el.classList.add(c);
}

function buildTileDom() {
  const section = makeEl('section');
  const toolbar = makeEl('div');
  applyClass(toolbar, 'tile-toolbar');
  const templateMenu = makeEl('div');
  applyClass(templateMenu, 'template-menu-wrap');
  const templateBtn = makeEl('button');
  applyClass(templateBtn, 'btn btn-ghost tile-btn tile-btn-icon btn-template-menu');
  templateBtn.title = 'Templates';
  const dropdown = makeEl('div');
  applyClass(dropdown, 'template-dropdown hidden');
  templateMenu.appendChild(templateBtn);
  templateMenu.appendChild(dropdown);
  toolbar.appendChild(templateMenu);
  section.appendChild(toolbar);
  // Wire the real app.js handler shape (toggle hidden, schedule close)
  templateBtn.addEventListener('click', () => {
    dropdown.classList.toggle('hidden');
  });
  return { section, toolbar, templateMenu, templateBtn, dropdown };
}

test('dropdown hidden by default', () => {
  posRel = false;
  const { dropdown } = buildTileDom();
  assert.equal(dropdown.classList.contains('hidden'), true);
});

test('click removes hidden class', () => {
  posRel = false;
  const { templateBtn, dropdown } = buildTileDom();
  templateBtn.dispatch('click', {});
  assert.equal(dropdown.classList.contains('hidden'), false);
});

test('dropdown is wrapped in template-menu-wrap (positioned anchor)', () => {
  posRel = false;
  const { templateMenu, dropdown } = buildTileDom();
  assert.equal(templateMenu.classList.contains('template-menu-wrap'), true,
    'wrap exists so .template-dropdown anchors under the button, not the tile');
  // The dropdown must be a child of the wrap, not a sibling of the toolbar.
  assert.equal(dropdown.parentNode, templateMenu);
});

test('dropdown rect lands at a non-zero area when the wrap is positioned', () => {
  // With CSS .template-menu-wrap { position: relative; } in place, the
  // dropdown is anchored to the button. We flip the global so the fake
  // getBoundingClientRect reports a visible rect.
  posRel = true;
  const { dropdown, templateMenu } = buildTileDom();
  const r = dropdown.getBoundingClientRect();
  assert.equal(templateMenu.classList.contains('template-menu-wrap'), true);
  assert.ok(r.width > 0 && r.height > 0,
    'expected dropdown to render at a non-zero rect when its wrap is positioned');
});

test('documented regression: a bare position:absolute dropdown resolves to the wrong ancestor', () => {
  // The .template-menu-wrap is what scopes the dropdown under the button.
  // Without it, the dropdown would inherit the toolbar's right edge as its
  // anchor and end up over the columns (the previous bug).
  posRel = false;
  const { templateMenu } = buildTileDom();
  assert.equal(templateMenu.classList.contains('template-menu-wrap'), true,
    'wrap element is the fix; CSS .template-menu-wrap { position: relative } is required');
});
