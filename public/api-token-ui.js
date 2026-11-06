// Shared, DOM-free helpers for the admin "API Tokens" tab (app.js).
//
// Kept as a classic script (like date-only.js) so the REAL functions can be
// loaded and exercised by node --test without a browser or npm deps.
//
// Security rules encoded here:
//   * token secrets are never returned in error strings, never logged, and
//     never persisted — they only travel from the create response into the
//     one-time reveal panel;
//   * list rendering whitelists the backend's declared safe metadata fields,
//     so an unexpected "token"/"token_hash" key can never reach the DOM.
(function (root) {
  "use strict";

  var TOKEN_NAME_MAX = 80;
  var CONTROL_CHARS_RE = /[\u0000-\u001f\u007f]/;

  function validateTokenName(raw) {
    var value = raw == null ? "" : String(raw);
    value = value.trim();
    if (!value) {
      return { ok: false, value: value, error: "Enter a name for this token." };
    }
    if (value.length > TOKEN_NAME_MAX) {
      return {
        ok: false,
        value: value,
        error: "Use " + TOKEN_NAME_MAX + " characters or fewer.",
      };
    }
    if (CONTROL_CHARS_RE.test(value)) {
      return { ok: false, value: value, error: "Name cannot contain control characters." };
    }
    return { ok: true, value: value, error: "" };
  }

  // The create contract is exactly {name}: scope is fixed server-side to the
  // single "read" scope, and the UI must not try to widen it.
  function buildCreateRequest(raw) {
    var check = validateTokenName(raw);
    if (!check.ok) return { ok: false, error: check.error, body: null };
    return { ok: true, error: "", body: { name: check.value } };
  }

  // The plaintext lives only in the create response (shown once).
  function extractCreatedSecret(payload) {
    var secret = payload && typeof payload.token === "string" ? payload.token : "";
    if (!secret) {
      return {
        ok: false,
        secret: "",
        error: "Token was created but no secret came back. Revoke it and create a new one.",
      };
    }
    return { ok: true, secret: secret, error: "" };
  }

  // Whitelist of metadata the list endpoint declares safe to display.
  // Deliberately drops any other key (e.g. a stray token/token_hash).
  function summarizeToken(t) {
    if (!t || typeof t !== "object") return null;
    var id = Number(t.id);
    if (!Number.isFinite(id) || id <= 0) return null;
    var revokedAt = typeof t.revoked_at === "string" && t.revoked_at ? t.revoked_at : null;
    return {
      id: id,
      name: t.name == null ? "" : String(t.name),
      prefix: t.token_prefix == null ? "" : String(t.token_prefix),
      scope: t.scope == null ? "" : String(t.scope),
      createdByEmail: t.created_by_email == null ? "" : String(t.created_by_email),
      createdAt: typeof t.created_at === "string" && t.created_at ? t.created_at : null,
      lastUsedAt: typeof t.last_used_at === "string" && t.last_used_at ? t.last_used_at : null,
      revokedAt: revokedAt,
      revoked: revokedAt !== null || t.revoked === true,
    };
  }

  function summarizeTokenList(payload) {
    var raw = payload && Array.isArray(payload.tokens) ? payload.tokens : [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var row = summarizeToken(raw[i]);
      if (row) out.push(row);
    }
    return out;
  }

  function countActiveTokens(rows) {
    if (!Array.isArray(rows)) return 0;
    return rows.filter(function (r) { return r && r.revoked !== true; }).length;
  }

  function formatTokenDate(value) {
    if (!value) return "\u2014";
    var d = new Date(value);
    if (Number.isNaN(d.getTime())) return "\u2014";
    try {
      return d.toLocaleString();
    } catch (_e) {
      return "\u2014";
    }
  }

  function tokenStatusText(row) {
    if (!row) return "";
    if (row.revoked) {
      return row.revokedAt ? "Revoked " + formatTokenDate(row.revokedAt) : "Revoked";
    }
    return "Active";
  }

  function revokeConfirm(row) {
    var name = row && row.name ? String(row.name) : "this token";
    var prefix = row && row.prefix ? String(row.prefix) : "";
    var message =
      'Revoke the token "' + name + '"? Any script or integration using it ' +
      "stops working immediately. This cannot be undone.";
    if (prefix) message += " Token prefix: " + prefix + ".";
    return {
      title: "Revoke API token",
      message: message,
      confirmLabel: "Revoke token",
    };
  }

  // Strict: only a real admin session may manage tokens.
  function isAdminViewer(flag) {
    return flag === true;
  }

  // Copy with a secure-context clipboard first, then a caller-supplied legacy
  // fallback. Never throws, never leaks the secret into the result, and never
  // surfaces the browser's error text (which could echo the payload).
  async function copySecretToClipboard(secret, deps) {
    var value = typeof secret === "string" ? secret : "";
    if (!value) return { ok: false, reason: "empty" };
    var d = deps || {};
    var clip = d.clipboard;
    if (clip && typeof clip.writeText === "function") {
      try {
        await clip.writeText(value);
        return { ok: true, method: "clipboard" };
      } catch (_e) {
        // fall through to the legacy path
      }
    }
    if (typeof d.legacyCopy === "function") {
      var legacyOk = false;
      try {
        legacyOk = await d.legacyCopy(value);
      } catch (_e) {
        legacyOk = false;
      }
      if (legacyOk) return { ok: true, method: "legacy" };
      return { ok: false, reason: "failed" };
    }
    return { ok: false, reason: "unavailable" };
  }

  root.SietchApiTokenUi = {
    TOKEN_NAME_MAX: TOKEN_NAME_MAX,
    validateTokenName: validateTokenName,
    buildCreateRequest: buildCreateRequest,
    extractCreatedSecret: extractCreatedSecret,
    summarizeToken: summarizeToken,
    summarizeTokenList: summarizeTokenList,
    countActiveTokens: countActiveTokens,
    formatTokenDate: formatTokenDate,
    tokenStatusText: tokenStatusText,
    revokeConfirm: revokeConfirm,
    isAdminViewer: isAdminViewer,
    copySecretToClipboard: copySecretToClipboard,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
