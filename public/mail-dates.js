/**
 * One canonical date/time formatter for mail (and anything else that shows a
 * timestamp to a human).
 *
 * WHY THIS FILE EXISTS (live bug 2026-11-09)
 * ------------------------------------------
 * Ken: an email Outlook shows as received "Mon 11/9/2026 1:20 PM" (EST,
 * 18:20Z) appeared in the Sietch email view as "11/9/2026, 2:20:03 AM".
 * Two independent classes of bug produced that:
 *
 *   1. A timestamp with no offset ("2026-11-09 18:20:03") was fed to
 *      `new Date()`, which parses it in the BROWSER's timezone. Every API
 *      value is UTC, so the browser silently shifted it by the local offset
 *      — and did it again on every screen that formatted dates by hand
 *      (there were ~40 ad-hoc `new Date(...).toLocaleString()` calls).
 *   2. The rendered timezone was whatever the browser happened to use. The
 *      server, the calendar and the reports all standardise on the viewer's
 *      timezone (default America/New_York); mail did not.
 *
 * Everything here is pure (no DOM, no app state) so it can be unit-tested
 * directly with node. `public/app.js` imports it and routes every mail date
 * through it; do not add another `new Date(...).toLocaleString()` for mail.
 *
 * CONTRACT
 *   * Input: ISO-8601 with `Z`/offset, "YYYY-MM-DD HH:MM:SS" (assumed UTC),
 *     RFC-2822 (`Mon, 9 Nov 2026 13:20:03 -0500`), epoch seconds or ms.
 *   * A value with NO offset is UTC. Never browser-local.
 *   * Output: rendered in the viewer's timezone — explicit user setting,
 *     else the browser timezone, else America/New_York.
 */

export const MAIL_DEFAULT_TIMEZONE = "America/New_York";

// "2026-11-09", "2026-11-09 18:20:03", "2026-11-09T18:20:03.123456"
const NAIVE_ISO = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?))?$/;

/**
 * Parse any timestamp we store or receive into a Date, or null.
 * Naive values are UTC (the API and DB are UTC; see mail_dates.py).
 */
export function parseMailDateInput(value) {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    const ms = Math.abs(value) < 1e12 ? value * 1000 : value; // seconds vs ms
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  if (typeof value === "object") {
    // Graph / OnlyOffice wrappers: { value: "..." } / { dateTime: "..." }
    const inner = value.value ?? value.dateTime ?? value.DateTime ?? null;
    return inner == null ? null : parseMailDateInput(inner);
  }
  let text = String(value).trim();
  if (!text) return null;
  const m = text.match(NAIVE_ISO);
  if (m) {
    // No offset in the source → it is UTC, not browser-local.
    text = `${m[1]}T${m[2] || "00:00:00"}Z`;
  }
  const d = new Date(text);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Browser timezone, or "" when Intl is unavailable. */
export function getBrowserTimezone() {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return typeof tz === "string" ? tz : "";
  } catch {
    return "";
  }
}

/**
 * The timezone mail is displayed in.
 *   opts.timezone        explicit per-call override
 *   opts.userTimezone    the signed-in user's setting (profile)
 *   else browser, else America/New_York
 */
export function resolveMailDisplayTimezone(opts = {}) {
  const explicit = (opts.timezone || "").trim();
  if (explicit) return explicit;
  const user = (opts.userTimezone || "").trim();
  if (user) return user;
  return getBrowserTimezone() || MAIL_DEFAULT_TIMEZONE;
}

const MODE_OPTIONS = {
  // "11/9/2026, 1:20:03 PM" — the email view header
  datetime: {
    year: "numeric", month: "numeric", day: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit",
  },
  // "11/9/2026, 1:20 PM" — list rows
  list: { year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit" },
  // "Monday, November 9, 2026 at 1:20 PM" — quoted/forwarded headers
  header: {
    weekday: "long", year: "numeric", month: "long", day: "numeric",
    hour: "numeric", minute: "2-digit",
  },
  date: { year: "numeric", month: "numeric", day: "numeric" },
  time: { hour: "numeric", minute: "2-digit" },
};

/**
 * Format a mail timestamp for display.
 * Returns "" for empty/unparseable input (callers may fall back to the raw
 * string when they want to show *something*).
 *
 * opts.mode       datetime (default) | list | header | date | time | iso
 * opts.timezone   override the display timezone
 * opts.userTimezone  the user's configured timezone
 * opts.locale     default "en-US" (stable, and what the UI copy expects)
 */
export function formatMailDate(value, opts = {}) {
  const d = parseMailDateInput(value);
  if (!d) return "";
  const mode = opts.mode || "datetime";
  if (mode === "iso") return d.toISOString();
  const options = MODE_OPTIONS[mode] || MODE_OPTIONS.datetime;
  const timeZone = resolveMailDisplayTimezone(opts);
  try {
    return new Intl.DateTimeFormat(opts.locale || "en-US", { ...options, timeZone }).format(d);
  } catch {
    // Invalid timezone name (bad user setting) — fall back to UTC rather
    // than showing nothing or the browser-local time.
    return new Intl.DateTimeFormat(opts.locale || "en-US", { ...options, timeZone: "UTC" }).format(d);
  }
}

/** formatMailDate, or the raw value when it cannot be parsed. */
export function formatMailDateOrRaw(value, opts = {}) {
  const formatted = formatMailDate(value, opts);
  if (formatted) return formatted;
  return value === null || value === undefined ? "" : String(value);
}
