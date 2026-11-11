/**
 * Which project-history events are SYSTEM events, and how to summarize them.
 *
 * Ken (2026-11-09): "system events (status changes, auto-logged actions, etc.)
 * should render as a compact single-line entry (icon + short text + timestamp)
 * with NO reply button. Human notes and emails stay as they are now."
 *
 * Source of truth (2026-11-10): the server marks every auto-logged row with
 * `isSystemEvent` on the history row. The marker is authoritative because a
 * field change can be filed under ANY category (or none at all): on prod the
 * synthetic "Action" category was missing, so field changes landed with a NULL
 * category and rendered as full cards with a Reply button.
 *
 * The category checks (`isSystem` / `is_system`, then the known system titles)
 * remain as a fallback for unmarked legacy rows until the backfill runs.
 *
 * Emails are excluded FIRST: "Mail Message" is a system category, but an email
 * event must keep its full rendering (body, attachments, reply thread) even if
 * the row carries the marker.
 *
 * Pure module (no DOM) so `tests/test_history_system_events.mjs` can import the
 * shipped functions instead of copying them.
 */

export const SYSTEM_EVENT_MAX_CHARS = 140;

// Titles that are auto-logged even when the category row is missing its
// is_system flag (the synthetic "Action" category has no seed row).
const KNOWN_SYSTEM_TITLES = new Set([
  "action",
  "files are uploaded",
  "task is closed",
]);

/**
 * @param {object} input
 * @param {boolean} [input.isSystemEvent] authoritative server marker
 * @param {boolean} [input.is_system_event] snake_case alias of the marker
 * @param {{title?:string,isSystem?:boolean,is_system?:boolean}|null} input.category
 * @param {boolean} [input.isMailEvent]   true for any email event / payload
 * @param {boolean} [input.hasMailPayload]
 * @param {boolean} [input.hasMessageId]
 * @returns {boolean}
 */
export function isSystemHistoryEvent(input) {
  const o = input || {};
  // Emails never become compact, even though "Mail Message" is a system
  // category and even if the row is marked — the full email card is the point.
  if (o.isMailEvent || o.hasMailPayload || o.hasMessageId) return false;
  // Server-side marker: an auto-logged row is a system event regardless of
  // the category it was filed under (or a missing category).
  if (o.isSystemEvent === true || o.is_system_event === true) return true;
  const cat = o.category;
  if (!cat) return false;
  const title = String(cat.title || "").trim().toLowerCase();
  if (cat.isSystem === true || cat.is_system === true) return true;
  return KNOWN_SYSTEM_TITLES.has(title);
}

function decodeEntities(text) {
  return String(text || "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#0*39;|&apos;/gi, "'");
}

/**
 * One short, plain-text line for a system event.
 * Strips HTML, collapses whitespace and truncates with an ellipsis.
 */
export function systemEventText(ev, max = SYSTEM_EVENT_MAX_CHARS) {
  const e = ev || {};
  const raw = e.content != null && e.content !== ""
    ? e.content
    : (e.title != null ? e.title : "");
  let text = decodeEntities(String(raw).replace(/<[^>]*>/g, " "));
  text = text.replace(/\s+/g, " ").trim();
  if (!text) {
    const label = String((e.category && e.category.title) || "").trim();
    text = label ? label + " event" : "System activity";
  }
  const limit = Math.max(1, Number(max) || SYSTEM_EVENT_MAX_CHARS);
  if (text.length > limit) text = text.slice(0, limit - 1).trimEnd() + "\u2026";
  return text;
}

/**
 * Who triggered a system history event, for the compact line's right side.
 * Prefer the history row's author / created_by display name; if the change
 * was made with an API token and no user is present, use the token name;
 * otherwise "System".
 */
export function systemEventActor(ev) {
  const e = ev || {};
  const direct = String(e.author || e.Author || "").trim();
  if (direct) return direct;
  const createBy = e.createBy || e.CreateBy || e.createdBy || e.CreatedBy;
  if (createBy && typeof createBy === "object") {
    const name = String(
      createBy.displayName ||
        createBy.DisplayName ||
        createBy.display_name ||
        createBy.userName ||
        createBy.UserName ||
        createBy.name ||
        ""
    ).trim();
    if (name) return name;
  }
  const token = String(
    e.tokenName ||
      e.token_name ||
      e.apiTokenName ||
      e.api_token_name ||
      e.token?.name ||
      e.apiToken?.name ||
      ""
  ).trim();
  if (token) return token;
  return "System";
}

/**
 * Compact timestamp for the right side of a system-event line.
 * Current year: "Nov 9, 1:53 PM". Other years: "Nov 9, 2025, 1:53 PM EST".
 * `now` and `locale` are injectable so tests can pin them.
 */
export function formatSystemEventWhen(d, now = new Date(), locale) {
  if (!(d instanceof Date) || Number.isNaN(d.getTime())) return "";
  const sameYear = d.getFullYear() === now.getFullYear();
  const opts = {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  if (!sameYear) {
    opts.year = "numeric";
    opts.timeZoneName = "short";
  }
  try {
    return d.toLocaleString(locale, opts);
  } catch {
    return d.toLocaleString();
  }
}
