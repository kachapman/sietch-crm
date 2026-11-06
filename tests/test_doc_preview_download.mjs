// Preview download + byte sniff. Synthetic labels only.
//
// The toolbar Download used to be an <a download> inside a sandboxed iframe
// that does not have allow-downloads, so the browser dropped it. The parent
// now saves the file, and only after checking origin and frame.
//
// Run with: node --test tests/test_doc_preview_download.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const appSrc = fs.readFileSync(path.join(root, 'public', 'app.js'), 'utf8');
const previewSrc = fs.readFileSync(path.join(root, 'public', 'doc-preview.html'), 'utf8');
const indexSrc = fs.readFileSync(path.join(root, 'public', 'index.html'), 'utf8');

function extractFunction(src, name) {
  const marker = `function ${name}(`;
  const start = src.indexOf(marker);
  assert.ok(start >= 0, `${name} missing`);
  let i = src.indexOf('{', start);
  let depth = 0;
  for (; i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced ${name}`);
}

function loadFunction(src, name, sandboxExtras = {}) {
  const sandbox = { ...sandboxExtras };
  vm.createContext(sandbox);
  vm.runInContext(`${extractFunction(src, name)}\n;globalThis.__fn = ${name};`, sandbox);
  return sandbox.__fn;
}

function u8(text, prefix = []) {
  return new Uint8Array([...prefix, ...Buffer.from(text, 'utf8')]);
}

const classify = loadFunction(previewSrc, 'classifyPreview');
const filenameFromPreview = loadFunction(previewSrc, 'filenameFromContentDisposition');
const filenameFromApp = loadFunction(appSrc, 'filenameFromContentDisposition');

test('pdf bytes win over a docx title and a word content-type', () => {
  const kind = classify(
    u8('%PDF-1.4\n1 0 obj\n'),
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'Estimate.docx',
  );
  assert.equal(kind, 'pdf');
});

test('extensionless title with pdf bytes is a pdf', () => {
  assert.equal(classify(u8('%PDF-1.4\n'), 'application/octet-stream', 'Estimate Packet'), 'pdf');
});

test('uppercase and doubled suffixes do not override pdf bytes', () => {
  assert.equal(classify(u8('%PDF-1.4\n'), 'application/octet-stream', 'PACKET.PDF'), 'pdf');
  assert.equal(classify(u8('%PDF-1.4\n'), 'application/pdf', 'packet.pdf.docx'), 'pdf');
});

test('a pdf label on non-pdf bytes is not a pdf', () => {
  assert.equal(classify(u8('not a pdf\n'), 'application/pdf', 'FILE.PDF'), 'unknown');
  assert.equal(classify(u8('not a pdf\n'), 'application/octet-stream', 'FILE.PDF'), 'unknown');
});

test('docx zip prefix wins over a pdf title and pdf content-type', () => {
  const kind = classify(
    u8('word/document.xml', [0x50, 0x4b, 0x03, 0x04]),
    'application/pdf',
    'Report.pdf',
  );
  assert.equal(kind, 'docx');
});

test('sheet zip prefix is a sheet even when the title says pdf', () => {
  const kind = classify(
    u8('xl/workbook.xml', [0x50, 0x4b, 0x03, 0x04]),
    'application/pdf',
    'grid.PDF',
  );
  assert.equal(kind, 'sheet');
});

test('png bytes are an image even when titled as a pdf', () => {
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0]);
  assert.equal(classify(png, 'application/pdf', 'scan.pdf'), 'image');
});

test('csv content-type is a sheet and plain text is text', () => {
  assert.equal(classify(u8('a,b\n1,2\n'), 'text/csv', 'Export'), 'sheet');
  assert.equal(classify(u8('hello\n'), 'text/plain', 'Field notes'), 'text');
});

test('html is not rendered as text', () => {
  assert.equal(classify(u8('<script>alert(1)</script>'), 'text/html', 'notes.html'), 'unknown');
});

function assertSafeFilename(parse) {
  assert.equal(
    parse('inline; filename="fallback.pdf"; filename*=UTF-8\'\'Q4%20Packet.pdf'),
    'Q4 Packet.pdf',
  );
  assert.equal(parse('attachment; filename="report.pdf"'), 'report.pdf');
  const injected = parse('inline; filename="evil.pdf"\r\nSet-Cookie: a=b');
  assert.equal(injected.includes('\r') || injected.includes('\n'), false);
  assert.equal(injected.includes('Set-Cookie'), false);
  assert.equal(parse("attachment; filename*=UTF-8''..%2F..%2Fetc%2Fpasswd.pdf"), 'passwd.pdf');
  assert.equal(parse(''), 'document');
  assert.equal(parse('inline; filename="../../etc/passwd.pdf"'), 'passwd.pdf');
}

test('preview and parent parse the same safe filename', () => {
  assertSafeFilename(filenameFromPreview);
  assertSafeFilename(filenameFromApp);
});

test('parent accepts a download request only from the open preview frame', () => {
  const child = {};
  const other = {};
  const iframe = { contentWindow: child };
  const fn = loadFunction(appSrc, 'acceptPreviewDownloadMessage', {
    window: { location: { origin: 'https://crm.example' } },
  });
  const good = {
    origin: 'https://crm.example',
    source: child,
    data: { type: 'docs-preview-download', docId: '5' },
  };
  assert.equal(fn(good, iframe, 5), true);
  assert.equal(fn(good, iframe, '5'), true);
  assert.equal(fn({ ...good, origin: 'https://evil.example' }, iframe, 5), false);
  assert.equal(fn({ ...good, source: other }, iframe, 5), false);
  assert.equal(fn({ ...good, data: { type: 'docs-preview-download', docId: '9' } }, iframe, 5), false);
  assert.equal(fn({ ...good, data: { type: 'docs-preview-fallback', docId: '5' } }, iframe, 5), false);
  assert.equal(fn({ ...good, data: { type: 'docs-preview-download', docId: { toString() { return '5'; } } } }, iframe, 5), false);
  assert.equal(fn(good, iframe, null), false);
});

test('preview posts the download to the parent origin and does not use an anchor download', () => {
  const click = previewSrc.slice(
    previewSrc.indexOf('downloadBtn.addEventListener'),
    previewSrc.indexOf('function showStatus'),
  );
  assert.match(click, /docs-preview-download/);
  assert.match(click, /window\.location\.origin/);
  assert.equal(click.includes('postMessage'), true);
  assert.equal(click.includes('"*') || click.includes("'*"), false);
  assert.equal(click.includes('a.download = titleHint'), false);
  assert.equal(click.includes('allow-downloads'), false);
  const posts = previewSrc.match(/postMessage\([\s\S]*?\);/g) || [];
  assert.ok(posts.length >= 1);
  for (const call of posts) {
    assert.equal(call.includes('"*'), false);
    assert.equal(call.includes("'*"), false);
    assert.match(call, /window\.location\.origin/);
  }
});

test('both parents check origin, frame, and the open document before downloading', () => {
  const docs = appSrc.indexOf('docs-preview-download');
  assert.ok(docs > 0);
  const occurrences = appSrc.split('if (!acceptPreviewDownloadMessage(ev, iframe,').length - 1;
  assert.equal(occurrences, 2);
  assert.match(appSrc, /downloadDocumentAuthenticated\(docsState\.previewDocId\)/);
  assert.match(appSrc, /downloadDocumentAuthenticated\(previewDocsState\.previewDocId\)/);
  const helper = extractFunction(appSrc, 'downloadDocumentAuthenticated');
  assert.match(helper, /credentials:\s*"same-origin"/);
  assert.match(helper, /\?download=1/);
  assert.match(helper, /filenameFromContentDisposition/);
  assert.equal(helper.includes('titleHint'), false);
});

test('list and context downloads still hit the document endpoint with auth', () => {
  const downloadDoc = extractFunction(appSrc, 'downloadDoc');
  assert.match(downloadDoc, /downloadDocumentAuthenticated\(docId\)/);
  assert.equal(downloadDoc.includes('a.download = title'), false);
  assert.match(appSrc, /\/api\/v2\/documents\/\$\{docId\}\?download=1/);
  assert.match(appSrc, /\/api\/v2\/documents\/batch-download/);
});

function sandboxAttr(src, id) {
  const at = src.indexOf(id);
  assert.ok(at >= 0, id);
  const window = src.slice(at, at + 500);
  const m = window.match(/sandbox="([^"]+)"/);
  assert.ok(m, `${id} sandbox`);
  return m[1];
}

test('preview iframes stay sandboxed without allow-downloads', () => {
  for (const value of [
    sandboxAttr(indexSrc, 'id="documents-preview-iframe"'),
    sandboxAttr(appSrc, 'id="opp-preview-doc-iframe"'),
  ]) {
    assert.match(value, /allow-same-origin/);
    assert.match(value, /allow-scripts/);
    assert.equal(value.includes('allow-downloads'), false);
  }
});
