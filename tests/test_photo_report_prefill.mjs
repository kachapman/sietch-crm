// Tests for document-generator customer/address mapping (2026-10-23,
// extended: invoice + COC shared helper).
//
// Photo report, invoice, and COC forms all took Customer Name + Address from
// the LINKED CONTACT — frequently the contractor/GC, not the homeowner.
// Shared rule (public/app.js#docGenCustomerAddress): customer = project
// title; street = deal address field, then contact street; city line = deal
// city-ish field, then contact parts only when NO deal address field matched
// (never mix sources across lines; blank beats wrong).
//
// Run with:  node --test tests/test_photo_report_prefill.mjs
//
// Mirrors docGenCustomerAddress. If you change one, change the other.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function docGenCustomerAddress(project, dealFields) {
  project = project || {};
  const contact = project.contact || {};
  const customFields = Array.isArray(dealFields) && dealFields.length ? dealFields : (project.customFields || []);
  const fieldText = (match) => {
    for (const cf of customFields) {
      const label = String(cf.label || '').toLowerCase();
      if (cf.value != null && String(cf.value).trim() && match.test(label)) return String(cf.value).trim();
    }
    return '';
  };
  const street = fieldText(/street|address|site|location|property/) || (contact.addressStreet || '');
  let cityStateZip = fieldText(/city|state|zip|country|county/) || '';
  if (!cityStateZip && !fieldText(/street|address|site|location|property/)) {
    cityStateZip = [contact.addressCity, contact.addressState, contact.addressZip].filter(Boolean).join(', ');
  }
  const customerName = project.title || 'Project';
  return { customerName, street, cityStateZip };
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
  const out = docGenCustomerAddress(PROJECT, FIELDS);
  assert.equal(out.customerName, 'Jill Carpenter');
  assert.ok(!out.customerName.includes('Restoration'), out.customerName);
});

test('address prefers the deal field over the contact address', () => {
  const out = docGenCustomerAddress(PROJECT, FIELDS);
  assert.ok(out.street.includes('Leech Road'), out.street);
  assert.ok(!out.street.includes('PO Box'), out.street);
});

test('no deal address field falls back to contact address, unmixed', () => {
  const out = docGenCustomerAddress(PROJECT, []);
  assert.equal(out.street, 'PO Box 189');
  assert.ok(out.cityStateZip.includes('Davis'), out.cityStateZip);
});

test('deal street without city parts leaves city blank, not GC-mixed', () => {
  const out = docGenCustomerAddress(PROJECT, FIELDS);
  assert.equal(out.cityStateZip, '');
});

test('photo form builds the single address line from the helper', () => {
  const { street, cityStateZip } = docGenCustomerAddress(PROJECT, FIELDS);
  assert.equal([street, cityStateZip].filter(Boolean).join(', '), '12170 Leech Road, Gramercy, LA');
});

test('app.js routes all three generators through the helper', () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'public', 'app.js'), 'utf8');
  assert.ok(src.includes('function docGenCustomerAddress'), 'shared helper missing');
  for (const fn of ['renderPhotoReportForm', 'renderInvoiceForm', 'renderCocForm']) {
    const at = src.indexOf(`function ${fn}`);
    assert.ok(at >= 0, `${fn} missing`);
    assert.ok(src.slice(at, at + 3000).includes('docGenCustomerAddress'), `${fn} must use the shared helper`);
  }
  assert.ok(!src.includes('contact.displayName || project.title'), 'contact-first customer fallback must be gone');
});
