/**
 * Turn phone numbers and emails in plain text into tel:/mailto: links.
 *
 * ISO dates (YYYY-MM-DD), US dates (MM/DD/YYYY and M/D/YY with / . or -),
 * and date ranges ("from 2026-11-09 to 2026-11-12") must NOT be treated as
 * phone numbers. The previous matcher (/[+]?[\\d\\s().-]{7,}\\d/) happily
 * ate "2026-11-09" (8 digits) and underlined it as a tel: link in History.
 *
 * Dates are masked before the phone matcher runs so a date sitting next to
 * a real number cannot be swallowed into one greedy match.
 *
 * Pure module, unit-tested by tests/test_linkify_phones_emails.mjs.
 */

export const EMAIL_RE = /([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/g;

// Greedy run of phone-like characters (same shape as the historical matcher).
export const PHONE_CANDIDATE_RE = /(\+?[\d\s().-]{7,}\d)/g;

// ISO YYYY-MM-DD, optionally followed by a time prefix we still don't want
// to treat as a phone. US dates: M/D/YYYY, MM-DD-YYYY, M.D.YY, etc.
export const DATE_RE =
  /(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)|(?<!\d)\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}(?!\d)/g;

/**
 * True when `text` (trimmed) is an ISO or US numeric date and nothing else.
 */
export function isDateLikeNumber(text) {
  const t = String(text || "").trim();
  if (!t) return false;
  DATE_RE.lastIndex = 0;
  const m = t.match(DATE_RE);
  return !!(m && m.length === 1 && m[0] === t);
}

function maskDates(text) {
  const saved = [];
  DATE_RE.lastIndex = 0;
  const masked = String(text ?? "").replace(DATE_RE, (m) => {
    const key = `\u0000DATE${saved.length}\u0000`;
    saved.push(m);
    return key;
  });
  return { masked, saved };
}

function unmaskDates(text, saved) {
  return String(text ?? "").replace(/\u0000DATE(\d+)\u0000/g, (_, i) => saved[Number(i)] ?? "");
}

function phoneAnchor(m) {
  const clean = m.replace(/[^\d+]/g, "");
  const digits = clean.replace(/\+/g, "");
  if (digits.length < 7) return m;
  return `<a href="tel:${clean}" class="preview-link">${m}</a>`;
}

/**
 * Linkify emails and (unless skipPhone) phone numbers in a plain-text string.
 * Returns HTML. Dates and date ranges are left as plain text.
 *
 * @param {string} text
 * @param {{skipPhone?: boolean}} [opts]
 * @returns {string}
 */
export function linkifyPhonesAndEmailsText(text, opts = {}) {
  const skipPhone = !!opts.skipPhone;
  let txt = String(text ?? "");
  txt = txt.replace(EMAIL_RE, (m) =>
    `<a href="mailto:${encodeURIComponent(m)}" class="preview-link">${m}</a>`
  );
  if (skipPhone) return txt;
  const { masked, saved } = maskDates(txt);
  PHONE_CANDIDATE_RE.lastIndex = 0;
  const linked = masked.replace(PHONE_CANDIDATE_RE, (m) => {
    if (m.includes("\u0000")) return m;
    return phoneAnchor(m);
  });
  return unmaskDates(linked, saved);
}

/**
 * Phone substrings that would become tel: links in `text` (dates excluded).
 * Used by tests; emails are ignored.
 */
export function phoneMatches(text) {
  const src = String(text ?? "");
  const { masked, saved } = maskDates(src);
  const out = [];
  PHONE_CANDIDATE_RE.lastIndex = 0;
  let m;
  while ((m = PHONE_CANDIDATE_RE.exec(masked)) !== null) {
    if (m[1].includes("\u0000")) continue;
    const clean = m[1].replace(/[^\d+]/g, "");
    if (clean.replace(/\+/g, "").length < 7) continue;
    out.push(unmaskDates(m[1], saved));
  }
  return out;
}
