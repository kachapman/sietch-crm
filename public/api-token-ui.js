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
//
// Default create body is FULL ACCESS. Optional restrictions are only added
// when the admin explicitly opts in at creation time.
(function (root) {
  "use strict";

  var TOKEN_NAME_MAX = 80;
  var CONTROL_CHARS_RE = /[\u0000-\u001f\u007f]/;
  var KNOWN_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];
  var KNOWN_RESOURCES = [
    "projects", "notes", "contacts", "documents", "tasks",
    "mail", "calendar", "history", "admin", "users", "me", "reference"
  ];

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

  function _cleanMethods(list) {
    if (!Array.isArray(list) || !list.length) return null;
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var m = String(list[i] || "").trim().toUpperCase();
      if (KNOWN_METHODS.indexOf(m) === -1) {
        return { error: "Unknown method: " + m };
      }
      if (out.indexOf(m) === -1) out.push(m);
    }
    return { value: out };
  }

  function _cleanResources(list) {
    if (!Array.isArray(list) || !list.length) return null;
    var out = [];
    for (var i = 0; i < list.length; i++) {
      var r = String(list[i] || "").trim().toLowerCase();
      if (KNOWN_RESOURCES.indexOf(r) === -1) {
        return { error: "Unknown resource: " + r };
      }
      if (out.indexOf(r) === -1) out.push(r);
    }
    return { value: out };
  }

  /**
   * Build the create POST body.
   * opts: { readOnly?: boolean, methods?: string[], resources?: string[], expiresAt?: string|null }
   * Default (no opts / empty opts) → { name } only → server grants full access.
   */
  function buildCreateRequest(raw, opts) {
    var check = validateTokenName(raw);
    if (!check.ok) return { ok: false, error: check.error, body: null };
    var body = { name: check.value };
    var o = opts || {};
    if (o.readOnly === true) {
      body.read_only = true;
      body.scope = "read";
    } else {
      body.scope = "full";
    }
    var restrictions = {};
    var methods = _cleanMethods(o.methods);
    if (methods && methods.error) return { ok: false, error: methods.error, body: null };
    if (methods && methods.value) restrictions.methods = methods.value;
    var resources = _cleanResources(o.resources);
    if (resources && resources.error) return { ok: false, error: resources.error, body: null };
    if (resources && resources.value) restrictions.resources = resources.value;
    if (Object.keys(restrictions).length) body.restrictions = restrictions;
    if (o.expiresAt) {
      var exp = String(o.expiresAt).trim();
      if (exp) body.expires_at = exp;
    }
    return { ok: true, error: "", body: body };
  }

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

  function summarizeToken(t) {
    if (!t || typeof t !== "object") return null;
    var id = Number(t.id);
    if (!Number.isFinite(id) || id <= 0) return null;
    var revokedAt = typeof t.revoked_at === "string" && t.revoked_at ? t.revoked_at : null;
    var restrictions = t.restrictions && typeof t.restrictions === "object" ? t.restrictions : {};
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
      expiresAt: typeof t.expires_at === "string" && t.expires_at ? t.expires_at : null,
      restrictions: restrictions,
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

  function describeRestrictions(row) {
    if (!row) return "full access";
    var bits = [];
    var scope = (row.scope || "full").toLowerCase();
    if (scope === "read") bits.push("read-only");
    else bits.push("full");
    var r = row.restrictions || {};
    if (Array.isArray(r.methods) && r.methods.length) {
      bits.push("methods " + r.methods.join("/"));
    }
    if (Array.isArray(r.resources) && r.resources.length) {
      bits.push("resources " + r.resources.join(", "));
    }
    if (row.expiresAt) bits.push("expires " + formatTokenDate(row.expiresAt));
    return bits.join(" · ");
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

  function isAdminViewer(flag) {
    return flag === true;
  }

  async function copySecretToClipboard(secret, deps) {
    var value = typeof secret === "string" ? secret : "";
    if (!value) return { ok: false, reason: "empty" };
    var d = deps || {};
    var clip = d.clipboard;
    if (clip && typeof clip.writeText === "function") {
      try {
        await clip.writeText(value);
        return { ok: true, method: "clipboard" };
      } catch (_e) {}
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

  /** Collect optional restriction fields from the admin create form (DOM). */
  function readCreateOptionsFromForm(doc) {
    var d = doc || (typeof document !== "undefined" ? document : null);
    if (!d || typeof d.getElementById !== "function") {
      return { readOnly: false, methods: [], resources: [], expiresAt: null };
    }
    var readOnlyEl = d.getElementById("api-token-read-only");
    var expiresEl = d.getElementById("api-token-expires");
    var methods = [];
    d.querySelectorAll("[data-api-token-method]:checked").forEach(function (el) {
      methods.push(el.value);
    });
    var resources = [];
    d.querySelectorAll("[data-api-token-resource]:checked").forEach(function (el) {
      resources.push(el.value);
    });
    var expiresAt = null;
    if (expiresEl && expiresEl.value) {
      // datetime-local → ISO-ish with local offset approximated as Z-less ISO
      var local = expiresEl.value; // YYYY-MM-DDTHH:MM
      var asDate = new Date(local);
      if (!Number.isNaN(asDate.getTime())) expiresAt = asDate.toISOString();
    }
    return {
      readOnly: !!(readOnlyEl && readOnlyEl.checked),
      methods: methods,
      resources: resources,
      expiresAt: expiresAt,
    };
  }

  function clearCreateOptionsForm(doc) {
    var d = doc || (typeof document !== "undefined" ? document : null);
    if (!d || typeof d.getElementById !== "function" || typeof d.querySelectorAll !== "function") return;
    var readOnlyEl = d.getElementById("api-token-read-only");
    if (readOnlyEl) readOnlyEl.checked = false;
    var expiresEl = d.getElementById("api-token-expires");
    if (expiresEl) expiresEl.value = "";
    d.querySelectorAll("[data-api-token-method], [data-api-token-resource]").forEach(function (el) {
      el.checked = false;
    });
  }

  root.SietchApiTokenUi = {
    TOKEN_NAME_MAX: TOKEN_NAME_MAX,
    KNOWN_METHODS: KNOWN_METHODS.slice(),
    KNOWN_RESOURCES: KNOWN_RESOURCES.slice(),
    validateTokenName: validateTokenName,
    buildCreateRequest: buildCreateRequest,
    extractCreatedSecret: extractCreatedSecret,
    summarizeToken: summarizeToken,
    summarizeTokenList: summarizeTokenList,
    countActiveTokens: countActiveTokens,
    formatTokenDate: formatTokenDate,
    tokenStatusText: tokenStatusText,
    describeRestrictions: describeRestrictions,
    revokeConfirm: revokeConfirm,
    isAdminViewer: isAdminViewer,
    copySecretToClipboard: copySecretToClipboard,
    readCreateOptionsFromForm: readCreateOptionsFromForm,
    clearCreateOptionsForm: clearCreateOptionsForm,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
