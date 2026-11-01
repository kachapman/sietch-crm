// Shared date-only parsing for the dashboard (app.js) and project.html.
// A "YYYY-MM-DD" string is a plain calendar date (PG DATE has no time or
// zone). Native new Date("2026-10-26") parses it as UTC midnight, which
// renders as the previous day in US timezones, so date-only strings are
// built as local midnight instead. Full datetimes keep native parsing.
function parseDateSmart(raw) {
  if (raw == null || raw === "") return null;
  var s = String(raw).trim();
  var m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return new Date(raw);
}

// Format a Date back to "YYYY-MM-DD" for <input type="date">.
function toDateInputValue(d) {
  if (!d || Number.isNaN(d.getTime())) return "";
  var mm = String(d.getMonth() + 1).padStart(2, "0");
  var dd = String(d.getDate()).padStart(2, "0");
  return d.getFullYear() + "-" + mm + "-" + dd;
}
