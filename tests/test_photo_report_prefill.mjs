// Tests for photo-report form prefill mapping (2026-10-23).
//
// The form took Customer Name + Address from the LINKED CONTACT — on Jill
// Carpenter's deal that's the GC ("A+ Restoration GC", PO Box 189), not the
// homeowner. Correct mapping: Customer Name = project title; Address = the
// deal's own address custom field, falling back to contact address.
//
// Run with:  node --test tests/test_photo_report_prefill.mjs
//
// Mirrors the mapping head of renderPhotoReportForm. If you change one,
// change the other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function prefill(project, dealFields) {
  const contact = project.contact || {};
  const customFields = Array.isArray(dealFields) && dealFields.length ? dealFields : (project.customFields || []);
  const fieldText = (match) => {
    for (const cf of customFields) {
      const label = String(cf.label || '').toLowerCase();
      if (cf.value != null && String(cf.value).trim() && match.test(label)) return String(cf.value).trim();
    }
    return '';
  };
  const fullAddress = fieldText(/address|location|site|property|street/) ||
    [contact.addressStreet, contact.addressCity, contact.addressState, contact.addressZip].filter(Boolean).join(', ');
  const projectTitle = project.title || 'Project';
  const customerName = projectTitle;
  return { customerName, fullAddress, fileName: `Photo Report - ${projectTitle} - ${customerName}` };
}

// Jill Carpenter fixture: linked contact is the GC, deal carries the site.
const PROJECT = {
  title: 'Jill Carpenter',
  contact: {
    displayName: 'A+ Restoration GC', addressStreet: 'PO Box 189',
    addressCity: 'Davis', addressState: 'Illinois', addressZip: '61019',
  },
};
const FIELDS = [
  { key: 'field_3', label: 'Date of Loss', value: '04/17/2026' },
  { key: 'field_9', label: 'Property Address', value: '12170 Leech Road, Gramercy, LA' },
];

test('customer name is the project title, never the GC contact', () => {
  const out = prefill(PROJECT, FIELDS);
  assert.equal(out.customerName, 'Jill Carpenter');
  assert.ok(!out.customerName.includes('Restoration'), out.customerName);
});

test('address prefers the deal field over the contact address', () => {
  const out = prefill(PROJECT, FIELDS);
  assert.ok(out.fullAddress.includes('Leech Road'), out.fullAddress);
  assert.ok(!out.fullAddress.includes('PO Box'), out.fullAddress);
});

test('no deal address field falls back to contact address', () => {
  const out = prefill(PROJECT, []);
  assert.ok(out.fullAddress.includes('PO Box 189'), out.fullAddress);
});

test('file name follows the corrected customer', () => {
  const out = prefill(PROJECT, FIELDS);
  assert.equal(out.fileName, 'Photo Report - Jill Carpenter - Jill Carpenter');
});

test('app.js prefill matches the mirror', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  const at = src.indexOf('function renderPhotoReportForm');
  const region = src.slice(at, at + 2200);
  assert.ok(region.includes('const customerName = projectTitle'), 'customer must come from project title');
  assert.ok(region.includes('dealFields'), 'form must receive fetched deal fields');
  assert.ok(!region.includes('contact.displayName || ""'), 'contact-name fallback must be gone');
});

test('photo grid packs rows at the top', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const css = fs.readFileSync(path.join(here, '..', 'public', 'styles.css'), 'utf8');
  const at = css.indexOf('.opp-photos-grid');
  assert.ok(at >= 0, 'grid rule missing');
  assert.ok(css.slice(at, at + 600).includes('align-content: start'), 'rows must pack at top');
});
